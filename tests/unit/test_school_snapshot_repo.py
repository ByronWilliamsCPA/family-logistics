"""School snapshot repository: replace-all storage, freshness and retention."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from family_logistics.core.database import BUSY_TIMEOUT_MS, create_sqlite_engine
from family_logistics.core.exceptions import ValidationError
from family_logistics.db import migrations_api, schema, school_snapshots
from family_logistics.db.retention import (
    SCHOOL_DAY_RETENTION_DAYS,
    purge_expired_school_days,
)
from family_logistics.db.school_snapshots import (
    PURGED_CONTENT_HASH,
    SNAPSHOT_RETRY_AFTER_SECONDS,
    ReplaceHooks,
    ReplaceStatus,
    SchoolSnapshotBusyError,
    SchoolSnapshotConflictError,
    SchoolSnapshotRepository,
    content_hash,
)
from family_logistics.domain.context import HA_SNAPSHOT_SOURCE
from family_logistics.domain.events import SchoolDay, SchoolDayKind, SchoolSnapshot

pytestmark = pytest.mark.unit

FETCHED = datetime(2026, 10, 23, 13, 0, tzinfo=UTC)
RECEIVED = FETCHED + timedelta(minutes=1)
FRIDAY = date(2026, 10, 23)
EAST = timezone(timedelta(hours=5))
WEST = timezone(timedelta(hours=-7))


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def repo(engine):
    return SchoolSnapshotRepository(engine)


def school_day(
    person_id: str = "sam",
    day: date = FRIDAY,
    kind: SchoolDayKind = SchoolDayKind.NORMAL,
) -> SchoolDay:
    start = datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC)
    end = start if kind is SchoolDayKind.NO_SCHOOL else start + timedelta(hours=7)
    return SchoolDay(
        person_id, day, "middle-school", start, end, kind, HA_SNAPSHOT_SOURCE
    )


def count(engine, table) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


class TestLatest:
    def test_nothing_stored_is_none(self, repo):
        assert repo.latest() is None
        assert repo.freshness() is None

    def test_round_trip_returns_a_domain_snapshot(self, repo):
        days = (
            school_day("sam"),
            school_day("alex", kind=SchoolDayKind.NO_SCHOOL),
            school_day("jordan", FRIDAY + timedelta(days=3), SchoolDayKind.UNKNOWN),
        )
        repo.replace(days, fetched_at=FETCHED, received_at=RECEIVED)
        snapshot = repo.latest()
        assert isinstance(snapshot, SchoolSnapshot)
        assert set(snapshot.days) == set(days)
        assert snapshot.received_at == FETCHED
        assert all(d.start.tzinfo is not None for d in snapshot.days)
        assert {d.source for d in snapshot.days} == {HA_SNAPSHOT_SOURCE}

    def test_days_come_back_in_day_then_person_order(self, repo):
        later = school_day("alex", FRIDAY + timedelta(days=1))
        days = (later, school_day("sam"), school_day("alex"))
        repo.replace(days, fetched_at=FETCHED, received_at=RECEIVED)
        snapshot = repo.latest()
        assert snapshot is not None
        assert [(d.date, d.person_id) for d in snapshot.days] == [
            (FRIDAY, "alex"),
            (FRIDAY, "sam"),
            (FRIDAY + timedelta(days=1), "alex"),
        ]

    def test_an_empty_snapshot_is_still_a_snapshot(self, repo):
        repo.replace((), fetched_at=FETCHED, received_at=RECEIVED)
        assert repo.latest() == SchoolSnapshot((), FETCHED)

    def test_a_concurrent_replace_cannot_interleave_with_a_read(
        self, repo, engine, monkeypatch
    ):
        repo.replace((school_day("sam"),), fetched_at=FETCHED, received_at=RECEIVED)
        later = FETCHED + timedelta(hours=1)
        original = school_snapshots._freshness_on
        interleaved: list[bool] = []

        def freshness_then_concurrent_write(connection):
            freshness = original(connection)
            if not interleaved:
                interleaved.append(True)
                # Another connection replaces the snapshot after the reader
                # has read the freshness but before it reads the days.
                SchoolSnapshotRepository(engine).replace(
                    (school_day("alex"), school_day("jordan")),
                    fetched_at=later,
                    received_at=later,
                )
            return freshness

        monkeypatch.setattr(
            school_snapshots, "_freshness_on", freshness_then_concurrent_write
        )
        snapshot = repo.latest()
        assert interleaved == [True]
        assert snapshot is not None
        assert snapshot.received_at == FETCHED
        assert [d.person_id for d in snapshot.days] == ["sam"]
        monkeypatch.undo()
        newer = repo.latest()
        assert newer is not None
        assert newer.received_at == later
        assert [d.person_id for d in newer.days] == ["alex", "jordan"]


class TestFreshness:
    def test_freshness_is_the_earlier_of_fetched_and_received(self, repo):
        future = RECEIVED + timedelta(minutes=3)
        repo.replace((school_day(),), fetched_at=future, received_at=RECEIVED)
        freshness = repo.freshness()
        assert freshness is not None
        assert freshness.fetched_at == RECEIVED
        assert freshness.received_at == RECEIVED
        snapshot = repo.latest()
        assert snapshot is not None
        assert snapshot.received_at == RECEIVED

    def test_older_fetch_keeps_its_own_time(self, repo):
        repo.replace((), fetched_at=FETCHED, received_at=RECEIVED)
        freshness = repo.freshness()
        assert freshness is not None
        assert (freshness.fetched_at, freshness.received_at, freshness.day_count) == (
            FETCHED,
            RECEIVED,
            0,
        )

    @pytest.mark.parametrize("naive", ["fetched_at", "received_at"])
    def test_naive_timestamps_are_refused(self, repo, engine, naive):
        times = {"fetched_at": FETCHED, "received_at": RECEIVED}
        times[naive] = times[naive].replace(tzinfo=None)
        with pytest.raises(ValidationError):
            repo.replace((school_day(),), **times)
        assert count(engine, schema.school_day) == 0
        assert repo.latest() is None


class TestReplace:
    def test_replace_drops_every_earlier_day(self, repo):
        repo.replace(
            (school_day("sam"), school_day("alex")),
            fetched_at=FETCHED,
            received_at=RECEIVED,
        )
        later = FETCHED + timedelta(hours=1)
        outcome = repo.replace(
            (school_day("jordan"),), fetched_at=later, received_at=later
        )
        assert outcome.status is ReplaceStatus.STORED
        snapshot = repo.latest()
        assert snapshot is not None
        assert [d.person_id for d in snapshot.days] == ["jordan"]
        assert snapshot.received_at == later

    def test_identical_repost_is_unchanged(self, repo, engine):
        days = (school_day("sam"),)
        first = repo.replace(days, fetched_at=FETCHED, received_at=RECEIVED)
        again = repo.replace(
            days, fetched_at=FETCHED, received_at=RECEIVED + timedelta(minutes=5)
        )
        assert first.status is ReplaceStatus.STORED
        assert again.status is ReplaceStatus.UNCHANGED
        assert again.freshness == first.freshness
        freshness = repo.freshness()
        assert freshness is not None
        assert freshness.received_at == RECEIVED
        assert count(engine, schema.school_day) == 1

    @pytest.mark.parametrize("offset", [EAST, WEST])
    def test_the_same_instant_in_another_offset_is_unchanged(self, repo, offset):
        days = (school_day("sam"),)
        repo.replace(days, fetched_at=FETCHED, received_at=RECEIVED)
        again = repo.replace(
            days,
            fetched_at=FETCHED.astimezone(offset),
            received_at=RECEIVED + timedelta(minutes=5),
        )
        assert again.status is ReplaceStatus.UNCHANGED

    def test_an_older_fetch_is_a_conflict_and_changes_nothing(self, repo):
        repo.replace((school_day("sam"),), fetched_at=FETCHED, received_at=RECEIVED)
        before = repo.latest()
        with pytest.raises(SchoolSnapshotConflictError):
            repo.replace(
                (school_day("alex"),),
                fetched_at=FETCHED - timedelta(minutes=1),
                received_at=RECEIVED + timedelta(minutes=1),
            )
        assert repo.latest() == before

    def test_same_fetch_time_with_other_content_is_a_conflict(self, repo):
        repo.replace((school_day("sam"),), fetched_at=FETCHED, received_at=RECEIVED)
        with pytest.raises(SchoolSnapshotConflictError):
            repo.replace(
                (school_day("alex"),), fetched_at=FETCHED, received_at=RECEIVED
            )

    def test_a_failing_hook_rolls_back_the_whole_replace(self, repo, engine):
        repo.replace((school_day("sam"),), fetched_at=FETCHED, received_at=RECEIVED)
        before = repo.latest()

        def explode(connection, outcome):
            msg = "audit write failed"
            raise RuntimeError(msg)

        later = FETCHED + timedelta(hours=1)
        with pytest.raises(RuntimeError):
            repo.replace(
                (school_day("alex"), school_day("jordan")),
                fetched_at=later,
                received_at=later,
                before_commit=explode,
            )
        assert repo.latest() == before
        assert count(engine, schema.school_day) == 1

    def test_the_hook_sees_the_outcome_inside_the_transaction(self, repo):
        seen = []

        def record(connection, outcome):
            stored = connection.execute(
                select(func.count()).select_from(schema.school_day)
            ).scalar_one()
            seen.append((outcome.status, outcome.freshness.day_count, stored))

        repo.replace(
            (school_day("sam"), school_day("alex")),
            fetched_at=FETCHED,
            received_at=RECEIVED,
            before_commit=record,
        )
        repo.replace(
            (school_day("sam"), school_day("alex")),
            fetched_at=FETCHED,
            received_at=RECEIVED,
            before_commit=record,
        )
        assert seen == [(ReplaceStatus.STORED, 2, 2), (ReplaceStatus.UNCHANGED, 2, 2)]

    def test_the_check_runs_under_the_lock_before_the_comparison(self, repo):
        repo.replace((school_day("sam"),), fetched_at=FETCHED, received_at=RECEIVED)
        before = repo.latest()
        seen = []

        def refuse(connection):
            seen.append(
                connection.execute(
                    select(func.count()).select_from(schema.school_day)
                ).scalar_one()
            )
            msg = "zone not in the registry"
            raise ValidationError(msg, error_code="UNKNOWN_ZONE")

        # An older snapshot would be a conflict, but the check's own error
        # wins because it runs first.
        with pytest.raises(ValidationError) as info:
            repo.replace_with(
                (school_day("alex"),),
                fetched_at=FETCHED - timedelta(hours=1),
                received_at=RECEIVED,
                hooks=ReplaceHooks(before_compare=refuse),
            )
        assert info.value.error_code == "UNKNOWN_ZONE"
        assert seen == [1]
        assert repo.latest() == before

    def test_a_passing_check_lets_the_replace_proceed(self, repo):
        calls = []
        outcome = repo.replace_with(
            (school_day("sam"),),
            fetched_at=FETCHED,
            received_at=RECEIVED,
            hooks=ReplaceHooks(
                before_compare=lambda connection: calls.append("compare"),
                before_commit=lambda connection, outcome: calls.append("commit"),
            ),
        )
        assert outcome.status is ReplaceStatus.STORED
        assert calls == ["compare", "commit"]

    def test_duplicate_person_and_day_is_refused(self, repo, engine):
        with pytest.raises(ValidationError):
            repo.replace(
                (school_day("sam"), school_day("sam", kind=SchoolDayKind.UNKNOWN)),
                fetched_at=FETCHED,
                received_at=RECEIVED,
            )
        assert count(engine, schema.school_day) == 0


class TestWriteLock:
    def test_a_held_write_lock_is_a_busy_error(self, engine):
        repo = SchoolSnapshotRepository(engine, lock_timeout_ms=50)
        with engine.connect() as holder:
            holder.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                with pytest.raises(SchoolSnapshotBusyError) as info:
                    repo.replace(
                        (school_day(),), fetched_at=FETCHED, received_at=RECEIVED
                    )
            finally:
                holder.rollback()
        assert info.value.retry_after == SNAPSHOT_RETRY_AFTER_SECONDS
        assert info.value.error_code == "SNAPSHOT_BUSY"
        assert repo.latest() is None

    def test_the_lock_timeout_is_restored_after_a_replace(self, engine):
        repo = SchoolSnapshotRepository(engine, lock_timeout_ms=50)
        repo.replace((school_day(),), fetched_at=FETCHED, received_at=RECEIVED)
        with engine.connect() as connection:
            timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        assert timeout == BUSY_TIMEOUT_MS


class TestUnstorableTimes:
    # 0001-01-01 01:00 at UTC+5 is before 0001-01-01 00:00 UTC, the earliest
    # instant a datetime can hold, so converting it to UTC overflows.
    EARLIEST_EAST = datetime(1, 1, 1, 1, 0, tzinfo=EAST)

    def unstorable_day(self) -> SchoolDay:
        start = self.EARLIEST_EAST
        return SchoolDay(
            "sam",
            date(1, 1, 1),
            "middle-school",
            start,
            start,
            SchoolDayKind.NO_SCHOOL,
            HA_SNAPSHOT_SOURCE,
        )

    def test_an_unstorable_day_is_a_validation_error(self, repo, engine):
        with pytest.raises(ValidationError) as info:
            repo.replace(
                (self.unstorable_day(),), fetched_at=FETCHED, received_at=RECEIVED
            )
        assert info.value.error_code == "INVALID_TIME"
        assert count(engine, schema.school_day) == 0

    def test_an_unstorable_fetch_time_is_a_validation_error(self, repo):
        with pytest.raises(ValidationError) as info:
            repo.replace(
                (school_day(),), fetched_at=self.EARLIEST_EAST, received_at=RECEIVED
            )
        assert info.value.error_code == "INVALID_TIME"
        assert repo.latest() is None

    def test_a_bind_failure_is_still_a_validation_error(
        self, repo, engine, monkeypatch
    ):
        # Defence in depth: even if the up-front check were bypassed, the
        # column type's own conversion failure maps to the same error and
        # rolls back.
        monkeypatch.setattr(school_snapshots, "_require_storable", lambda *_: None)
        with pytest.raises(ValidationError) as info:
            repo.replace(
                (school_day("alex"), self.unstorable_day()),
                fetched_at=FETCHED,
                received_at=RECEIVED,
            )
        assert info.value.error_code == "INVALID_TIME"
        assert count(engine, schema.school_day) == 0
        assert repo.latest() is None


class TestContentHash:
    def test_hash_depends_on_days_and_fetch_time_only(self):
        days = (school_day("sam"), school_day("alex"))
        reordered = (days[1], days[0])
        assert content_hash(days, FETCHED) == content_hash(reordered, FETCHED)
        assert content_hash(days, FETCHED) != content_hash(days[:1], FETCHED)
        assert content_hash(days, FETCHED) != content_hash(
            days, FETCHED + timedelta(seconds=1)
        )

    def test_hash_uses_the_instant_not_the_offset(self):
        days = (school_day("sam"),)
        assert content_hash(days, FETCHED) == content_hash(
            days, FETCHED.astimezone(EAST)
        )

    def test_the_purged_marker_is_never_a_digest(self):
        assert len(PURGED_CONTENT_HASH) == 64
        assert set(PURGED_CONTENT_HASH) - set("0123456789abcdef")

    def test_hash_is_lowercase_hex(self):
        digest = content_hash((), FETCHED)
        assert len(digest) == 64
        assert digest == digest.lower()


class TestRetention:
    def test_old_school_days_are_purged(self, repo, engine):
        old_day = FRIDAY - timedelta(days=SCHOOL_DAY_RETENTION_DAYS + 2)
        repo.replace(
            (school_day("sam", old_day), school_day("sam")),
            fetched_at=FETCHED,
            received_at=RECEIVED,
        )
        with engine.begin() as connection:
            purged = purge_expired_school_days(connection, now=RECEIVED)
        assert purged == 1
        snapshot = repo.latest()
        assert snapshot is not None
        assert [d.date for d in snapshot.days] == [FRIDAY]
        with engine.begin() as connection:
            assert purge_expired_school_days(connection, now=RECEIVED) == 0

    def test_purge_keeps_the_day_count_and_hash_honest(self, repo, engine):
        old_day = FRIDAY - timedelta(days=SCHOOL_DAY_RETENTION_DAYS + 2)
        days = (school_day("sam", old_day), school_day("sam"))
        repo.replace(days, fetched_at=FETCHED, received_at=RECEIVED)
        with engine.begin() as connection:
            assert purge_expired_school_days(connection, now=RECEIVED) == 1
        freshness = repo.freshness()
        assert freshness is not None
        assert freshness.day_count == 1
        assert freshness.content_hash == PURGED_CONTENT_HASH
        assert freshness.fetched_at == FETCHED
        # The identical re-post no longer matches the stored hash, so it is not
        # reported as unchanged while its purged rows are gone.
        with pytest.raises(SchoolSnapshotConflictError):
            repo.replace(
                days, fetched_at=FETCHED, received_at=RECEIVED + timedelta(hours=1)
            )
        assert count(engine, schema.school_day) == 1

    def test_a_purge_that_removes_nothing_keeps_the_snapshot_as_is(self, repo, engine):
        days = (school_day("sam"),)
        repo.replace(days, fetched_at=FETCHED, received_at=RECEIVED)
        before = repo.freshness()
        with engine.begin() as connection:
            assert purge_expired_school_days(connection, now=RECEIVED) == 0
        assert repo.freshness() == before
        again = repo.replace(days, fetched_at=FETCHED, received_at=RECEIVED)
        assert again.status is ReplaceStatus.UNCHANGED

    def test_purge_without_a_snapshot_is_a_no_op(self, engine):
        with engine.begin() as connection:
            assert purge_expired_school_days(connection, now=RECEIVED) == 0
        assert count(engine, schema.school_snapshot) == 0

    def test_purge_refuses_a_naive_now(self, engine):
        with pytest.raises(ValidationError), engine.begin() as connection:
            purge_expired_school_days(connection, now=RECEIVED.replace(tzinfo=None))
