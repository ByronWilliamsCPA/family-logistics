"""Calendar source, sync state and occurrence repositories."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from caldav_fakes import register_person_calendar

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.sources import (
    MAX_ERROR_LENGTH,
    CalendarSourceRepository,
    SourceEventRepository,
    SyncState,
    SyncStateRepository,
    UnregisteredCalendarError,
)
from family_logistics.domain.events import (
    CalendarKind,
    CalendarSource,
    CollectionResult,
    SourceEvent,
)

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
START = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
END = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
SOURCE = CalendarSource("cal-a", "Sam", CalendarKind.PERSON)


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    register_person_calendar(engine, "cal-a", "Sam")
    yield engine
    engine.dispose()


def occurrence(
    uid: str, day: int, *, summary: str = "Lesson", rid: str = ""
) -> SourceEvent:
    start = datetime(2026, 10, day, 23, 0, tzinfo=UTC)
    return SourceEvent(
        calendar_id="cal-a",
        uid=uid,
        recurrence_id=rid,
        start=start,
        end=start + timedelta(hours=1),
        all_day=False,
        summary=summary,
        location=None,
        description=None,
        status="confirmed",
        etag=None,
        raw_hash=f"{uid}-{rid}-{summary}",
        is_override=False,
        series_uid_has_rrule=bool(rid),
        last_seen=NOW,
    )


def result(*events: SourceEvent, complete: bool = True) -> CollectionResult:
    return CollectionResult(
        SOURCE, events, START, END, NOW, complete, None if complete else "partial"
    )


@pytest.mark.unit
class TestCalendarSources:
    def test_ensure_keeps_registry_owned_fields(self, engine):
        repo = CalendarSourceRepository(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE calendar_source SET kind='school_bell', person_id=NULL, "
                "enabled=0 WHERE calendar_id='cal-a'"
            )
        source = repo.ensure("cal-a", "Sam (renamed)", CalendarKind.TRANSPORT)
        assert source.display_name == "Sam (renamed)"
        assert source.kind is CalendarKind.SCHOOL_BELL
        assert not source.enabled

    def test_ensure_registers_a_new_transport_calendar(self, engine):
        repo = CalendarSourceRepository(engine)
        source = repo.ensure("cal-t", "Family Transportation", CalendarKind.TRANSPORT)
        assert source.kind is CalendarKind.TRANSPORT
        assert source.person_id is None

    def test_ensure_refuses_a_new_person_calendar_without_an_owner(self, engine):
        repo = CalendarSourceRepository(engine)
        with pytest.raises(UnregisteredCalendarError, match="registry seed"):
            repo.ensure("cal-new", "Jordan", CalendarKind.PERSON)
        assert repo.get("cal-new") is None

    def test_success_and_failure_are_recorded(self, engine):
        repo = CalendarSourceRepository(engine)
        repo.record_failure("cal-a", "x" * (MAX_ERROR_LENGTH + 50))
        failed = repo.get("cal-a")
        assert failed is not None
        assert failed.last_error is not None
        assert len(failed.last_error) == MAX_ERROR_LENGTH
        repo.record_success("cal-a", NOW)
        ok = repo.get("cal-a")
        assert ok is not None
        assert ok.last_success == NOW
        assert ok.last_error is None

    def test_all_and_missing(self, engine):
        repo = CalendarSourceRepository(engine)
        repo.ensure("cal-b", "Family Transportation", CalendarKind.TRANSPORT)
        assert [s.calendar_id for s in repo.all()] == ["cal-b", "cal-a"]
        assert repo.get("cal-missing") is None


@pytest.mark.unit
class TestSyncState:
    def test_save_inserts_then_updates(self, engine):
        repo = SyncStateRepository(engine)
        assert repo.get("cal-a") is None
        repo.save(SyncState("cal-a", "t1", "c1", NOW))
        repo.save(SyncState("cal-a", "t2", None, NOW + timedelta(hours=1)))
        assert repo.get("cal-a") == SyncState(
            "cal-a", "t2", None, NOW + timedelta(hours=1)
        )


@pytest.mark.unit
class TestOccurrences:
    def test_insert_then_unchanged_then_updated(self, engine):
        repo = SourceEventRepository(engine)
        first = repo.apply(result(occurrence("u1", 20), occurrence("u2", 22)), NOW)
        assert (first.inserted, first.updated, first.unchanged) == (2, 0, 0)
        again = repo.apply(result(occurrence("u1", 20), occurrence("u2", 22)), NOW)
        assert (again.inserted, again.updated, again.unchanged) == (0, 0, 2)
        edited = repo.apply(
            result(occurrence("u1", 20, summary="Moved room"), occurrence("u2", 22)),
            NOW,
        )
        assert (edited.updated, edited.unchanged) == (1, 1)
        stored = repo.active(START, END)
        assert [e.summary for e in stored] == ["Moved room", "Lesson"]
        assert stored[0].start.tzinfo is UTC

    def test_complete_result_marks_missing_deleted(self, engine):
        repo = SourceEventRepository(engine)
        repo.apply(result(occurrence("u1", 20), occurrence("u2", 22)), NOW)
        later = NOW + timedelta(hours=1)
        summary = repo.apply(result(occurrence("u1", 20)), later)
        assert summary.deleted == 1
        assert [e.uid for e in repo.active(START, END)] == ["u1"]
        (gone,) = repo.deleted(START, END)
        assert gone.uid == "u2"

    def test_incomplete_result_never_deletes(self, engine):
        repo = SourceEventRepository(engine)
        repo.apply(result(occurrence("u1", 20), occurrence("u2", 22)), NOW)
        summary = repo.apply(result(complete=False), NOW)
        assert summary.deleted == 0
        assert len(repo.active(START, END)) == 2

    def test_reappearing_occurrence_is_restored(self, engine):
        repo = SourceEventRepository(engine)
        repo.apply(result(occurrence("u1", 20)), NOW)
        repo.apply(result(), NOW)
        assert repo.active(START, END) == ()
        summary = repo.apply(result(occurrence("u1", 20)), NOW)
        assert summary.updated == 1
        assert [e.uid for e in repo.active(START, END)] == ["u1"]

    def test_occurrences_outside_the_window_are_left_alone(self, engine):
        repo = SourceEventRepository(engine)
        early = replace(
            occurrence("old", 20),
            start=START - timedelta(days=3),
            end=START - timedelta(days=3, hours=-1),
        )
        repo.apply(result(early, occurrence("u1", 20)), NOW)
        summary = repo.apply(result(occurrence("u1", 20)), NOW)
        assert summary.deleted == 0
        assert [e.uid for e in repo.active(START - timedelta(days=5), END)] == [
            "old",
            "u1",
        ]

    def test_series_occurrences_are_distinct(self, engine):
        repo = SourceEventRepository(engine)
        a = occurrence("series", 20, rid="2026-10-20T23:00:00Z")
        b = occurrence("series", 27, rid="2026-10-27T23:00:00Z")
        assert repo.apply(result(a, b), NOW).inserted == 2

    def test_calendar_filter(self, engine):
        repo = SourceEventRepository(engine)
        repo.apply(result(occurrence("u1", 20)), NOW)
        assert repo.active(START, END, ["cal-other"]) == ()
        assert len(repo.active(START, END, ["cal-a"])) == 1
