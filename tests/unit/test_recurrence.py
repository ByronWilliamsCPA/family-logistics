"""Recurrence expansion of synthetic calendar objects."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import pytest
from caldav_fakes import fixture_text
from fl_factories import REGISTRY

from family_logistics.domain import recurrence
from family_logistics.domain.models import OccurrenceKind
from family_logistics.domain.parser import parse_trip
from family_logistics.domain.recurrence import (
    RecurrenceError,
    expand_object,
    occurrence_kind,
    recurrence_id_text,
)

ZONE = ZoneInfo("America/Los_Angeles")
SEEN = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
CALENDAR = "cal-synthetic"


def expand(name_or_ics: str, start: datetime, end: datetime, *, raw: bool = False):
    ics = name_or_ics if raw else fixture_text(name_or_ics)
    return expand_object(
        ics,
        calendar_id=CALENDAR,
        window_start=start,
        window_end=end,
        zone=ZONE,
        etag='"e1"',
        seen_at=SEEN,
    )


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


WIDE = (utc(2026, 10, 18), utc(2026, 12, 1))


@pytest.mark.unit
class TestLessonSeries:
    def test_exdate_removed_and_overrides_kept(self):
        events = expand("lesson_series.ics", *WIDE)
        assert [e.recurrence_id for e in events] == [
            "2026-10-20T23:00:00Z",
            "2026-11-04T00:00:00Z",
            "2026-11-11T00:00:00Z",
            "2026-11-18T00:00:00Z",
            "2026-11-25T00:00:00Z",
        ]

    def test_moved_occurrence_keeps_series_identity(self):
        moved = next(
            e for e in expand("lesson_series.ics", *WIDE) if "moved" in e.summary
        )
        assert moved.recurrence_id == "2026-11-04T00:00:00Z"
        assert moved.start == utc(2026, 11, 5, 1, 0)
        assert moved.is_override
        assert occurrence_kind(moved) is OccurrenceKind.OVERRIDE

    def test_cancelled_occurrence_is_kept_as_cancelled(self):
        cancelled = [
            e for e in expand("lesson_series.ics", *WIDE) if e.status == "cancelled"
        ]
        assert [e.recurrence_id for e in cancelled] == ["2026-11-11T00:00:00Z"]
        assert cancelled[0].is_override

    def test_dst_change_keeps_local_wall_time(self):
        events = expand("lesson_series.ics", *WIDE)
        local = [
            e.start.astimezone(ZONE).strftime("%H:%M")
            for e in events
            if not e.is_override
        ]
        assert local == ["16:00", "16:00", "16:00"]
        assert events[0].start == utc(2026, 10, 20, 23, 0)
        assert events[-1].start == utc(2026, 11, 25, 0, 0)

    def test_series_instances_are_classified(self):
        first = expand("lesson_series.ics", *WIDE)[0]
        assert first.series_uid_has_rrule
        assert not first.is_override
        assert occurrence_kind(first) is OccurrenceKind.SERIES_INSTANCE
        assert first.location == "Music Studio"
        assert first.etag == '"e1"'
        assert first.last_seen == SEEN

    def test_window_keeps_only_overlapping_occurrences(self):
        events = expand("lesson_series.ics", utc(2026, 11, 3), utc(2026, 11, 12))
        assert [e.summary for e in events] == [
            "Sam music lesson (moved)",
            "Sam music lesson",
        ]

    def test_hash_is_stable_and_content_sensitive(self):
        first = expand("lesson_series.ics", *WIDE)
        again = expand("lesson_series.ics", *WIDE)
        assert [e.raw_hash for e in first] == [e.raw_hash for e in again]
        edited = fixture_text("lesson_series.ics").replace(
            "SUMMARY:Sam music lesson (moved)", "SUMMARY:Sam music lesson (room 2)"
        )
        changed = expand(edited, *WIDE, raw=True)
        assert first[1].raw_hash != changed[1].raw_hash
        assert first[0].raw_hash == changed[0].raw_hash


@pytest.mark.unit
class TestOrphanOverrides:
    """Overrides without a master in the object (single-instance invitations)."""

    def test_each_override_keeps_its_own_recurrence_id(self):
        events = expand("orphan_overrides.ics", *WIDE)
        assert [(e.uid, e.recurrence_id) for e in events] == [
            ("synthetic-practice-alex", "2026-10-27T22:00:00Z"),
            ("synthetic-practice-alex", "2026-11-03T23:00:00Z"),
        ]
        assert len({e.to_key() for e in events}) == 2

    def test_orphan_overrides_are_classified_as_overrides(self):
        moved = expand("orphan_overrides.ics", *WIDE)[1]
        assert moved.summary == "Alex practice (moved)"
        assert moved.start == utc(2026, 11, 4, 0, 0)
        assert moved.is_override
        assert not moved.series_uid_has_rrule
        assert occurrence_kind(moved) is OccurrenceKind.OVERRIDE

    def test_single_event_key_ignores_start_time(self):
        (event,) = expand("ride_transport.ics", *WIDE)
        moved = fixture_text("ride_transport.ics").replace("T153000", "T143000")
        (rescheduled,) = expand(moved, *WIDE, raw=True)
        assert event.recurrence_id == ""
        assert rescheduled.recurrence_id == ""
        assert event.start - rescheduled.start == timedelta(hours=1)


@pytest.mark.unit
class TestSingleEvents:
    def test_all_day_event_spans_local_midnights(self):
        (event,) = expand("all_day.ics", *WIDE)
        assert event.all_day
        assert event.start == utc(2026, 10, 23, 7, 0)
        assert event.end == utc(2026, 10, 24, 7, 0)
        assert event.recurrence_id == ""
        assert occurrence_kind(event) is OccurrenceKind.SINGLE

    def test_floating_time_uses_configured_zone_and_duration(self):
        (event,) = expand("floating_time.ics", *WIDE)
        assert event.start == utc(2026, 10, 22, 22, 30)
        assert event.end - event.start == timedelta(minutes=90)
        assert not event.series_uid_has_rrule

    def test_transport_notes_reach_the_parser(self):
        (event,) = expand("ride_transport.ics", *WIDE)
        assert event.status == "confirmed"
        result = parse_trip(
            event.description or "",
            key=event.to_key(),
            start=event.start,
            end=event.end,
            registry=REGISTRY,
            occurrence=occurrence_kind(event),
        )
        assert result.trip is not None, [f.message for f in result.findings]
        assert result.trip.driver_id == "ext-driver-s"

    def test_missing_end_and_duration_defaults(self):
        ics = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//x//EN\nBEGIN:VEVENT\n"
            "UID:a\nDTSTAMP:20261001T000000Z\nDTSTART:20261022T090000Z\n"
            "SUMMARY:Point in time\nEND:VEVENT\nBEGIN:VEVENT\nUID:b\n"
            "DTSTAMP:20261001T000000Z\nDTSTART;VALUE=DATE:20261025\n"
            "SUMMARY:Whole day\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        point, day = expand(ics, *WIDE, raw=True)
        assert point.end == point.start
        assert day.end - day.start == timedelta(days=1)


@pytest.mark.unit
class TestFailures:
    def test_unparseable_object(self):
        with pytest.raises(RecurrenceError):
            expand("not a calendar", *WIDE, raw=True)

    def test_missing_dtstart(self):
        ics = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//x//EN\nBEGIN:VEVENT\n"
            "UID:a\nDTSTAMP:20261001T000000Z\nSUMMARY:No start\nEND:VEVENT\n"
            "END:VCALENDAR\n"
        )
        with pytest.raises(RecurrenceError):
            expand(ics, *WIDE, raw=True)


@pytest.mark.unit
class TestRecurrenceIdText:
    def test_date_and_floating_datetime(self):
        assert recurrence_id_text(date(2026, 10, 20), ZONE) == "2026-10-20"
        floating = datetime(2026, 10, 20, 16, 0)  # noqa: DTZ001 - floating on purpose
        assert recurrence_id_text(floating, ZONE) == "2026-10-20T23:00:00Z"


@pytest.mark.unit
class TestPropertyHelpers:
    """Defensive paths that recurring-ical-events does not normally reach."""

    def event(self, **props: object) -> icalendar.Event:
        event = icalendar.Event()
        for name, value in props.items():
            event.add(name, value)
        return event

    def test_end_from_duration_and_defaults(self):
        start = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
        with_duration = self.event(duration=timedelta(minutes=30))
        assert recurrence._end(with_duration, start) == start + timedelta(minutes=30)
        assert recurrence._end(self.event(), start) == start
        day = date(2026, 10, 25)
        assert recurrence._end(self.event(), day) == date(2026, 10, 26)

    def test_non_temporal_value_is_rejected(self):
        event = self.event(dtstart=datetime(2026, 10, 22, 9, 0, tzinfo=UTC))

        class Odd:
            dt = "soon"

        event["DTEND"] = Odd()
        with pytest.raises(RecurrenceError, match="DTEND is not a date"):
            recurrence._temporal(event, "DTEND")

    def test_missing_required_value(self):
        event = self.event()
        with pytest.raises(RecurrenceError, match="DTSTART is missing"):
            recurrence._required_temporal(event, "DTSTART")
