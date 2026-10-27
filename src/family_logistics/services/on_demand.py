"""On-demand assessment runs for the Home Assistant button (ADR 0007).

``POST /api/v1/assessments`` calls ``OnDemandService.request``, which answers
at once with a run id and does the work on one background thread. The id is
allocated before the run exists (``AssessmentRepository.begin_run`` accepts it),
so the caller can poll ``progress`` while the run waits for the pipeline lock.

Coalescing: one on-demand run is queued or running at a time, and a request
that finds one gets its id. A request that finds a scheduled run executing gets
that run's id (``PipelineCoordinator.active_run_id``). A request that finds the
scheduler collecting (lock held, no run yet) queues one on-demand run behind it.
Two pipelines never run at once: the thread takes the shared lock first.

An on-demand run assesses what is stored; it does not collect. A stale calendar
shows up as DATA-011 in the run, never as a clear result.

#CRITICAL: Timing: a request answered with a run id must always be able to
learn how that run ended. A run that raises, never gets the lock within
``LOCK_WAIT``, or is abandoned at shutdown is remembered as failed (bounded,
in memory) when it left no row; one that left a row is read from the database.
Neither reads as clear.
#VERIFY: tests/unit/test_on_demand.py (TestFailures, TestShutdown).
#ASSUME: Concurrency: one process serves the API, so the in-memory ``_pending``
id and the coordinator's lock describe every run this app starts.
#VERIFY: docs/runbook.md (On-demand runs): run a single ``serve`` process.
#EDGE: Timing: a queued run that never started when the process stopped has no
row, so after a restart its id answers 404; the caller posts again.
#VERIFY: tests/unit/test_on_demand.py (TestShutdown).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from family_logistics.adapters.routing.factory import build_route_provider
from family_logistics.core.crypto import AddressCipher
from family_logistics.core.exceptions import BusinessLogicError
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.db.school_snapshots import SchoolSnapshotRepository
from family_logistics.domain.models import RunStatus
from family_logistics.services.assessments import AssessmentService
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

TRIGGER: Final = "on_demand"
"""``analysis_run.trigger`` of a run the API starts."""
ON_DEMAND_LOCK_WAIT: Final = LOCK_WAIT
"""How long a queued run waits for the scheduler's pipeline before it fails."""
_POLL_SECONDS: Final = 0.25
"""Lock wait slice, so a stop is noticed within this long."""
_REMEMBERED_FAILURES: Final = 32
"""Failed runs with no row that ``progress`` still answers for."""

RunState = Literal["queued", "running", "finished"]


class OnDemandStoppedError(BusinessLogicError):
    """The service is shutting down and takes no new runs."""


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

    # Keyword-only collaborators are injected so tests can replace each one.
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
        self._failed: OrderedDict[str, None] = OrderedDict()

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
        """Report whether any run is queued or executing in this process."""
        with self._state:
            return (
                self._pending is not None or self._coordinator.active_run_id is not None
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
        """
        with self._state:
            if self._stopping.is_set():
                msg = "on-demand runs are stopping"
                raise OnDemandStoppedError(msg)
            if self._pending is not None:
                return OnDemandTicket(self._pending, coalesced=True)
            active = self._coordinator.active_run_id
            if active is not None:
                return OnDemandTicket(active, coalesced=True)
            run_id = str(uuid.uuid4())
            self._pending = run_id
            self._thread = threading.Thread(
                target=self._work, args=(run_id,), name="on-demand-run", daemon=True
            )
            self._thread.start()
        return OnDemandTicket(run_id, coalesced=False)

    def progress(self, run_id: str) -> RunProgress | None:
        """Report one run's progress.

        Args:
            run_id: A run id (any shape; an unknown one is simply not found).

        Returns:
            The progress, or None when no such run is known.
        """
        # In-memory state is read before the row: the executing mark is cleared
        # only after the run's outcome is stored, so "not executing" plus an
        # unfinished row means the run died.
        active = self._coordinator.active_run_id
        with self._state:
            pending = self._pending
            failed = run_id in self._failed
        row = self._repo.get_run(run_id)
        if run_id == active:
            return _running(run_id, row)
        if run_id == pending:
            return RunProgress(run_id, "queued")
        if failed:
            return RunProgress(run_id, "finished", status=RunStatus.FAILED)
        if row is None:
            return None
        if row.finished_at is None:
            return RunProgress(
                run_id, "finished", row.trigger, RunStatus.FAILED, None, row.started_at
            )
        return RunProgress(
            run_id,
            "finished",
            row.trigger,
            row.status,
            row.counts,
            row.started_at,
            row.finished_at,
        )

    def stop(self) -> None:
        """Refuse new runs, abandon a queued one and wait for an executing one.

        Safe to call more than once and before any request.
        """
        with self._state:
            self._stopping.set()
            thread = self._thread
        if thread is not None:
            thread.join()

    def _acquire(self) -> bool:
        """Take the pipeline lock; False on timeout or when stopping."""
        deadline = time.monotonic() + self._lock_wait
        while not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._coordinator.lock.acquire(timeout=min(_POLL_SECONDS, remaining)):
                return True
        return False

    def _work(self, run_id: str) -> None:
        """Run one assessment under the pipeline lock; never raises."""
        stored = False
        try:
            stored = self._run_locked(run_id)
        except Exception as exc:  # noqa: BLE001 - a raised run must be remembered as failed
            logger.error(  # noqa: TRY400 - a traceback can carry household text
                "on_demand.run_failed", run_id=run_id, error=type(exc).__name__
            )
        finally:
            with self._state:
                self._pending = None
                if not stored:
                    self._failed[run_id] = None
                    while len(self._failed) > _REMEMBERED_FAILURES:
                        self._failed.popitem(last=False)

    def _run_locked(self, run_id: str) -> bool:
        """Wait for the lock, then run; True when the run's outcome was stored."""
        if not self._acquire():
            logger.warning("on_demand.not_started", run_id=run_id)
            return False
        try:
            assessor = self._assessor_factory()
            with self._coordinator.tracking(run_id):
                assessor.run(TRIGGER, self._school.latest(), run_id=run_id)
        finally:
            self._coordinator.lock.release()
        return True


def _running(run_id: str, row: RunRecord | None) -> RunProgress:
    """Describe the executing run; its row exists once the run has begun."""
    if row is None:
        return RunProgress(run_id, "running")
    return RunProgress(run_id, "running", row.trigger, started_at=row.started_at)
