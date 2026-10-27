"""GET /api/v1/status: the family-wide card, never clear when it cannot be.

Fictional household only. The clock is fixed (``NOW``); runs and snapshots are
placed relative to it, so no test sleeps.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fl_on_demand import (
    AUTH,
    JOIN_SECONDS,
    NOW,
    Rig,
    StoringAssessor,
    make_engine,
    wait_until,
)
from sqlalchemy.exc import OperationalError

from family_logistics.db.assessments import AssessmentRepository
from family_logistics.domain.engine import AssessmentResult
from family_logistics.domain.lifecycle import Lifecycle, TrackedFinding
from family_logistics.domain.models import Finding, RunStatus, Severity

pytestmark = pytest.mark.unit

URL = "/api/v1/status"
PRIVATE = "private_marker_text"
FRESH_SCHOOL = NOW - timedelta(hours=1)
RECENT = NOW - timedelta(minutes=10)


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


@pytest.fixture
def rig(engine):
    rig = Rig(engine, StoringAssessor(engine))
    yield rig
    rig.on_demand.stop()


def seed_run(rig, status, finished_at=RECENT, findings=(), *, finish=True):
    started = finished_at - timedelta(minutes=1)
    run_id = rig.runs.begin_run(
        "daily", started, started, started, rules_version="test"
    )
    if finish:
        result = AssessmentResult(
            status,
            tuple(TrackedFinding(f, life) for f, life in findings),
            (),
            (),
            finished_at,
        )
        rig.runs.complete_run(run_id, result, finished_at, ())
    return run_id


def seed_school(rig, fetched_at=FRESH_SCHOOL):
    rig.schools.replace((), fetched_at=fetched_at, received_at=fetched_at)


def card(rig):
    response = rig.client.get(URL, headers=AUTH)
    assert response.status_code == 200
    return response.json()


def finding(rule="RIDE-005", key="ride-a", severity=Severity.CRITICAL):
    return Finding(rule, severity, f"{PRIVATE} {key}", (key,), PRIVATE)


class TestAuthentication:
    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer " + "w" * 40}],
        ids=["missing", "wrong"],
    )
    def test_without_the_token_there_is_no_card(self, rig, headers):
        seed_run(rig, RunStatus.CLEAR)
        response = rig.client.get(URL, headers=headers)
        assert response.status_code == 401
        assert "status" not in response.json()


class TestCard:
    def test_a_clear_run_with_fresh_inputs_is_clear(self, rig):
        seed_run(rig, RunStatus.CLEAR)
        seed_school(rig)
        body = card(rig)
        assert body["status"] == "clear"
        assert body["reasons"] == []
        assert body["analysis_stale"] is False
        assert body["school_snapshot_stale"] is False
        assert body["run_in_flight"] is False
        assert body["counts"] == {"critical": 0, "warning": 0, "incomplete": 0}
        assert body["lifecycle"] == {"new": 0, "ongoing": 0, "regressed": 0}

    def test_the_times_are_the_runs_finish_and_the_snapshot_fetch(self, rig):
        seed_run(rig, RunStatus.CLEAR)
        seed_school(rig)
        body = card(rig)
        assert body["last_analysis_at"].startswith(RECENT.isoformat()[:19])
        assert body["last_school_sync_at"].startswith(FRESH_SCHOOL.isoformat()[:19])
        assert body["generated_at"].startswith(NOW.isoformat()[:19])

    def test_counts_by_severity_and_lifecycle(self, rig):
        seed_run(
            rig,
            RunStatus.ATTENTION_REQUIRED,
            findings=[
                (finding(key="a"), Lifecycle.NEW),
                (finding(key="b", severity=Severity.WARNING), Lifecycle.ONGOING),
                (finding(key="c", severity=Severity.WARNING), Lifecycle.REGRESSED),
            ],
        )
        seed_school(rig)
        body = card(rig)
        assert body["status"] == "attention_required"
        assert body["counts"] == {"critical": 1, "warning": 2, "incomplete": 0}
        assert body["lifecycle"] == {"new": 1, "ongoing": 1, "regressed": 1}

    def test_the_card_holds_counts_and_times_only(self, rig):
        seed_run(
            rig,
            RunStatus.ATTENTION_REQUIRED,
            findings=[(finding(key="alex-pickup"), Lifecycle.NEW)],
        )
        seed_school(rig)
        response = rig.client.get(URL, headers=AUTH)
        assert set(response.json()) == {
            "status",
            "reasons",
            "counts",
            "lifecycle",
            "last_analysis_at",
            "last_school_sync_at",
            "analysis_stale",
            "school_snapshot_stale",
            "run_in_flight",
            "generated_at",
        }
        for text in (PRIVATE, "alex-pickup", "ride-a", "RIDE-005"):
            assert text not in response.text


class TestNeverClear:
    def test_no_run_yet_is_incomplete_not_clear(self, rig):
        seed_school(rig)
        body = card(rig)
        assert body["status"] == "incomplete"
        assert "no_run" in body["reasons"]
        assert body["analysis_stale"] is True
        assert body["counts"] is None
        assert body["lifecycle"] is None
        assert body["last_analysis_at"] is None

    @pytest.mark.parametrize(
        "status", [RunStatus.FAILED, RunStatus.INCOMPLETE, RunStatus.ATTENTION_REQUIRED]
    )
    def test_a_run_that_is_not_clear_keeps_its_status(self, rig, status):
        seed_run(rig, status)
        seed_school(rig)
        assert card(rig)["status"] == status.value

    def test_a_failed_run_still_shows_the_findings_it_kept_open(self, rig):
        seed_run(
            rig,
            RunStatus.ATTENTION_REQUIRED,
            RECENT - timedelta(minutes=30),
            findings=[(finding(), Lifecycle.NEW)],
        )
        failed = rig.runs.begin_run(
            "daily", RECENT, RECENT, RECENT, rules_version="test"
        )
        rig.runs.complete_run(
            failed,
            AssessmentResult(RunStatus.FAILED, (), (), (), RECENT, error="boom"),
            RECENT,
            (),
        )
        seed_school(rig)
        body = card(rig)
        assert body["status"] == "failed"
        assert body["counts"] == {"critical": 0, "warning": 0, "incomplete": 0}

    def test_a_clear_run_without_a_school_snapshot_is_incomplete(self, rig):
        seed_run(rig, RunStatus.CLEAR)
        body = card(rig)
        assert body["status"] == "incomplete"
        assert body["reasons"] == ["school_snapshot_missing"]
        assert body["school_snapshot_stale"] is True
        assert body["last_school_sync_at"] is None

    def test_a_run_that_started_and_never_finished_reads_as_failed(self, rig):
        seed_run(rig, RunStatus.CLEAR, RECENT - timedelta(minutes=30))
        seed_run(rig, RunStatus.CLEAR, RECENT, finish=False)
        seed_school(rig)
        body = card(rig)
        assert body["status"] == "failed"
        assert body["reasons"] == ["run_interrupted"]
        assert body["run_in_flight"] is False

    def test_a_run_that_finished_after_it_was_read_is_not_interrupted(
        self, rig, monkeypatch
    ):
        run_id = seed_run(rig, RunStatus.CLEAR)
        seed_school(rig)
        stored = rig.runs.get_run(run_id)
        assert stored is not None
        unfinished = SimpleNamespace(
            run_id=run_id, finished_at=None, status=stored.status, counts=stored.counts
        )
        monkeypatch.setattr(rig.runs, "latest_run", lambda: unfinished)
        body = card(rig)
        assert (body["status"], body["reasons"]) == ("clear", [])

    def test_an_executing_run_does_not_hide_the_last_finished_one(self, rig):
        seed_run(rig, RunStatus.ATTENTION_REQUIRED, RECENT - timedelta(minutes=30))
        running = seed_run(rig, RunStatus.CLEAR, RECENT, finish=False)
        seed_school(rig)
        with rig.coordinator.lock, rig.coordinator.tracking(running):
            body = card(rig)
        assert body["status"] == "attention_required"
        assert body["run_in_flight"] is True
        assert body["reasons"] == []

    def test_an_executing_run_with_no_earlier_run_is_incomplete(self, rig):
        running = seed_run(rig, RunStatus.CLEAR, RECENT, finish=False)
        seed_school(rig)
        with rig.coordinator.lock, rig.coordinator.tracking(running):
            body = card(rig)
        assert body["status"] == "incomplete"
        assert body["reasons"] == ["no_run"]


class TestStale:
    def test_an_analysis_older_than_the_source_limit_is_stale(self, rig):
        limit = timedelta(minutes=rig.settings.source_max_staleness_minutes)
        seed_run(rig, RunStatus.CLEAR, NOW - limit - timedelta(seconds=1))
        seed_school(rig)
        body = card(rig)
        assert body["status"] == "incomplete"
        assert body["analysis_stale"] is True
        assert body["reasons"] == ["analysis_stale"]

    def test_an_analysis_exactly_at_the_limit_is_not_stale(self, rig):
        limit = timedelta(minutes=rig.settings.source_max_staleness_minutes)
        seed_run(rig, RunStatus.CLEAR, NOW - limit)
        seed_school(rig)
        body = card(rig)
        assert (body["status"], body["analysis_stale"]) == ("clear", False)

    def test_the_limit_follows_the_staleness_setting(self, engine):
        rig = Rig(engine, StoringAssessor(engine), source_max_staleness_minutes=30)
        seed_run(rig, RunStatus.CLEAR, NOW - timedelta(minutes=31))
        seed_school(rig)
        assert card(rig)["analysis_stale"] is True
        rig.on_demand.stop()

    def test_a_school_snapshot_older_than_26_hours_is_stale(self, rig):
        seed_run(rig, RunStatus.CLEAR)
        seed_school(rig, NOW - timedelta(hours=26, seconds=1))
        body = card(rig)
        assert body["status"] == "incomplete"
        assert body["school_snapshot_stale"] is True
        assert body["reasons"] == ["school_snapshot_stale"]

    def test_a_school_snapshot_exactly_26_hours_old_is_not_stale(self, rig):
        seed_run(rig, RunStatus.CLEAR)
        seed_school(rig, NOW - timedelta(hours=26))
        body = card(rig)
        assert (body["status"], body["school_snapshot_stale"]) == ("clear", False)

    def test_a_stale_attention_card_stays_attention_and_flags_the_age(self, rig):
        seed_run(rig, RunStatus.ATTENTION_REQUIRED, NOW - timedelta(days=1))
        seed_school(rig)
        body = card(rig)
        assert body["status"] == "attention_required"
        assert body["analysis_stale"] is True


class TestRunInFlight:
    def test_a_queued_on_demand_run_is_in_flight(self, rig):
        seed_run(rig, RunStatus.CLEAR)
        seed_school(rig)
        rig.coordinator.lock.acquire()
        rig.on_demand.request()
        assert card(rig)["run_in_flight"] is True
        rig.coordinator.lock.release()
        assert wait_until(lambda: not card(rig)["run_in_flight"], JOIN_SECONDS)


class TestStoreFailure:
    def test_a_store_error_is_a_503_never_a_card(self, rig, monkeypatch):
        seed_run(rig, RunStatus.CLEAR)
        seed_school(rig)

        def broken(_self):
            raise OperationalError("select secret_table", {}, Exception("disk"))

        monkeypatch.setattr(AssessmentRepository, "latest_run", broken)
        response = rig.client.get(URL, headers=AUTH)
        assert response.status_code == 503
        assert response.json() == {"detail": "Status store unavailable"}
        assert "secret_table" not in response.text
