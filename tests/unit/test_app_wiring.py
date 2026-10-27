"""``create_app`` wires one pipeline lock into the scheduler and the on-demand API.

Fictional household only; the assessor is a fake that stores real run rows.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from fl_on_demand import (
    AUTH,
    JOIN_SECONDS,
    TOKEN,
    FakeSchool,
    StoringAssessor,
    make_engine,
    wait_until,
)
from fl_service import make_settings

from family_logistics.api.app import create_app
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.services.on_demand import OnDemandService
from family_logistics.services.scheduler import SchedulerService

pytestmark = pytest.mark.unit

URL = "/api/v1/assessments"


class Wiring:
    """Replaces the two ``from_settings`` factories and records what they got."""

    def __init__(self, tmp_path, monkeypatch, *, scheduler_enabled, **assessor):
        self.engine = make_engine(tmp_path)
        self.assessor = StoringAssessor(self.engine, **assessor)
        self.settings = make_settings(
            database_path=tmp_path / "fl.db",
            ha_token=TOKEN,
            scheduler_enabled=scheduler_enabled,
        )
        self.coordinators: list[object] = []
        self.events: list[str] = []
        self.on_demand: OnDemandService | None = None
        self.scheduler_start_error: Exception | None = None
        monkeypatch.setattr(OnDemandService, "from_settings", self._on_demand)
        monkeypatch.setattr(SchedulerService, "from_settings", self._scheduler)

    def _on_demand(self, _settings, engine, coordinator, **_kwargs):
        self.coordinators.append(coordinator)
        self.on_demand = OnDemandService(
            coordinator,
            lambda: self.assessor,
            FakeSchool(),
            AssessmentRepository(engine),
        )
        original = self.on_demand.stop

        def spy():
            self.events.append("on_demand.stop")
            original()

        self.on_demand.stop = spy
        return self.on_demand

    def _scheduler(self, _settings, _engine, *, coordinator=None, **_kwargs):
        self.coordinators.append(coordinator)
        wiring = self

        class Stub:
            def start(self):
                wiring.events.append("scheduler.start")
                if wiring.scheduler_start_error is not None:
                    raise wiring.scheduler_start_error

            def stop(self):
                wiring.events.append("scheduler.stop")

        return Stub()

    def app(self):
        return create_app(self.settings)


@pytest.fixture
def wire(tmp_path, monkeypatch):
    created: list[Wiring] = []

    def build(*, scheduler_enabled, **assessor):
        wiring = Wiring(
            tmp_path, monkeypatch, scheduler_enabled=scheduler_enabled, **assessor
        )
        created.append(wiring)
        return wiring

    yield build
    for wiring in created:
        wiring.engine.dispose()


class TestRoutes:
    def test_the_three_routes_exist_and_need_the_token(self, wire):
        wiring = wire(scheduler_enabled=False)
        with TestClient(wiring.app()) as client:
            assert client.get("/api/v1/status").status_code == 401
            assert client.post(URL).status_code == 401
            assert (
                client.get(f"{URL}/{'0' * 8}-0000-4000-8000-{'0' * 12}").status_code
                == 401
            )
            assert client.get("/api/v1/status", headers=AUTH).status_code == 200


class TestSharedLock:
    def test_the_scheduler_and_the_on_demand_service_share_one_coordinator(self, wire):
        wiring = wire(scheduler_enabled=True)
        with TestClient(wiring.app()):
            pass
        assert len(wiring.coordinators) == 2
        assert wiring.coordinators[0] is wiring.coordinators[1]
        assert wiring.coordinators[0] is not None

    def test_with_the_scheduler_disabled_the_lock_still_holds_the_run_back(self, wire):
        wiring = wire(scheduler_enabled=False)
        with TestClient(wiring.app()) as client:
            (coordinator,) = wiring.coordinators
            coordinator.lock.acquire()
            try:
                run_id = client.post(URL, headers=AUTH).json()["run_id"]
                queued = client.get(f"{URL}/{run_id}", headers=AUTH).json()
                assert queued["state"] == "queued"
                assert wiring.assessor.run_ids == []
            finally:
                coordinator.lock.release()
            assert wait_until(lambda: wiring.assessor.run_ids == [run_id])
            assert wait_until(lambda: not wiring.on_demand.in_flight())
            done = client.get(f"{URL}/{run_id}", headers=AUTH).json()
            assert done["state"] == "finished"
            assert wiring.assessor.max_running == 1


class TestShutdown:
    def test_on_demand_stops_even_when_the_scheduler_fails_to_start(self, wire):
        wiring = wire(scheduler_enabled=True)
        wiring.scheduler_start_error = RuntimeError("start failed")
        with (
            pytest.raises(RuntimeError, match="start failed"),
            TestClient(wiring.app()),
        ):
            pass
        assert wiring.events == [
            "scheduler.start",
            "scheduler.stop",
            "on_demand.stop",
        ]

    def test_on_demand_stops_after_the_scheduler_when_all_goes_well(self, wire):
        wiring = wire(scheduler_enabled=True)
        with TestClient(wiring.app()):
            pass
        assert wiring.events == [
            "scheduler.start",
            "scheduler.stop",
            "on_demand.stop",
        ]

    def test_shutdown_waits_for_a_run_that_is_executing(self, wire):
        hold = threading.Event()
        wiring = wire(scheduler_enabled=False, hold=hold)
        with TestClient(wiring.app()) as client:
            run_id = client.post(URL, headers=AUTH).json()["run_id"]
            assert wiring.assessor.started.wait(JOIN_SECONDS)
            releaser = threading.Timer(0.3, hold.set)
            releaser.start()
        releaser.join(JOIN_SECONDS)
        rows = AssessmentRepository(wiring.engine)
        stored = rows.get_run(run_id)
        assert stored is not None
        assert stored.finished_at is not None

    def test_shutdown_abandons_a_queued_run_without_hanging(self, wire):
        wiring = wire(scheduler_enabled=False)
        with TestClient(wiring.app()) as client:
            (coordinator,) = wiring.coordinators
            coordinator.lock.acquire()
            run_id = client.post(URL, headers=AUTH).json()["run_id"]
        coordinator.lock.release()
        assert wiring.assessor.run_ids == []
        assert AssessmentRepository(wiring.engine).get_run(run_id) is None
