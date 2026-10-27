"""POST /api/v1/assessments and GET /api/v1/assessments/{run_id}.

The assessor is a fake that stores real run rows and can block, so the double
press is a real concurrent one. Fictional household only.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import Literal, get_args, get_origin
from urllib.parse import quote

import pytest
from fl_on_demand import (
    AUTH,
    JOIN_SECONDS,
    Rig,
    StoringAssessor,
    in_thread,
    make_engine,
    wait_until,
)
from sqlalchemy.exc import OperationalError

from family_logistics.api.assessments import AssessmentBody
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.domain.models import RunStatus

pytestmark = pytest.mark.unit

URL = "/api/v1/assessments"
UNKNOWN = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


def make_rig(engine, **options):
    assessor = StoringAssessor(engine, **options)
    rig = Rig(engine, assessor, on_demand_enabled=True)
    return rig, assessor


def settled(rig, run_id):
    assert wait_until(lambda: not rig.on_demand.in_flight())
    response = rig.client.get(f"{URL}/{run_id}", headers=AUTH)
    assert response.status_code == 200
    return response.json()


class TestAuthentication:
    def test_post_without_the_token_starts_nothing(self, engine):
        rig, assessor = make_rig(engine)
        response = rig.client.post(URL)
        assert response.status_code == 401
        assert not rig.on_demand.in_flight()
        assert assessor.run_ids == []
        assert rig.runs.latest_run() is None

    def test_get_without_the_token_reveals_nothing(self, engine):
        rig, _ = make_rig(engine)
        started = rig.client.post(URL, headers=AUTH).json()["run_id"]
        settled(rig, started)
        response = rig.client.get(f"{URL}/{started}", headers={})
        assert response.status_code == 401
        assert "state" not in response.json()
        rig.on_demand.stop()


class TestPost:
    def test_it_answers_202_with_a_run_id_and_the_run_completes(self, engine):
        rig, assessor = make_rig(engine)
        response = rig.client.post(URL, headers=AUTH)
        assert response.status_code == 202
        body = response.json()
        assert set(body) == {"run_id", "coalesced"}
        assert body["coalesced"] is False
        assert str(uuid.UUID(body["run_id"])) == body["run_id"]
        result = settled(rig, body["run_id"])
        assert result["state"] == "finished"
        assert result["status"] == "clear"
        assert result["trigger"] == "on_demand"
        assert assessor.triggers == ["on_demand"]
        rig.on_demand.stop()

    def test_a_request_body_is_ignored(self, engine):
        rig, _ = make_rig(engine)
        response = rig.client.post(URL, headers=AUTH, content=b"not json at all")
        assert response.status_code == 202
        rig.on_demand.stop()

    def test_two_concurrent_posts_get_the_same_run_id(self, engine):
        hold = threading.Event()
        rig, assessor = make_rig(engine, hold=hold)
        barrier = threading.Barrier(2)

        def press():
            barrier.wait(JOIN_SECONDS)
            return rig.client.post(URL, headers=AUTH)

        first, first_result = in_thread(press)
        second, second_result = in_thread(press)
        assert assessor.started.wait(JOIN_SECONDS)
        first.join(JOIN_SECONDS)
        second.join(JOIN_SECONDS)
        responses = [first_result[0], second_result[0]]
        hold.set()
        assert [r.status_code for r in responses] == [202, 202]
        bodies = [r.json() for r in responses]
        assert bodies[0]["run_id"] == bodies[1]["run_id"]
        assert sorted(b["coalesced"] for b in bodies) == [False, True]
        settled(rig, bodies[0]["run_id"])
        assert assessor.run_ids == [bodies[0]["run_id"]]
        rig.on_demand.stop()

    def test_a_post_while_the_service_stops_is_a_fixed_503(self, engine):
        rig, assessor = make_rig(engine)
        rig.on_demand.stop()
        response = rig.client.post(URL, headers=AUTH)
        assert response.status_code == 503
        assert response.json() == {"detail": "Service is shutting down"}
        assert assessor.run_ids == []


class TestDisabled:
    """``FL_ON_DEMAND_ENABLED`` is off by default: POST starts nothing."""

    def disabled_rig(self, engine):
        assessor = StoringAssessor(engine)
        return Rig(engine, assessor), assessor

    def test_the_default_configuration_is_disabled(self, engine):
        rig, _ = self.disabled_rig(engine)
        assert rig.settings.on_demand_enabled is False

    def test_post_is_a_fixed_503_that_starts_nothing_and_allocates_no_id(
        self, engine, monkeypatch
    ):
        rig, assessor = self.disabled_rig(engine)
        threads: list[object] = []
        original = threading.Thread.start

        def counting(thread):
            threads.append(thread)
            original(thread)

        monkeypatch.setattr(threading.Thread, "start", counting)
        response = rig.client.post(URL, headers=AUTH)
        assert response.status_code == 503
        assert response.json() == {
            "detail": "On-demand runs are disabled",
            "code": "on_demand_disabled",
        }
        assert "run_id" not in response.text
        assert not any(t.name == "on-demand-run" for t in threads)
        assert rig.on_demand.active_run_id() is None
        assert not rig.on_demand.in_flight()
        assert assessor.run_ids == []
        assert rig.runs.latest_run() is None

    def test_repeated_posts_never_start_a_run(self, engine):
        rig, assessor = self.disabled_rig(engine)
        codes = [rig.client.post(URL, headers=AUTH).status_code for _ in range(3)]
        assert codes == [503, 503, 503]
        assert assessor.run_ids == []
        assert not rig.on_demand.in_flight()

    def test_the_token_is_still_checked_first(self, engine):
        rig, _ = self.disabled_rig(engine)
        assert rig.client.post(URL).status_code == 401

    def test_the_read_routes_stay_available(self, engine):
        rig, _ = self.disabled_rig(engine)
        assert rig.client.get("/api/v1/status", headers=AUTH).status_code == 200
        response = rig.client.get(f"{URL}/{UNKNOWN}", headers=AUTH)
        assert response.status_code == 404

    def test_a_run_started_elsewhere_can_still_be_read(self, engine):
        rig, _ = self.disabled_rig(engine)
        now = datetime.now(UTC)
        run_id = rig.runs.begin_run(
            "daily", now, now, now, rules_version="test", run_id=UNKNOWN
        )
        body = rig.client.get(f"{URL}/{run_id}", headers=AUTH).json()
        assert body["run_id"] == UNKNOWN
        assert body["state"] == "finished"


class TestResponseSchema:
    def test_the_status_values_are_exactly_the_run_statuses(self):
        annotation = AssessmentBody.model_fields["status"].annotation
        members = get_args(annotation)
        literal = next(m for m in members if get_origin(m) is Literal)
        assert set(get_args(literal)) == {status.value for status in RunStatus}
        assert type(None) in members, "status is null until the run finishes"


class TestGetAssessment:
    def test_a_queued_run_has_no_status(self, engine):
        rig, assessor = make_rig(engine)
        rig.coordinator.lock.acquire()
        run_id = rig.client.post(URL, headers=AUTH).json()["run_id"]
        body = rig.client.get(f"{URL}/{run_id}", headers=AUTH).json()
        assert body["state"] == "queued"
        assert body["status"] is None
        assert body["counts"] is None
        assert assessor.run_ids == []
        rig.coordinator.lock.release()
        settled(rig, run_id)
        rig.on_demand.stop()

    def test_a_running_run_has_no_status_and_a_finished_one_has_counts(self, engine):
        hold = threading.Event()
        rig, assessor = make_rig(engine, hold=hold)
        run_id = rig.client.post(URL, headers=AUTH).json()["run_id"]
        assert assessor.started.wait(JOIN_SECONDS)
        running = rig.client.get(f"{URL}/{run_id}", headers=AUTH).json()
        assert running["state"] == "running"
        assert running["status"] is None
        assert running["counts"] is None
        assert running["finished_at"] is None
        hold.set()
        done = settled(rig, run_id)
        assert done["state"] == "finished"
        assert set(done["counts"].values()) == {0}
        assert done["finished_at"] is not None
        rig.on_demand.stop()

    @pytest.mark.parametrize(
        "status", [RunStatus.INCOMPLETE, RunStatus.ATTENTION_REQUIRED]
    )
    def test_a_finished_run_reports_the_status_it_was_stored_with(self, engine, status):
        rig, _ = make_rig(engine, status=status)
        run_id = rig.client.post(URL, headers=AUTH).json()["run_id"]
        assert settled(rig, run_id)["status"] == status.value
        rig.on_demand.stop()

    def test_a_run_that_crashed_is_failed_never_clear(self, engine):
        rig, _ = make_rig(engine, fail_at="after_begin")
        run_id = rig.client.post(URL, headers=AUTH).json()["run_id"]
        body = settled(rig, run_id)
        assert (body["state"], body["status"]) == ("finished", "failed")

    def test_a_run_that_raised_before_any_row_is_failed_never_clear(self, engine):
        rig, _ = make_rig(engine, fail_at="before_begin")
        run_id = rig.client.post(URL, headers=AUTH).json()["run_id"]
        body = settled(rig, run_id)
        assert (body["state"], body["status"]) == ("finished", "failed")
        assert rig.runs.latest_run() is None

    def test_an_unknown_run_is_a_fixed_404(self, engine):
        rig, _ = make_rig(engine)
        response = rig.client.get(f"{URL}/{UNKNOWN}", headers=AUTH)
        assert response.status_code == 404
        assert response.json() == {"detail": "Run not found"}
        assert UNKNOWN not in response.text

    @pytest.mark.parametrize(
        "run_id",
        [
            "private_marker_text",
            "x" * 600,
            "ABCDEF00-0000-4000-8000-000000000000",
            UNKNOWN + "0",
            "0000000000004000800000000000000000",
            quote("' OR 1=1 --"),
            "not%20a%20uuid",
        ],
    )
    def test_a_malformed_id_is_a_fixed_422_that_echoes_nothing(self, engine, run_id):
        rig, _ = make_rig(engine)
        response = rig.client.get(f"{URL}/{run_id}", headers=AUTH)
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid run id"}
        assert run_id not in response.text

    def test_a_store_error_is_a_503_never_a_state(self, engine, monkeypatch):
        rig, _ = make_rig(engine)

        def broken(_self, _run_id):
            raise OperationalError("select secret_table", {}, Exception("disk"))

        monkeypatch.setattr(AssessmentRepository, "get_run", broken)
        response = rig.client.get(f"{URL}/{UNKNOWN}", headers=AUTH)
        assert response.status_code == 503
        assert response.json() == {"detail": "Status store unavailable"}
        assert "secret_table" not in response.text
