"""On-demand assessment runs for the Home Assistant button (ADR 0007).

Nothing calls this module yet: the status and assessment routes and the
application wiring that start it arrive in the next pull request, so it is
exercised only by its tests until then.

``POST /api/v1/assessments`` calls ``OnDemandService.request``, which answers
at once with a run id and does the work on one background thread. The id is
allocated before the run exists (``AssessmentRepository.begin_run`` accepts it),
so the caller can poll ``progress`` while the run waits for the pipeline lock.

Coalescing: one on-demand run is queued or running at a time, and a request
that finds one gets its id. A request that finds a scheduled run executing gets
that run's id (``PipelineCoordinator.active_run_id``). A request that finds
another pipeline holding the lock with no run executing (the scheduler
collecting, notifying or purging) queues one on-demand run behind it. Two
pipelines never run at once: the thread takes the shared lock first.

A run that has failed is never joined. A run that has just finished still can
be: between the moment its row is stored and the moment its executing mark
clears, a request gets that run's id and its progress reads ``finished`` with
the run's real status. That is the same answer as a request a moment earlier,
so it is allowed; the guarantee is that no caller reads a finished run as
queued or running.

An on-demand run assesses what is stored; it does not collect. A stale calendar
shows up as DATA-011 in the run, never as a clear result.

``in_flight`` is true while an on-demand run is queued or executing and while
any pipeline holds the shared lock: a scheduled collect, assess and notify, or
a purge. The executing-run mark covers only the assessment itself, so the lock
is the signal that covers the rest.

#CRITICAL: Timing: a request answered with a run id must always be able to
learn how that run ended. A run that raises (scheduled or on demand), never
gets the lock within ``LOCK_WAIT``, or is abandoned at shutdown is remembered
as failed by the ``PipelineCoordinator`` (bounded, in memory) when it left no
row; one that left a row is read from the database. Neither reads as clear.
#VERIFY: tests/unit/test_on_demand.py (TestFailures, TestFailureMemory,
TestShutdown).
#ASSUME: Concurrency: one process serves the API, so the in-memory ``_pending``
id and the coordinator's lock describe every run this app starts.
#VERIFY: tests/unit/test_on_demand.py (TestSharedLock). The operator guidance
to run a single ``serve`` process lands with the routes pull request (#42).
#EDGE: Timing: a queued run that never started when the process stopped has no
row, so after a restart its id answers 404; the caller posts again.
#VERIFY: tests/unit/test_on_demand.py (TestShutdown).
#CRITICAL: Concurrency: ``request`` publishes ``_pending`` and ``_thread`` only
after the thread has started, and does both under ``_state``. A thread that
cannot start (``RuntimeError: can't start new thread``) would otherwise leave a
queued id that no thread will ever run, so every later request would join it,
its progress would read ``queued`` forever and ``in_flight`` would stay true.
Publishing after the start needs no rollback, and a thread that finishes at once
cannot race it: its first touch of ``_state`` is ``_clear_pending``, which waits
for ``request`` to release the lock, by which time the id is published.
#VERIFY: tests/unit/test_on_demand.py (TestStartFailure).
#CRITICAL: Concurrency: shutdown must stop a queued run before the scheduler
releases the pipeline lock, or the queued run starts during shutdown. The app
calls ``begin_stop`` first, then stops the scheduler, then ``stop``. ``_acquire``
re-checks the stop flag after it gets the lock for the same reason.
#VERIFY: tests/unit/test_on_demand.py (TestShutdown).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from family_logistics.adapters.routing.factory import build_route_provider
from family_logistics.core.crypto import AddressCipher
from family_logistics.core.exceptions import BusinessLogicError
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.db.school_snapshots import SchoolSnapshotRepository
from family_logistics.domain.models import RunStatus
from family_logistics.services.assessments import (
    ON_DEMAND_TRIGGER,
    AssessmentService,
)
from family_logistics.services.scheduler import LOCK_WAIT
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import timedelta

    from sqlalchemy.engine import Engine

    from family_logistics.core.config import Settings
    from family_logistics.db.assessments import RunRecord
    from family_logistics.services.pipeline import PipelineCoordinator, RunAssessing
    from family_logistics.services.scheduler import SchoolSource

logger = get_logger(__name__)

TRIGGER: Final = ON_DEMAND_TRIGGER
"""``analysis_run.trigger`` of a run the API starts."""
ON_DEMAND_LOCK_WAIT: Final = LOCK_WAIT
"""How long a queued run waits for the scheduler's pipeline before it fails."""
_POLL_SECONDS: Final = 0.25
"""Lock wait slice, so a stop is noticed within this long."""

RunState = Literal["queued", "running", "finished"]
_QUEUED: Final = "queued"
_RUNNING: Final = "running"
_FINISHED: Final = "finished"


class OnDemandStoppedError(BusinessLogicError):
    """The service is shutting down and takes no new runs."""


class OnDemandStartError(BusinessLogicError):
    """The run's thread could not be started; no run was queued."""


@dataclass(frozen=True, slots=True)
class OnDemandTicket:
    """The answer to a request.

    Attributes:
        run_id: The run the caller should poll.
        coalesced: True when the id belongs to a run that was already queued or
            executing, False when this request queued a new one.
    """

    run_id: str
    coalesced: bool


@dataclass(frozen=True, slots=True)
class RunProgress:
    """Where one run stands; counts only, no finding text.

    Attributes:
        run_id: The run.
        state: ``queued`` (waiting for the pipeline), ``running`` or
            ``finished``.
        trigger: What started it, when a row exists.
        status: The run status, only once finished. A run that died, or was
            never stored, is ``failed``.
        counts: Open findings per severity, only for a finished run with a row.
        started_at: When it started, when a row exists.
        finished_at: When it finished, when it did.
    """

    run_id: str
    state: RunState
    trigger: str | None = None
    status: RunStatus | None = None
    counts: Mapping[str, int] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OnDemandService:
    """Starts coalesced on-demand runs on a background thread.

    Args:
        coordinator: The lock and in-flight mark shared with the scheduler.
        assessor_factory: Builds the assessment service when a run starts, so a
            setting the factory rejects fails that run, not application start.
        school: The school snapshot store.
        repository: The run repository (progress reads).
        lock_wait: How long a queued run waits for the pipeline lock.
    """

    # The collaborators are injected so tests can replace each one.
    def __init__(  # noqa: PLR0913
        self,
        coordinator: PipelineCoordinator,
        assessor_factory: Callable[[], RunAssessing],
        school: SchoolSource,
        repository: AssessmentRepository,
        *,
        lock_wait: timedelta = ON_DEMAND_LOCK_WAIT,
    ) -> None:
        self._coordinator = coordinator
        self._assessor_factory = assessor_factory
        self._school = school
        self._repo = repository
        self._lock_wait = lock_wait.total_seconds()
        self._state = threading.Lock()
        self._stopping = threading.Event()
        self._pending: str | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        engine: Engine,
        coordinator: PipelineCoordinator,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> OnDemandService:
        """Build the service from settings; nothing is opened until a run.

        Args:
            settings: Application settings.
            engine: Database engine at the schema head.
            coordinator: Shared with the scheduler.
            clock: The current aware time (replaceable in tests).

        Returns:
            The service.
        """

        def build() -> RunAssessing:
            provider = build_route_provider(settings, engine=engine)
            key = settings.data_key
            cipher = None if key is None else AddressCipher(key)
            return AssessmentService(
                engine, settings, provider, cipher=cipher, clock=clock
            )

        return cls(
            coordinator,
            build,
            SchoolSnapshotRepository(engine),
            AssessmentRepository(engine),
        )

    def in_flight(self) -> bool:
        """Report whether a run is queued or a pipeline is executing here.

        True while an on-demand run is queued, while a run executes, and while
        any pipeline holds the shared lock (a scheduled collect, assess and
        notify, or a purge), so a scan that is still collecting counts.
        """
        with self._state:
            return (
                self._pending is not None
                or self._coordinator.active_run_id is not None
                or self._coordinator.lock.locked()
            )

    def active_run_id(self) -> str | None:
        """Return the id of the run executing now (scheduled or on demand)."""
        return self._coordinator.active_run_id

    def request(self) -> OnDemandTicket:
        """Queue an on-demand run, or join the one already queued or executing.

        Returns:
            The run id and whether it was joined.

        Raises:
            OnDemandStoppedError: The service is stopping.
            OnDemandStartError: The run's thread could not be started. Nothing
                was queued and no id was handed out: the state is as it was
                before the call, so the next request tries again with a new id
                and ``progress`` of the id that was never issued is unknown.
        """
        with self._state:
            if self._stopping.is_set():
                msg = "on-demand runs are stopping"
                raise OnDemandStoppedError(msg)
            pending = self._pending
            if pending is not None and not self._coordinator.has_failed(pending):
                # A pending run that already failed is only waiting for its
                # thread to clear the id; joining it would hand out a dead run.
                return OnDemandTicket(pending, coalesced=True)
            active = self._coordinator.active_run_id
            if active is not None and not self._coordinator.has_failed(active):
                # The same holds for an executing run that raised: it is
                # remembered as failed before its mark is cleared.
                return OnDemandTicket(active, coalesced=True)
            run_id = str(uuid.uuid4())
            thread = threading.Thread(
                target=self._work, args=(run_id,), name="on-demand-run", daemon=True
            )
            # #CRITICAL: Concurrency: publish only a thread that started. The
            # thread cannot clear the id before this block ends: ``_work`` takes
            # ``_state`` to do so, and this block holds it.
            # #VERIFY: tests/unit/test_on_demand.py (TestStartFailure).
            try:
                thread.start()
            except RuntimeError as exc:
                msg = "the on-demand run could not be started"
                raise OnDemandStartError(msg) from exc
            self._pending = run_id
            self._thread = thread
        return OnDemandTicket(run_id, coalesced=False)

    def progress(self, run_id: str) -> RunProgress | None:
        """Report one run's progress.

        Args:
            run_id: A run id (any shape; an unknown one is simply not found).

        Returns:
            The progress, or None when no such run is known.
        """
        # In-memory state is read before the row, and in this order: the failure
        # memory is written before the executing mark and the pending id are
        # cleared, so a reader that finds neither also finds the failure.
        active = self._coordinator.active_run_id
        with self._state:
            pending = self._pending
        failed = self._coordinator.has_failed(run_id)
        row = self._repo.get_run(run_id)
        if row is not None and row.finished_at is None and run_id != active:
            # #CRITICAL: Timing: ``active`` is older than the row. A queued run
            # that began in between has an unfinished row and a stale "nothing
            # is executing", and would read as dead while it is live.
            # #VERIFY: tests/unit/test_on_demand.py (TestProgressRaces).
            if self._coordinator.active_run_id == run_id:
                return _running(run_id, row)
            # Not executing now, so any run that ended did so before this read
            # and the row it left is final.
            row = self._repo.get_run(run_id)
        if row is None or row.finished_at is None:
            # A run whose row has a finish time is never live, even while the
            # marks are still being cleared, so no caller reads "running" or
            # "queued" for a run that has ended.
            if run_id == active:
                return _running(run_id, row)
            if failed or row is not None:
                # Remembered as failed, or never finished and not executing:
                # the process that began the row died.
                return _failed(run_id, row)
            if run_id == pending:
                return RunProgress(run_id, _QUEUED)
            return None
        return RunProgress(
            run_id,
            _FINISHED,
            row.trigger,
            row.status,
            row.counts,
            row.started_at,
            row.finished_at,
        )

    def begin_stop(self) -> None:
        """Refuse new runs and abandon a queued one, without waiting.

        Call it before anything that releases the pipeline lock at shutdown (the
        scheduler's ``stop``), so a queued run is never started by that release.
        An executing run is not interrupted; ``stop`` waits for it.
        """
        with self._state:
            self._stopping.set()

    def stop(self) -> None:
        """Refuse new runs, abandon a queued one and wait for an executing one.

        Safe to call more than once and before any request. The join has no
        timeout, the same posture as the scheduler's ``shutdown(wait=True)``: an
        executing run is allowed to finish rather than be cut off.
        """
        self.begin_stop()
        with self._state:
            thread = self._thread
        if thread is not None:
            thread.join()

    def _clear_pending(self, run_id: str) -> None:
        """Forget ``run_id`` as queued; a newer pending run is left alone."""
        with self._state:
            if self._pending == run_id:
                self._pending = None

    def _acquire(self) -> bool:
        """Take the pipeline lock; False on timeout or when stopping."""
        deadline = time.monotonic() + self._lock_wait
        while not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._coordinator.lock.acquire(timeout=min(_POLL_SECONDS, remaining)):
                if not self._stopping.is_set():
                    return True
                # The stop began while this thread waited; a lock released by
                # the shutting-down scheduler must not start a run.
                self._coordinator.lock.release()
        return False

    def _work(self, run_id: str) -> None:
        """Run one assessment under the pipeline lock; never raises."""
        stored = False
        try:
            stored = self._run_locked(run_id)
        # A raised run must be remembered as failed, never lost.
        except Exception as exc:  # noqa: BLE001
            # Only the class name is logged: a traceback can carry household text.
            logger.error(  # noqa: TRY400
                "on_demand.run_failed", run_id=run_id, error=type(exc).__name__
            )
        finally:
            # The failure is remembered before the pending id is cleared, so a
            # reader that no longer finds the run queued finds it failed.
            if not stored:
                self._coordinator.remember_failed(run_id)
            self._clear_pending(run_id)

    def _run_locked(self, run_id: str) -> bool:
        """Wait for the lock, then run; True when the run's outcome was stored."""
        if not self._acquire():
            logger.warning("on_demand.not_started", run_id=run_id)
            return False
        try:
            assessor = self._assessor_factory()
            with self._coordinator.tracking(run_id):
                assessor.run(TRIGGER, self._school.latest(), run_id=run_id)
                # The row is stored: the run is no longer queued. Clearing the
                # id here, still marked as executing, leaves no moment in which
                # a reader sees it queued. A request in the moment before the
                # mark clears still joins this run, whose progress already reads
                # finished with its real status (the same as a moment earlier).
                self._clear_pending(run_id)
        finally:
            self._coordinator.lock.release()
        return True


def _failed(run_id: str, row: RunRecord | None) -> RunProgress:
    """Describe a run that failed or died; it is never clear."""
    if row is None:
        return RunProgress(run_id, _FINISHED, status=RunStatus.FAILED)
    return RunProgress(
        run_id, _FINISHED, row.trigger, RunStatus.FAILED, None, row.started_at
    )


def _running(run_id: str, row: RunRecord | None) -> RunProgress:
    """Describe the executing run; its row exists once the run has begun."""
    if row is None:
        return RunProgress(run_id, _RUNNING)
    return RunProgress(run_id, _RUNNING, row.trigger, started_at=row.started_at)
