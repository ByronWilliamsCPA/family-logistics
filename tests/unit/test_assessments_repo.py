"""Run, finding-state and trip-snapshot persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fl_factories import notes, trip
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.db.schema import analysis_run, finding, trip_snapshot
from family_logistics.domain.engine import AssessmentResult, failed_result
from family_logistics.domain.lifecycle import Lifecycle, TrackedFinding
from family_logistics.domain.models import Finding, RunStatus, Severity
from family_logistics.domain.trip_codec import trip_to_json

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
WINDOW = (T0, T0 + timedelta(days=14))


def item(rule: str = "RIDE-005", key: str = "a", severity=Severity.CRITICAL):
    return Finding(rule, severity, f"{rule} for {key}", (key,), "do something")


def result(findings=(), resolved=(), status=RunStatus.ATTENTION_REQUIRED, now=T0):
    return AssessmentResult(
        status,
        tuple(TrackedFinding(f, life) for f, life in findings),
        tuple(resolved),
        (),
        now,
    )


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def repo(engine):
    return AssessmentRepository(engine)


def begin(repo, at=T0):
    return repo.begin_run("on_demand", *WINDOW, at, rules_version="m2.1")


def rows_of(engine):
    """Return every run, finding and snapshot row exactly as stored."""
    with engine.connect() as connection:
        return tuple(
            [
                dict(row)
                for row in connection.execute(
                    select(table).order_by(table.c[order])
                ).mappings()
            ]
            for table, order in (
                (analysis_run, "run_id"),
                (finding, "issue_id"),
                (trip_snapshot, "trip_key"),
            )
        )


class TestRuns:
    def test_a_started_run_is_failed_until_completed(self, repo):
        run_id = begin(repo)
        record = repo.get_run(run_id)
        assert record is not None
        assert record.status is RunStatus.FAILED
        assert record.finished_at is None
        assert record.error == "run did not finish"

    def test_completed_run_records_status_and_counts(self, repo):
        run_id = begin(repo)
        outcome = result(
            [
                (item(), Lifecycle.NEW),
                (item(key="b", severity=Severity.WARNING), Lifecycle.NEW),
            ]
        )
        repo.complete_run(run_id, outcome, T1, ())
        record = repo.get_run(run_id)
        assert record is not None
        assert record.status is RunStatus.ATTENTION_REQUIRED
        assert record.counts == {"critical": 1, "warning": 1, "incomplete": 0}
        assert record.finished_at == T1
        assert record.error is None

    def test_recent_runs_newest_first(self, repo):
        first = begin(repo, T0)
        second = begin(repo, T1)
        assert [r.run_id for r in repo.recent_runs()] == [second, first]
        latest = repo.latest_run()
        assert latest is not None
        assert latest.run_id == second

    def test_runs_started_at_the_same_time_list_the_latest_inserted_first(self, repo):
        run_ids = [begin(repo, T0) for _ in range(5)]
        assert [r.run_id for r in repo.recent_runs()] == run_ids[::-1]
        latest = repo.latest_run()
        assert latest is not None
        assert latest.run_id == run_ids[-1]

    def test_a_later_start_time_wins_over_a_later_insert(self, repo):
        newer = begin(repo, T2)
        older = begin(repo, T1)
        assert [r.run_id for r in repo.recent_runs()] == [newer, older]

    def test_counts_are_read_only_but_compare_equal_to_a_dict(self, repo):
        run_id = begin(repo)
        repo.complete_run(run_id, result([(item(), Lifecycle.NEW)]), T1, ())
        record = repo.get_run(run_id)
        assert record is not None
        assert record.counts == {"critical": 1, "warning": 0, "incomplete": 0}
        counts = cast("dict[str, int]", record.counts)
        with pytest.raises(TypeError):
            counts["critical"] = 0
        assert record.counts["critical"] == 1

    def test_unknown_run_and_empty_history(self, repo):
        assert repo.get_run("missing") is None
        assert repo.latest_run() is None
        history = repo.load_history()
        assert history.open_findings == ()
        assert history.trips == ()

    def test_error_text_is_truncated(self, repo):
        run_id = begin(repo)
        repo.complete_run(run_id, failed_result(T1, "x" * 900), T1, ())
        record = repo.get_run(run_id)
        assert record is not None
        assert record.error is not None
        assert len(record.error) == 500


class TestFindingState:
    def test_new_finding_is_stored_open(self, repo):
        run_id = begin(repo)
        repo.complete_run(run_id, result([(item(), Lifecycle.NEW)]), T1, ())
        history = repo.load_history()
        assert history.open_findings == (item(),)
        assert repo.findings_of(run_id) == (item(),)

    def test_ongoing_keeps_first_seen_and_updates_last_seen(self, engine, repo):
        repo.complete_run(begin(repo), result([(item(), Lifecycle.NEW)]), T1, ())
        repo.complete_run(
            begin(repo, T1), result([(item(), Lifecycle.ONGOING)]), T2, ()
        )
        with engine.connect() as connection:
            row = connection.execute(select(finding)).mappings().one()
        assert row["lifecycle"] == "ongoing"
        assert row["first_seen"] == T1
        assert row["last_seen"] == T2

    def test_resolved_finding_leaves_the_open_set(self, repo):
        repo.complete_run(begin(repo), result([(item(), Lifecycle.NEW)]), T1, ())
        repo.complete_run(
            begin(repo, T1), result([], [item()], RunStatus.CLEAR), T2, ()
        )
        history = repo.load_history()
        assert history.open_findings == ()
        assert history.resolved_issue_ids == frozenset({item().issue_id})

    def test_regression_reopens_with_original_first_seen(self, engine, repo):
        repo.complete_run(begin(repo), result([(item(), Lifecycle.NEW)]), T1, ())
        repo.complete_run(
            begin(repo, T1), result([], [item()], RunStatus.CLEAR), T1, ()
        )
        repo.complete_run(
            begin(repo, T2), result([(item(), Lifecycle.REGRESSED)]), T2, ()
        )
        with engine.connect() as connection:
            row = connection.execute(select(finding)).mappings().one()
        assert row["lifecycle"] == "regressed"
        assert row["resolved_at"] is None
        assert row["first_seen"] == T1
        assert repo.load_history().resolved_issue_ids == frozenset()

    def test_resolving_an_unknown_or_resolved_finding_is_a_no_op(self, repo):
        repo.complete_run(begin(repo), result([], [item()], RunStatus.CLEAR), T1, ())
        repo.complete_run(begin(repo, T1), result([(item(), Lifecycle.NEW)]), T1, ())
        repo.complete_run(
            begin(repo, T2), result([], [item()], RunStatus.CLEAR), T2, ()
        )
        repo.complete_run(
            begin(repo, T2), result([], [item()], RunStatus.CLEAR), T2, ()
        )
        assert repo.load_history().resolved_issue_ids == frozenset({item().issue_id})


class TestFailedRunWritesNothingElse:
    def test_failed_run_keeps_findings_and_trips(self, engine, repo):
        ride = trip(notes())
        repo.complete_run(begin(repo), result([(item(), Lifecycle.NEW)]), T1, [ride])
        failed = failed_result(T2, "boom", [item()])
        repo.complete_run(begin(repo, T1), failed, T2, [])
        history = repo.load_history()
        assert history.open_findings == (item(),)
        assert [t.key for t in history.trips] == [ride.key]
        with engine.connect() as connection:
            row = connection.execute(select(finding)).mappings().one()
        assert row["lifecycle"] == "new"


class TestTripSnapshot:
    def test_snapshot_is_replaced_every_completed_run(self, engine, repo):
        first = trip(notes(), uid="evt-1")
        second = trip(notes(), uid="evt-2")
        repo.complete_run(begin(repo), result(status=RunStatus.CLEAR), T1, [first])
        repo.complete_run(begin(repo, T1), result(status=RunStatus.CLEAR), T2, [second])
        with engine.connect() as connection:
            keys = connection.execute(select(trip_snapshot.c.trip_key)).scalars().all()
        assert keys == [second.key.as_str()]
        assert [t.key for t in repo.load_history().trips] == [second.key]

    def test_snapshot_round_trips_legs(self, repo):
        ride = trip(
            notes(family="Sam, Alex", destinations=(("Sam", "HOME"), ("Alex", "HOME")))
        )
        repo.complete_run(begin(repo), result(status=RunStatus.CLEAR), T1, [ride])
        (stored,) = repo.load_history().trips
        assert stored.legs() == ride.legs()

    def test_snapshot_rows_record_the_run_that_wrote_them(self, engine, repo):
        run_id = begin(repo)
        repo.complete_run(run_id, result(status=RunStatus.CLEAR), T1, [trip(notes())])
        with engine.connect() as connection:
            run = connection.execute(select(analysis_run.c.run_id)).scalar_one()
            snap = connection.execute(select(trip_snapshot.c.run_id)).scalar_one()
        assert run == snap == run_id

    def test_completing_an_unknown_run_is_rejected_and_writes_nothing(
        self, engine, repo
    ):
        kept = trip(notes(), uid="evt-1")
        repo.complete_run(begin(repo), result([(item(), Lifecycle.NEW)]), T1, [kept])
        before = rows_of(engine)

        # The run row must exist before its snapshot rows. The result has no
        # findings, so the snapshot insert is the statement that fails (the
        # foreign key is enforced on every connection), and the delete of the
        # old snapshot in the same call rolls back with it.
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            repo.complete_run(
                "no-such-run",
                result(status=RunStatus.CLEAR),
                T2,
                [trip(notes(), uid="evt-2")],
            )

        assert rows_of(engine) == before
        assert [t.key for t in repo.load_history().trips] == [kept.key]

    def test_a_failure_while_writing_the_snapshot_rolls_everything_back(
        self, engine, repo, monkeypatch
    ):
        first = trip(notes(), uid="evt-1")
        repo.complete_run(begin(repo), result([(item(), Lifecycle.NEW)]), T1, [first])
        before = rows_of(engine)
        run_id = begin(repo, T1)
        started = rows_of(engine)
        calls = []

        def second_trip_fails(ride):
            calls.append(ride)
            if len(calls) == 2:
                msg = "snapshot encoding failed"
                raise RuntimeError(msg)
            return trip_to_json(ride)

        monkeypatch.setattr(
            "family_logistics.db.assessments.trip_to_json", second_trip_fails
        )
        with pytest.raises(RuntimeError, match="snapshot encoding failed"):
            repo.complete_run(
                run_id,
                result([(item(key="b"), Lifecycle.NEW)], status=RunStatus.CLEAR),
                T2,
                [trip(notes(), uid="evt-2"), trip(notes(), uid="evt-3")],
            )

        assert len(calls) == 2
        assert rows_of(engine) == started
        assert started != before
        record = repo.get_run(run_id)
        assert record is not None
        assert record.status is RunStatus.FAILED
        assert record.error == "run did not finish"
        assert record.finished_at is None
        history = repo.load_history()
        assert history.open_findings == (item(),)
        assert [t.key for t in history.trips] == [first.key]
