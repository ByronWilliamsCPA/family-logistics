"""The one pipeline lock and in-flight run of a process.

The scheduler exists only when ``FL_SCHEDULER_ENABLED`` is true, but the
on-demand API always does, so the lock cannot belong to the scheduler. ``create_app``
builds one ``PipelineCoordinator`` and hands it to both: a scheduled pipeline
and an on-demand run then exclude each other whether or not the scheduler runs.

The coordinator also records which run is executing right now, in memory. The
run row is written with ``finished_at`` NULL and the safe status ``failed``
before the engine starts, and a process that dies leaves that row behind
forever, so "a row with no finish time" cannot tell a live run from a dead one.

#CRITICAL: Concurrency: the lock is process-local. A second process on the same
database (the ``collect`` CLI, a second ``serve``) is not excluded, and issue
#22 (one database write lock from history load to run completion) stays open.
#VERIFY: tests/unit/test_on_demand.py (TestSharedLock); docs/runbook.md
(On-demand runs) tells the operator to run one ``serve`` only.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Generator

    from family_logistics.domain.events import SchoolSnapshot
    from family_logistics.services.assessments import RunOutcome


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
    """Owns ``lock`` (one pipeline at a time) and the id of the run executing.

    Whoever holds ``lock`` may run an assessment; ``active_run_id`` is set only
    while that run executes, so it is never set by two runs at once.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._state = threading.Lock()
        self._active: str | None = None

    @property
    def active_run_id(self) -> str | None:
        """Return the id of the run executing now, or None."""
        with self._state:
            return self._active

    @contextmanager
    def tracking(self, run_id: str) -> Generator[None]:
        """Mark ``run_id`` as the executing run until the block exits.

        The caller must hold ``lock``. The mark is cleared after the run's row
        is stored (the block ends after ``AssessmentService.run`` returns), so
        a reader that sees no active run and an unfinished row knows the run
        died; see ``services.status``.

        Args:
            run_id: The run about to execute.

        Yields:
            Nothing.
        """
        with self._state:
            self._active = run_id
        try:
            yield
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

        Args:
            trigger: What started the run.
            school: The latest school snapshot, or None.

        Returns:
            The stored run and its result.
        """
        run_id = str(uuid.uuid4())
        with self._coordinator.tracking(run_id):
            return self._assessor.run(trigger, school, run_id=run_id)
