"""Contract types shared by the collector, registry and engine."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest

from family_logistics.core.exceptions import ValidationError
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
    to_occurrence_key,
)
from family_logistics.domain.models import SourceEventKey

START = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
NAIVE = START.replace(tzinfo=None)
BEFORE_START = START - timedelta(seconds=1)
RECURRENCE_ID = "2026-10-23T22:00:00+00:00"


def source_event(recurrence_id: str = "", **overrides: object) -> SourceEvent:
    values: dict[str, object] = {
        "calendar_id": "cal-transport",
        "uid": "uid-1",
        "recurrence_id": recurrence_id,
        "start": START,
        "end": START + timedelta(minutes=30),
        "all_day": False,
        "summary": "Ride | Morgan | Lesson pickup",
        "location": "Music Studio",
        "description": "FL_VERSION: 1",
        "status": "confirmed",
        "etag": '"etag-1"',
        "raw_hash": "0" * 64,
        "is_override": False,
        "series_uid_has_rrule": bool(recurrence_id),
        "last_seen": START,
    }
    values.update(overrides)
    return SourceEvent(**cast("Any", values))


def calendar_source(**overrides: object) -> CalendarSource:
    values: dict[str, object] = {
        "calendar_id": "cal-sam",
        "display_name": "Sam",
        "kind": CalendarKind.PERSON,
        "person_id": "sam",
    }
    values.update(overrides)
    return CalendarSource(**cast("Any", values))


def collection_result(**overrides: object) -> CollectionResult:
    values: dict[str, object] = {
        "source": calendar_source(),
        "events": (),
        "window_start": START - timedelta(days=2),
        "window_end": START + timedelta(days=14),
        "fetched_at": START,
        "complete": True,
        "error": None,
    }
    values.update(overrides)
    return CollectionResult(**cast("Any", values))


def transport_default(**overrides: object) -> TransportDefault:
    values: dict[str, object] = {
        "person_id": "alex",
        "weekdays": frozenset({0, 1, 2, 3, 4}),
        "direction": TransportDirection.TO_SCHOOL,
        "mode": TransportMode.BIKE,
        "origin_location_id": "home",
        "destination_location_id": "school-alex",
    }
    values.update(overrides)
    return TransportDefault(**cast("Any", values))


def activity(**overrides: object) -> Activity:
    values: dict[str, object] = {
        "activity_id": "a1",
        "participant_ids": ("sam",),
        "location_id": "studio",
        "location_text": "Music Studio",
        "start": START,
        "end": START + timedelta(hours=1),
        "source_key": ("cal-sam", "uid-2", ""),
    }
    values.update(overrides)
    return Activity(**cast("Any", values))


def school_day(**overrides: object) -> SchoolDay:
    values: dict[str, object] = {
        "person_id": "jordan",
        "date": date(2026, 10, 23),
        "location_id": "school-jordan",
        "start": START,
        "end": START + timedelta(hours=6),
        "kind": SchoolDayKind.NORMAL,
        "source": "district",
    }
    values.update(overrides)
    return SchoolDay(**cast("Any", values))


def assessment_input(**overrides: object) -> AssessmentInput:
    values: dict[str, object] = {
        "window_start": START,
        "window_end": START + timedelta(days=7),
    }
    values.update(overrides)
    return AssessmentInput(**cast("Any", values))


def assert_rejects(factory: Any, field_name: str, **overrides: object) -> None:
    """Assert the factory raises ValidationError naming exactly ``field_name``."""
    with pytest.raises(ValidationError) as caught:
        factory(**overrides)
    assert caught.value.details["field"] == field_name


@pytest.mark.unit
class TestSourceEvent:
    def test_single_event_key(self):
        event = source_event()
        assert event.occurrence_key == ("cal-transport", "uid-1", "")
        assert event.to_key() == SourceEventKey("cal-transport", "uid-1", None)

    def test_occurrence_key_carries_recurrence_id(self):
        event = source_event(RECURRENCE_ID)
        assert event.occurrence_key[2] == RECURRENCE_ID
        assert event.to_key().recurrence_id == RECURRENCE_ID

    @pytest.mark.parametrize("recurrence_id", ["", RECURRENCE_ID])
    def test_occurrence_key_round_trips_through_source_event_key(self, recurrence_id):
        event = source_event(recurrence_id)
        assert to_occurrence_key(event.to_key()) == event.occurrence_key

    def test_to_occurrence_key_maps_none_to_empty_string(self):
        assert to_occurrence_key(SourceEventKey("c", "u")) == ("c", "u", "")
        assert to_occurrence_key(SourceEventKey("c", "u", "r")) == ("c", "u", "r")

    def test_repr_omits_raw_text_and_identifiers(self):
        text = repr(source_event())
        for private in (
            "Lesson pickup",
            "Music Studio",
            "FL_VERSION",
            "uid-1",
            "cal-transport",
            "etag-1",
        ):
            assert private not in text
        assert text.startswith("SourceEvent(")
        assert "status='confirmed'" in text
        assert "raw_hash='" in text

    def test_is_immutable(self):
        event = cast("Any", source_event())
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.summary = "changed"

    @pytest.mark.parametrize(
        ("overrides", "field_name"),
        [
            ({"calendar_id": ""}, "calendar_id"),
            ({"uid": ""}, "uid"),
            ({"uid": "   "}, "uid"),
            ({"start": NAIVE}, "start"),
            ({"end": NAIVE}, "end"),
            ({"last_seen": NAIVE}, "last_seen"),
            ({"end": BEFORE_START}, "end"),
            ({"status": "deleted"}, "status"),
        ],
    )
    def test_rejects_invalid_values(self, overrides, field_name):
        assert_rejects(source_event, field_name, **overrides)

    def test_error_names_the_field_without_the_value(self):
        with pytest.raises(ValidationError) as caught:
            source_event(status="leaky-status")
        assert "leaky-status" not in str(caught.value)
        assert "value" not in caught.value.details

    @pytest.mark.parametrize("status", ["confirmed", "tentative", "cancelled"])
    def test_accepts_every_status(self, status):
        assert source_event(status=status).status == status

    def test_accepts_all_day_event(self):
        day = datetime(2026, 10, 24, tzinfo=UTC)
        event = source_event(all_day=True, start=day, end=day + timedelta(days=1))
        assert event.all_day is True
        assert event.end - event.start == timedelta(days=1)

    def test_accepts_zero_length_event(self):
        event = source_event(all_day=True, start=START, end=START)
        assert event.start == event.end


@pytest.mark.unit
class TestCollectionResult:
    @pytest.mark.parametrize(
        ("complete", "error", "valid"),
        [
            (True, None, True),
            (False, "timeout", True),
            (True, "timeout", False),
            (False, None, False),
        ],
    )
    def test_complete_must_match_error(self, complete, error, valid):
        if valid:
            result = collection_result(complete=complete, error=error)
            assert result.complete == (result.error is None)
        else:
            assert_rejects(
                collection_result, "complete", complete=complete, error=error
            )

    def test_list_of_events_is_converted_to_tuple(self):
        event = source_event()
        result = collection_result(events=[event])
        assert isinstance(result.events, tuple)
        assert result.events == (event,)

    def test_tuple_of_events_is_kept(self):
        events = (source_event(), source_event(RECURRENCE_ID))
        assert collection_result(events=events).events == events

    @pytest.mark.parametrize("events", ["abc", None, frozenset(), iter(())])
    def test_rejects_other_event_containers(self, events):
        assert_rejects(collection_result, "events", events=events)

    @pytest.mark.parametrize(
        ("overrides", "field_name"),
        [
            ({"window_start": NAIVE}, "window_start"),
            ({"window_end": NAIVE}, "window_end"),
            ({"fetched_at": NAIVE}, "fetched_at"),
            ({"window_end": START - timedelta(days=3)}, "window_end"),
        ],
    )
    def test_rejects_invalid_window_values(self, overrides, field_name):
        assert_rejects(collection_result, field_name, **overrides)

    def test_accepts_empty_window(self):
        result = collection_result(window_start=START, window_end=START)
        assert result.window_start == result.window_end


@pytest.mark.unit
class TestCalendarSource:
    def test_source_defaults(self):
        source = CalendarSource(
            "cal-t", "Family Transportation", CalendarKind.TRANSPORT
        )
        assert source.enabled is True
        assert source.person_id is None
        assert source.last_success is None
        assert source.last_error is None

    def test_repr_omits_calendar_id(self):
        text = repr(calendar_source(calendar_id="cal-private"))
        assert "cal-private" not in text
        assert "kind=" in text

    def test_wire_value_is_converted_to_enum(self):
        assert calendar_source(kind="school_bell").kind is CalendarKind.SCHOOL_BELL

    @pytest.mark.parametrize(
        ("overrides", "field_name"),
        [
            ({"calendar_id": ""}, "calendar_id"),
            ({"person_id": ""}, "person_id"),
            ({"last_success": NAIVE}, "last_success"),
            ({"kind": "calendar"}, "kind"),
        ],
    )
    def test_rejects_invalid_values(self, overrides, field_name):
        assert_rejects(calendar_source, field_name, **overrides)

    def test_accepts_no_person_and_aware_last_success(self):
        source = calendar_source(person_id=None, last_success=START)
        assert source.person_id is None
        assert source.last_success == START


@pytest.mark.unit
class TestTransportDefault:
    def test_applies_on_weekdays_only(self):
        rule = transport_default()
        assert rule.applies_on(date(2026, 10, 23)) is True  # Friday
        assert rule.applies_on(date(2026, 10, 24)) is False  # Saturday

    def test_effective_window(self):
        rule = transport_default(
            effective_from=date(2026, 10, 20), effective_to=date(2026, 10, 22)
        )
        assert rule.applies_on(date(2026, 10, 19)) is False
        assert rule.applies_on(date(2026, 10, 20)) is True
        assert rule.applies_on(date(2026, 10, 21)) is True
        assert rule.applies_on(date(2026, 10, 22)) is True
        assert rule.applies_on(date(2026, 10, 23)) is False

    def test_single_day_window(self):
        day = date(2026, 10, 22)  # Thursday
        rule = transport_default(effective_from=day, effective_to=day)
        assert rule.applies_on(day - timedelta(days=1)) is False
        assert rule.applies_on(day) is True
        assert rule.applies_on(day + timedelta(days=1)) is False

    def test_sunday_is_weekday_six(self):
        rule = transport_default(weekdays=frozenset({6}))
        assert rule.applies_on(date(2026, 10, 25)) is True  # Sunday
        assert rule.applies_on(date(2026, 10, 26)) is False  # Monday

    def test_monday_is_weekday_zero(self):
        rule = transport_default(weekdays=frozenset({0}))
        assert rule.applies_on(date(2026, 10, 26)) is True  # Monday
        assert rule.applies_on(date(2026, 10, 25)) is False  # Sunday

    def test_no_weekdays_never_applies(self):
        rule = transport_default(weekdays=frozenset())
        assert rule.applies_on(date(2026, 10, 23)) is False

    @pytest.mark.parametrize(
        ("weekdays", "valid"),
        [
            (frozenset({-1}), False),
            (frozenset({0}), True),
            (frozenset({6}), True),
            (frozenset({7}), False),
            (frozenset({1, 2, 3, 4, 5, 6, 7}), False),
            (frozenset({0, 1, 2, 3, 4, 5, 6}), True),
        ],
    )
    def test_weekday_range(self, weekdays, valid):
        if valid:
            assert transport_default(weekdays=weekdays).weekdays == weekdays
        else:
            assert_rejects(transport_default, "weekdays", weekdays=weekdays)

    @pytest.mark.parametrize(
        ("overrides", "field_name"),
        [
            ({"person_id": ""}, "person_id"),
            ({"origin_location_id": ""}, "origin_location_id"),
            ({"destination_location_id": ""}, "destination_location_id"),
            ({"driver_id": ""}, "driver_id"),
            ({"direction": "sideways"}, "direction"),
            ({"mode": "scooter"}, "mode"),
            (
                {
                    "effective_from": date(2026, 10, 22),
                    "effective_to": date(2026, 10, 20),
                },
                "effective_to",
            ),
        ],
    )
    def test_rejects_invalid_values(self, overrides, field_name):
        assert_rejects(transport_default, field_name, **overrides)

    def test_accepts_no_driver_and_open_window(self):
        rule = transport_default(driver_id=None)
        assert rule.driver_id is None
        assert rule.effective_from is None
        assert rule.effective_to is None

    def test_wire_values_are_converted_to_enums(self):
        rule = transport_default(direction="from_school", mode="carpool")
        assert rule.direction is TransportDirection.FROM_SCHOOL
        assert rule.mode is TransportMode.CARPOOL


@pytest.mark.unit
class TestActivity:
    def test_defaults_and_redaction(self):
        item = activity()
        assert item.classification is ActivityClassification.OTHER
        assert item.stays_until_end is True
        text = repr(item)
        for private in ("Music Studio", "cal-sam", "uid-2"):
            assert private not in text
        nested = repr(assessment_input(activities=(item,)))
        for private in ("Music Studio", "cal-sam", "uid-2"):
            assert private not in nested

    def test_list_of_participants_is_converted_to_tuple(self):
        item = activity(participant_ids=["sam", "alex"])
        assert item.participant_ids == ("sam", "alex")

    def test_wire_value_is_converted_to_enum(self):
        assert activity(classification="sport").classification is (
            ActivityClassification.SPORT
        )

    @pytest.mark.parametrize(
        ("overrides", "field_name"),
        [
            ({"activity_id": ""}, "activity_id"),
            ({"location_id": ""}, "location_id"),
            ({"source_key": ("", "uid-2", "")}, "source_key.calendar_id"),
            ({"source_key": ("cal-sam", "", "")}, "source_key.uid"),
            ({"start": NAIVE}, "start"),
            ({"end": NAIVE}, "end"),
            ({"end": BEFORE_START}, "end"),
            ({"participant_ids": "sam"}, "participant_ids"),
            ({"classification": "dance"}, "classification"),
        ],
    )
    def test_rejects_invalid_values(self, overrides, field_name):
        assert_rejects(activity, field_name, **overrides)

    def test_accepts_unresolved_location_and_zero_length(self):
        item = activity(location_id=None, start=START, end=START)
        assert item.location_id is None
        assert item.start == item.end


@pytest.mark.unit
class TestSchoolDay:
    def test_no_school_keeps_start_equal_to_end(self):
        day = school_day(kind=SchoolDayKind.NO_SCHOOL, end=START)
        assert day.kind is SchoolDayKind.NO_SCHOOL
        assert day.start == day.end

    def test_wire_value_is_converted_to_enum(self):
        assert school_day(kind="early_release").kind is SchoolDayKind.EARLY_RELEASE

    @pytest.mark.parametrize(
        ("overrides", "field_name"),
        [
            ({"person_id": ""}, "person_id"),
            ({"location_id": ""}, "location_id"),
            ({"start": NAIVE}, "start"),
            ({"end": NAIVE}, "end"),
            ({"end": BEFORE_START}, "end"),
            ({"kind": "holiday"}, "kind"),
        ],
    )
    def test_rejects_invalid_values(self, overrides, field_name):
        assert_rejects(school_day, field_name, **overrides)


@pytest.mark.unit
class TestAssessmentInput:
    def test_freshness_lookup(self):
        fresh = START - timedelta(minutes=10)
        data = assessment_input(
            source_freshness=(("cal-transport", fresh), ("cal-sam", None))
        )
        assert data.freshness("cal-transport") == fresh
        assert data.freshness("cal-sam") is None
        assert data.freshness("unknown") is None
        assert data.timezone == "America/Los_Angeles"
        assert hash(data) == hash(dataclasses.replace(data))

    def test_repr_omits_source_ids(self):
        data = assessment_input(source_freshness=(("cal-transport", START),))
        assert "cal-transport" not in repr(data)

    @pytest.mark.parametrize(
        ("overrides", "field_name"),
        [
            ({"window_start": NAIVE}, "window_start"),
            ({"window_end": NAIVE}, "window_end"),
            ({"window_end": BEFORE_START}, "window_end"),
            ({"source_freshness": (("cal-sam", NAIVE),)}, "source_freshness"),
            ({"timezone": "Mars/Olympus"}, "timezone"),
            ({"timezone": "not a zone"}, "timezone"),
            ({"timezone": ""}, "timezone"),
        ],
    )
    def test_rejects_invalid_values(self, overrides, field_name):
        assert_rejects(assessment_input, field_name, **overrides)

    def test_timezone_error_does_not_echo_the_value(self):
        with pytest.raises(ValidationError) as caught:
            assessment_input(timezone="Mars/Olympus")
        assert "Mars/Olympus" not in str(caught.value)
        assert "value" not in caught.value.details

    @pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Europe/London"])
    def test_accepts_iana_timezones(self, zone):
        assert assessment_input(timezone=zone).timezone == zone

    def test_accepts_empty_window_and_unknown_freshness(self):
        data = assessment_input(window_end=START, source_freshness=(("cal-sam", None),))
        assert data.window_start == data.window_end
        assert data.freshness("cal-sam") is None


@pytest.mark.unit
class TestImmutability:
    @pytest.mark.parametrize(
        ("builder", "attribute", "value"),
        [
            (collection_result, "complete", False),
            (calendar_source, "enabled", False),
            (transport_default, "mode", TransportMode.BUS),
            (activity, "stays_until_end", False),
            (school_day, "kind", SchoolDayKind.UNKNOWN),
            (assessment_input, "timezone", "UTC"),
        ],
    )
    def test_contract_types_are_frozen(self, builder, attribute, value):
        instance = builder()
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, attribute, value)


@pytest.mark.unit
class TestEnumWireValues:
    @pytest.mark.parametrize(
        ("enum_type", "wire_values"),
        [
            (
                CalendarKind,
                ["transport", "person", "school_district", "school_bell"],
            ),
            (ActivityClassification, ["school", "sport", "lesson", "work", "other"]),
            (TransportDirection, ["to_school", "from_school"]),
            (TransportMode, ["bike", "bus", "walk", "parent", "carpool"]),
            (
                SchoolDayKind,
                ["normal", "early_release", "late_start", "no_school", "unknown"],
            ),
        ],
    )
    def test_wire_values_are_stable_and_round_trip(self, enum_type, wire_values):
        assert [member.value for member in enum_type] == wire_values
        for member in enum_type:
            assert enum_type(member.value) is member
