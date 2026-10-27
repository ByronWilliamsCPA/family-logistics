"""Builders for the on-demand run, status and assessment route tests.

A ``StoringAssessor`` stands in for ``AssessmentService``: it stores a real run
row through ``AssessmentRepository`` (so the progress and status reads see what
production would), can block on an event, and can fail before or after the row.
Everything here is fictional and carries no household data.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fl_service import make_settings

from family_logistics.api.assessments import register_assessment_routes
from family_logistics.api.ha import HaAuthThrottle, build_ha_router
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.db.school_snapshots import SchoolSnapshotRepository
from family_logistics.db.sources import CalendarSourceRepository
from family_logistics.domain.engine import AssessmentResult
from family_logistics.domain.events import CalendarKind
from family_logistics.domain.models import RunStatus
from family_logistics.services.assessments import RunOutcome
from family_logistics.services.collector import CollectionRun
from family_logistics.services.notifications import DispatchReport
from family_logistics.services.on_demand import OnDemandService
from family_logistics.services.pipeline import PipelineCoordinator
from family_logistics.services.status import StatusService

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

JOIN_SECONDS = 10
"""Longest any test waits for a thread; a hang fails the test, not the run."""


def make_engine(tmp_path: Path) -> Engine:
    """Create a file-backed engine migrated to the schema head."""
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    return engine


def wait_until(predicate, seconds: float = JOIN_SECONDS) -> bool:
    """Poll ``predicate`` until it is true or ``seconds`` pass."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class FakeSchool:
    """The school snapshot store: no snapshot has arrived."""

    def latest(self):
        return None


class ProbeLock:
    """A pipeline lock that tells a test when a thread waits and when one lets go.

    ``waiting`` is set once a thread tries to take the lock while it is held.
    ``on_release(thread_name, hook)`` runs ``hook`` once, in the named thread,
    just before that thread releases the lock, so a test can look at the
    service in the moment after a run ends and before the lock is free.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hooks: dict[str, object] = {}
        self.waiting = threading.Event()

    def on_release(self, thread_name: str, hook) -> None:
        self._hooks[thread_name] = hook

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.locked():
            self.waiting.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        hook = self._hooks.pop(threading.current_thread().name, None)
        if hook is not None:
            hook()
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


class StoringAssessor:
    """An assessment service that stores a run row, optionally slowly or badly.

    Args:
        engine: Database engine at the schema head.
        status: The status the run is stored with.
        hold: When given, the run waits on it after its row is written.
        fail_at: ``before_begin`` raises before any row; ``after_begin`` raises
            once the placeholder row exists (a run that dies mid-way).
    """

    def __init__(
        self,
        engine: Engine,
        *,
        status: RunStatus = RunStatus.CLEAR,
        hold: threading.Event | None = None,
        fail_at: Literal["before_begin", "after_begin"] | None = None,
    ) -> None:
        self.repo = AssessmentRepository(engine)
        self.status = status
        self.hold = hold
        self.fail_at = fail_at
        self.started = threading.Event()
        self.run_ids: list[str] = []
        self.triggers: list[str] = []
        self.running = 0
        self.max_running = 0
        self._guard = threading.Lock()

    def run(self, trigger, school=None, *, run_id=None):
        with self._guard:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            return self._run(trigger, run_id)
        finally:
            with self._guard:
                self.running -= 1

    def _run(self, trigger: str, run_id: str | None) -> RunOutcome:
        if self.fail_at == "before_begin":
            msg = "assessor failed before its run began"
            raise RuntimeError(msg)
        now = datetime.now(UTC)
        stored = self.repo.begin_run(
            trigger, now, now, now, rules_version="test", run_id=run_id
        )
        self.run_ids.append(stored)
        self.triggers.append(trigger)
        self.started.set()
        if self.hold is not None:
            assert self.hold.wait(JOIN_SECONDS), "the test never released the run"
        if self.fail_at == "after_begin":
            msg = "assessor died after its run began"
            raise RuntimeError(msg)
        result = AssessmentResult(self.status, (), (), (), now)
        self.repo.complete_run(stored, result, datetime.now(UTC), ())
        return RunOutcome(stored, result, trigger)


class FakeCollector:
    """The collector: complete runs, optionally blocking inside the collection."""

    def __init__(self, on_run=None) -> None:
        self.on_run = on_run

    def run_once(self) -> CollectionRun:
        if self.on_run is not None:
            self.on_run()
        now = datetime.now(UTC)
        return CollectionRun(window_start=now, window_end=now)


def in_thread(function):
    """Run ``function`` in a thread; return the thread and its results."""
    results: list[object] = []
    thread = threading.Thread(target=lambda: results.append(function()))
    thread.start()
    return thread, results


class Notifier:
    """The notification service: every dispatch is a quiet, empty report."""

    def dispatch(self, outcome):
        return DispatchReport(outcome.run_id, "shadow")

    def dispatch_unavailable(self, trigger):
        return DispatchReport(trigger, "shadow")


class NoAps:
    """The slice of ``BackgroundScheduler`` the scheduler reads when not started."""

    running = False


TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
"""The status clock: runs and snapshots are placed relative to it."""


class Rig:
    """The HA router with the status and assessment routes over fakes.

    Attributes:
        client: A test client for the app (no lifespan).
        on_demand: The on-demand service (stop it at the end of a test).
        coordinator: The shared lock.
        status: The status service.
        runs: The run repository.
        schools: The school snapshot store.
        sources: The calendar source store.
        settings: The settings the app was built with.
    """

    def __init__(
        self, engine, assessor, *, clock=None, on_demand_enabled=False, **settings
    ) -> None:
        self.settings = make_settings(
            ha_token=TOKEN, on_demand_enabled=on_demand_enabled, **settings
        )
        self.coordinator = PipelineCoordinator()
        self.runs = AssessmentRepository(engine)
        self.schools = SchoolSnapshotRepository(engine)
        self.sources = CalendarSourceRepository(engine)
        self.on_demand = OnDemandService(
            self.coordinator, lambda: assessor, FakeSchool(), self.runs
        )
        self.status = StatusService(
            self.settings,
            runs=self.runs,
            schools=self.schools,
            sources=self.sources,
            on_demand=self.on_demand,
            clock=clock or (lambda: NOW),
        )
        router = build_ha_router(self.settings, throttle=HaAuthThrottle())
        register_assessment_routes(
            router,
            self.status,
            self.on_demand,
            on_demand_enabled=self.settings.on_demand_enabled,
        )
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app, client=("192.0.2.1", 50000))

    def collect_calendar(self, collected_at: datetime) -> None:
        """Record a complete collection of the transport calendar at that time."""
        calendar_id = "cal-transport-sample"
        self.sources.ensure(
            calendar_id, self.settings.caldav_transport_calendar, CalendarKind.TRANSPORT
        )
        self.sources.record_success(calendar_id, collected_at)
