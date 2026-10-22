"""Contract types shared by the collector, registry and engine."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from family_logistics.domain.events import (
    Activity,
    ActivityClassification,
    AssessmentInput,
    CalendarKind,
    CalendarSource,
    CollectionResult,
    SchoolDay,
    SchoolDayKind,
    SourceEvent,
    TransportDefault,
    TransportDirection,
    TransportMode,
)
from family_logistics.domain.models import SourceEventKey

START = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


def source_event(recurrence_id: str = "", *, is_override: bool = False) -> SourceEvent:
    return SourceEvent(
        calendar_id="cal-transport",
        uid="uid-1",
        recurrence_id=recurrence_id,
        start=START,
        end=START + timedelta(minutes=30),
        all_day=False,
        summary="Ride | Morgan | Lesson pickup",
        location="Music Studio",
        description="FL_VERSION: 1",
        status="confirmed",
        etag='"etag-1"',
        raw_hash="0" * 64,
        is_override=is_override,
        series_uid_has_rrule=bool(recurrence_id),
        last_seen=START,
    )


@pytest.mark.unit
class TestSourceEvent:
    def test_single_event_key(self):
        event = source_event()
        assert event.occurrence_key == ("cal-transport", "uid-1", "")
        assert event.to_key() == SourceEventKey("cal-transport", "uid-1", None)

    def test_occurrence_key_carries_recurrence_id(self):
        event = source_event("2026-10-23T22:00:00+00:00")
        assert event.occurrence_key[2] == "2026-10-23T22:00:00+00:00"
        assert event.to_key().recurrence_id == "2026-10-23T22:00:00+00:00"
        assert event.is_override is False

    def test_override_event_sets_override_status(self):
        event = source_event("2026-10-23T22:00:00+00:00", is_override=True)
        assert event.is_override is True

    def test_repr_omits_raw_calendar_text(self):
        text = repr(source_event())
        assert "Lesson pickup" not in text
        assert "Music Studio" not in text
        assert "FL_VERSION" not in text
        assert "uid-1" in text

    def test_is_immutable(self):
        event = source_event()
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.summary = "changed"  # type: ignore[misc]


@pytest.mark.unit
class TestCollectionAndSources:
    def test_incomplete_result_keeps_error(self):
        source = CalendarSource("cal-sam", "Sam", CalendarKind.PERSON, person_id="sam")
        result = CollectionResult(
            source=source,
            events=(),
            window_start=START - timedelta(days=2),
            window_end=START + timedelta(days=14),
            fetched_at=START,
            complete=False,
            error="timeout",
        )
        assert result.complete is False
        assert result.error == "timeout"
        assert result.source.person_id == "sam"

    def test_source_defaults(self):
        source = CalendarSource(
            "cal-t", "Family Transportation", CalendarKind.TRANSPORT
        )
        assert source.enabled is True
        assert source.person_id is None
        assert source.last_success is None


@pytest.mark.unit
class TestTransportDefault:
    def default(self, **overrides) -> TransportDefault:
        values = {
            "person_id": "alex",
            "weekdays": frozenset({0, 1, 2, 3, 4}),
            "direction": TransportDirection.TO_SCHOOL,
            "mode": TransportMode.BIKE,
            "origin_location_id": "home",
            "destination_location_id": "school-alex",
        }
        values.update(overrides)
        return TransportDefault(**values)  # type: ignore[arg-type]

    def test_applies_on_weekdays_only(self):
        rule = self.default()
        assert rule.applies_on(date(2026, 10, 23)) is True  # Friday
        assert rule.applies_on(date(2026, 10, 24)) is False  # Saturday

    def test_effective_window(self):
        rule = self.default(
            effective_from=date(2026, 10, 20), effective_to=date(2026, 10, 22)
        )
        assert rule.applies_on(date(2026, 10, 19)) is False
        assert rule.applies_on(date(2026, 10, 20)) is True
        assert rule.applies_on(date(2026, 10, 21)) is True
        assert rule.applies_on(date(2026, 10, 22)) is True
        assert rule.applies_on(date(2026, 10, 23)) is False


@pytest.mark.unit
class TestEngineInputs:
    def test_activity_defaults(self):
        activity = Activity(
            activity_id="a1",
            participant_ids=("sam",),
            location_id="studio",
            location_text="Music Studio",
            start=START,
            end=START + timedelta(hours=1),
            source_key=("cal-sam", "uid-2", ""),
        )
        assert activity.classification is ActivityClassification.OTHER
        assert activity.stays_until_end is True
        assert "Music Studio" not in repr(activity)
        nested = AssessmentInput(
            window_start=START, window_end=START, activities=(activity,)
        )
        assert "Music Studio" not in repr(nested)

    def test_school_day(self):
        day = SchoolDay(
            person_id="jordan",
            date=date(2026, 10, 23),
            location_id="school-jordan",
            start=START,
            end=START,
            kind=SchoolDayKind.NO_SCHOOL,
            source="district",
        )
        assert day.kind is SchoolDayKind.NO_SCHOOL
        local = ZoneInfo("America/Los_Angeles")
        assert day.start.astimezone(local).date() == day.date

    def test_assessment_input_freshness_lookup(self):
        fresh = START - timedelta(minutes=10)
        data = AssessmentInput(
            window_start=START,
            window_end=START + timedelta(days=7),
            source_freshness=(("cal-transport", fresh), ("cal-sam", None)),
        )
        assert data.freshness("cal-transport") == fresh
        assert data.freshness("cal-sam") is None
        assert data.freshness("unknown") is None
        assert data.timezone == "America/Los_Angeles"
        assert hash(data) == hash(dataclasses.replace(data))
