"""The one pipeline lock and in-flight run of a process.

The scheduler exists only when ``FL_SCHEDULER_ENABLED`` is true, but the
on-demand API always does, so the lock cannot belong to the scheduler. ``create_app``
builds one ``PipelineCoordinator`` and hands it to both: a scheduled pipeline
and an on-demand run then exclude each other whether or not the scheduler runs.

The coordinator also records, in memory, which run is executing right now and
which recent runs raised. The run row is written with ``finished_at`` NULL and
the safe status ``failed`` before the engine starts, and a process that dies
leaves that row behind forever, so "a row with no finish time" cannot tell a
live run from a dead one. A run that raises before its row exists leaves no row
at all; the failure memory is what lets a caller that was handed its id (an
on-demand request that joined a scheduled run) still learn how it ended.

#CRITICAL: Concurrency: the lock is process-local. A second process on the same
database (the ``collect`` CLI, a second ``serve``) is not excluded, and issue
#22 (one database write lock from history load to run completion) stays open.
#VERIFY: tests/unit/test_on_demand.py (TestSharedLock); docs/runbook.md
(On-demand runs) tells the operator to run one ``serve`` only.

#CRITICAL: Timing: a run id that was handed to a caller must always resolve. A
run that raises is recorded as failed BEFORE its executing mark is cleared, so
a reader that sees no executing run also sees the failure. The memory holds
``REMEMBERED_FAILURES`` ids and is lost on restart; an evicted or restarted id
with no row reads as not found, never as clear.
#VERIFY: tests/unit/test_on_demand.py (TestFailureMemory).
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Generator

    from family_logistics.domain.events import SchoolSnapshot
    from family_logistics.services.assessments import RunOutcome

REMEMBERED_FAILURES: Final = 32
"""Failed runs with no row that the coordinator still answers for."""


class RunAssessing(Protocol):
    """An assessment service that accepts a pre-allocated run id."""

    def run(
        self,
        trigger: str,
        school: SchoolSnapshot | None = None,
        *,
        run_id: str | None = None,
    ) -> RunOutcome:
        """Run and store one assessment."""
        ...


class PipelineCoordinator:
    """Owns ``lock`` (one pipeline at a time), the executing run and failures.

    Whoever holds ``lock`` may run an assessment; ``active_run_id`` is set only
    while that run executes, so it is never set by two runs at once.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._state = threading.Lock()
        self._active: str | None = None
        self._failed: OrderedDict[str, None] = OrderedDict()

    @property
    def active_run_id(self) -> str | None:
        """Return the id of the run executing now, or None."""
        with self._state:
            return self._active

    def remember_failed(self, run_id: str) -> None:
        """Remember a run that failed and may have left no row (bounded).

        Args:
            run_id: The failed run.
        """
        with self._state:
            self._failed[run_id] = None
            self._failed.move_to_end(run_id)
            while len(self._failed) > REMEMBERED_FAILURES:
                self._failed.popitem(last=False)

    def has_failed(self, run_id: str) -> bool:
        """Report whether ``run_id`` is remembered as failed."""
        with self._state:
            return run_id in self._failed

    @contextmanager
    def tracking(self, run_id: str) -> Generator[None]:
        """Mark ``run_id`` as the executing run until the block exits.

        The caller must hold ``lock``. The mark is cleared after the run's row
        is stored (the block ends after ``AssessmentService.run`` returns), so
        a reader that sees no active run and an unfinished row knows the run
        died; see ``services.status``. A block that raises records ``run_id``
        as failed first, then clears the mark, and passes the error on.

        Args:
            run_id: The run about to execute.

        Raises:
            BaseException: Whatever the block raised, after the run is recorded
                as failed and the mark is cleared.
        """
        with self._state:
            self._active = run_id
        try:
            yield
        except BaseException:
            self.remember_failed(run_id)
            raise
        finally:
            with self._state:
                self._active = None

    def tracked(self, assessor: RunAssessing) -> TrackedAssessor:
        """Wrap an assessor so every run it makes is marked as executing.

        Args:
            assessor: The assessment service.

        Returns:
            An object with the scheduler's ``run(trigger, school)`` signature.
        """
        return TrackedAssessor(assessor, self)


class TrackedAssessor:
    """Runs an assessment under ``PipelineCoordinator.tracking``.

    Args:
        assessor: The assessment service.
        coordinator: The coordinator whose lock the caller already holds.
    """

    def __init__(
        self, assessor: RunAssessing, coordinator: PipelineCoordinator
    ) -> None:
        self._assessor = assessor
        self._coordinator = coordinator

    def run(self, trigger: str, school: SchoolSnapshot | None = None) -> RunOutcome:
        """Run one assessment with a pre-allocated, tracked run id.

        A run that raises is remembered as failed by the coordinator, so a
        caller that was given this id can still learn how the run ended.

        Args:
            trigger: What started the run.
            school: The latest school snapshot, or None.

        Returns:
            The stored run and its result.
        """
        run_id = str(uuid.uuid4())
        with self._coordinator.tracking(run_id):
            return self._assessor.run(trigger, school, run_id=run_id)
