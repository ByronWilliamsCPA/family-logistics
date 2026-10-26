"""Home Assistant snapshot payload schema: strict, bounded, no extra keys."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from family_logistics.adapters.home_assistant.schemas import (
    MAX_CALENDARS,
    MAX_DESCRIPTION_LENGTH,
    MAX_EVENTS_PER_CALENDAR,
    MAX_LOCATION_LENGTH,
    MAX_REPORTED_ERRORS,
    MAX_SNAPSHOT_BYTES,
    MAX_SUMMARY_LENGTH,
    MAX_TOTAL_EVENTS,
    MAX_ZONES,
    HaCalendar,
    HaCalendarEvent,
    HaSnapshotPayload,
    safe_error_locations,
)
from family_logistics.core.config import (
    MAX_HA_SCHOOLS,
    MAX_SCHOOL_CALENDARS,
    Settings,
)

if TYPE_CHECKING:
    from collections.abc import MutableMapping

pytestmark = pytest.mark.unit

PACIFIC_DST = timezone(timedelta(hours=-7))
FETCHED = "2026-10-23T06:00:00-07:00"


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "fetched_at": FETCHED,
        "calendars": {
            "calendar.middle_school_bell": {
                "events": [
                    {
                        "start": "2026-10-23T08:00:00-07:00",
                        "end": "2026-10-23T15:00:00-07:00",
                        "summary": "Regular Day",
                    }
                ]
            },
            "calendar.district": {
                "events": [
                    {
                        "start": "2026-10-26",
                        "end": "2026-10-27",
                        "summary": "No School",
                        "description": "Staff development",
                        "location": "All schools",
                    }
                ]
            },
        },
        "zones": ["zone.home", "zone.middle_school"],
    }
    return base | overrides


def parse(data: dict[str, object]) -> HaSnapshotPayload:
    return HaSnapshotPayload.model_validate_json(json.dumps(data))


def rejected_at(data: dict[str, object]) -> list[dict[str, object]]:
    """Validate ``data`` as JSON text; return the redacted error locations.

    The body is built before the ``raises`` block so that only the validation
    call itself can satisfy it.
    """
    body = json.dumps(data)
    with pytest.raises(ValidationError) as info:
        HaSnapshotPayload.model_validate_json(body)
    return safe_error_locations(info.value)


def error(loc: list[object], kind: str) -> list[dict[str, object]]:
    return [{"loc": loc, "type": kind}]


def event_error(field: str | None, kind: str) -> list[dict[str, object]]:
    loc: list[object] = ["calendars", "*", "events", 0]
    return error(loc if field is None else [*loc, field], kind)


def event(**fields: object) -> dict[str, object]:
    return {"start": "2026-10-23", "end": "2026-10-24", "summary": "x"} | fields


def with_events(*events: dict[str, object]) -> dict[str, object]:
    return payload(calendars={"calendar.district": {"events": list(events)}})


class TestAccepted:
    def test_a_get_events_payload_parses(self):
        snapshot = parse(payload())
        assert snapshot.fetched_at == datetime(2026, 10, 23, 13, 0, tzinfo=UTC)
        bell = snapshot.calendars["calendar.middle_school_bell"].events[0]
        assert bell.start == datetime(2026, 10, 23, 8, 0, tzinfo=PACIFIC_DST)
        assert not bell.all_day
        closure = snapshot.calendars["calendar.district"].events[0]
        assert closure.start == date(2026, 10, 26)
        assert closure.all_day
        assert snapshot.zones == ("zone.home", "zone.middle_school")

    def test_zones_and_optional_fields_may_be_absent(self):
        data = payload()
        del data["zones"]
        snapshot = parse(data)
        assert snapshot.zones == ()

    def test_an_empty_calendar_is_allowed(self):
        snapshot = parse(with_events())
        assert snapshot.calendars["calendar.district"].events == ()

    def test_a_timed_event_may_be_instantaneous(self):
        moment = "2026-10-23T12:30:00-07:00"
        snapshot = parse(with_events(event(start=moment, end=moment)))
        item = snapshot.calendars["calendar.district"].events[0]
        assert item.start == item.end


class TestRejected:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (payload(extra=1), error(["*"], "extra_forbidden")),
            (
                payload(fetched_at="2026-10-23T06:00:00"),
                error(["fetched_at"], "value_error"),
            ),
            (payload(fetched_at=1_761_224_400), error(["fetched_at"], "value_error")),
            (payload(fetched_at="yesterday"), error(["fetched_at"], "value_error")),
            (
                payload(zones=["person.sam"]),
                error(["zones", 0], "string_pattern_mismatch"),
            ),
            (
                payload(zones=["device_tracker.phone"]),
                error(["zones", 0], "string_pattern_mismatch"),
            ),
            (payload(zones="zone.home"), error(["zones"], "tuple_type")),
            (
                payload(calendars={"person.sam": {"events": []}}),
                error(["calendars", "*", "*"], "string_pattern_mismatch"),
            ),
            (
                payload(calendars={"sensor.location": {"events": []}}),
                error(["calendars", "*", "*"], "string_pattern_mismatch"),
            ),
            (
                payload(calendars={"calendar.district": {"events": [], "extra": 1}}),
                error(["calendars", "*", "*"], "extra_forbidden"),
            ),
            (
                payload(calendars={"calendar.district": []}),
                error(["calendars", "*"], "model_type"),
            ),
            (payload(calendars={}), error(["calendars"], "too_short")),
        ],
        ids=[
            "unknown-top-level-key",
            "naive-fetched-at",
            "numeric-fetched-at",
            "unparseable-fetched-at",
            "person-entity-as-zone",
            "device-tracker-as-zone",
            "zones-not-a-list",
            "person-entity-as-calendar",
            "sensor-entity-as-calendar",
            "unknown-calendar-key",
            "events-not-wrapped",
            "no-calendar",
        ],
    )
    def test_bad_payload_is_rejected(self, data, expected):
        assert rejected_at(data) == expected

    @pytest.mark.parametrize(
        ("item", "expected"),
        [
            (event(uid="abc"), event_error("*", "extra_forbidden")),
            (event(summary=None), event_error("summary", "string_type")),
            (event(summary=5), event_error("summary", "string_type")),
            (
                event(start="2026-10-23T08:00:00", end="2026-10-23T09:00:00"),
                event_error("start", "value_error") + event_error("end", "value_error"),
            ),
            (
                event(start="2026-10-23", end="2026-10-23T09:00:00-07:00"),
                event_error(None, "value_error"),
            ),
            (
                event(start="2026-10-24", end="2026-10-23"),
                event_error(None, "value_error"),
            ),
            (
                event(start="2026-10-23", end="2026-10-23"),
                event_error(None, "value_error"),
            ),
            (
                event(
                    start="2026-10-23T09:00:00-07:00",
                    end="2026-10-23T08:00:00-07:00",
                ),
                event_error(None, "value_error"),
            ),
            (event(start="23/10/2026"), event_error("start", "value_error")),
            (
                event(start="2026-02-30", end="2026-03-01"),
                event_error("start", "value_error"),
            ),
            (
                event(start="2026-10-23T08:00:00-07:00" + " " * 40),
                event_error("start", "value_error"),
            ),
            (
                event(summary="x" * (MAX_SUMMARY_LENGTH + 1)),
                event_error("summary", "string_too_long"),
            ),
            (
                event(description="x" * (MAX_DESCRIPTION_LENGTH + 1)),
                event_error("description", "value_error"),
            ),
            (
                event(location="x" * (MAX_LOCATION_LENGTH + 1)),
                event_error("location", "value_error"),
            ),
            (event(location=5), event_error("location", "value_error")),
        ],
        ids=[
            "unknown-event-key",
            "null-summary",
            "numeric-summary",
            "naive-times",
            "mixed-all-day-and-timed",
            "all-day-end-before-start",
            "all-day-zero-length",
            "timed-end-before-start",
            "not-iso",
            "impossible-date",
            "trailing-spaces",
            "summary-too-long",
            "description-too-long",
            "location-too-long",
            "numeric-location",
        ],
    )
    def test_bad_event_is_rejected(self, item, expected):
        assert rejected_at(with_events(item)) == expected

    def test_too_many_calendars(self):
        calendars = {
            f"calendar.school_{i}": {"events": []} for i in range(MAX_CALENDARS + 1)
        }
        assert rejected_at(payload(calendars=calendars)) == error(
            ["calendars"], "too_long"
        )

    def test_too_many_events(self):
        data = with_events(*[event()] * (MAX_EVENTS_PER_CALENDAR + 1))
        assert rejected_at(data) == error(["calendars", "*", "events"], "too_long")

    def test_too_many_events_across_calendars(self):
        per_calendar = MAX_EVENTS_PER_CALENDAR
        needed = MAX_TOTAL_EVENTS // per_calendar + 1
        calendars = {
            f"calendar.school_{i}": {"events": [event()] * per_calendar}
            for i in range(needed)
        }
        assert needed * per_calendar > MAX_TOTAL_EVENTS
        assert rejected_at(payload(calendars=calendars)) == error([], "value_error")

    def test_the_total_event_cap_is_accepted_exactly(self):
        per_calendar = MAX_EVENTS_PER_CALENDAR
        calendars = {
            f"calendar.school_{i}": {"events": [event()] * per_calendar}
            for i in range(MAX_TOTAL_EVENTS // per_calendar)
        }
        snapshot = parse(payload(calendars=calendars))
        total = sum(len(item.events) for item in snapshot.calendars.values())
        assert total == MAX_TOTAL_EVENTS

    def test_the_total_event_cap_never_refuses_what_the_byte_limit_allows(self):
        smallest = json.dumps(
            {"start": "2000-01-01", "end": "2000-01-02", "summary": ""},
            separators=(",", ":"),
        )
        assert (len(smallest) + 1) * MAX_TOTAL_EVENTS > MAX_SNAPSHOT_BYTES

    def test_too_many_zones(self):
        zones = [f"zone.z{i}" for i in range(MAX_ZONES + 1)]
        assert rejected_at(payload(zones=zones)) == error(["zones"], "too_long")

    @pytest.mark.parametrize(
        "zone",
        [
            "zone.Home",
            "zone.a b",
            "zone." + "a" * 201,
            "zone.",
            "zone.x\n",
            "zone.a-b",
            "Zone.home",
            "zone.home.extra",
        ],
        ids=[
            "upper-case",
            "space",
            "over-200-characters",
            "empty-name",
            "trailing-newline",
            "hyphen",
            "upper-case-domain",
            "dotted-name",
        ],
    )
    def test_malformed_zone_is_rejected(self, zone):
        assert rejected_at(payload(zones=[zone])) == error(
            ["zones", 0], "string_pattern_mismatch"
        )

    def test_the_longest_zone_name_is_accepted(self):
        zone = "zone." + "a" * 200
        assert parse(payload(zones=[zone])).zones == (zone,)

    def test_a_digit_outside_ascii_is_rejected_without_echoing_it(self):
        fetched = "2025-10-29T\u0661\u0662:30:00+00:00"
        body = json.dumps(payload(fetched_at=fetched))
        with pytest.raises(ValidationError) as info:
            HaSnapshotPayload.model_validate_json(body)
        message = info.value.errors(include_url=False)[0]["msg"]
        assert (
            message == "Value error, expected an ISO 8601 date-time with a UTC offset"
        )
        assert "\u0661" not in str(info.value)
        assert safe_error_locations(info.value) == error(["fetched_at"], "value_error")

    @pytest.mark.parametrize(
        "fetched", ["2026-13-45T10:00:00+00:00", "2026-10-23T25:61:00+00:00"]
    )
    def test_an_impossible_fetched_at_is_rejected_without_echoing_it(self, fetched):
        body = json.dumps(payload(fetched_at=fetched))
        with pytest.raises(ValidationError) as info:
            HaSnapshotPayload.model_validate_json(body)
        message = info.value.errors(include_url=False)[0]["msg"]
        assert (
            message == "Value error, expected an ISO 8601 date-time with a UTC offset"
        )
        assert fetched not in str(info.value)

    def test_a_non_ascii_digit_event_date_is_rejected(self):
        item = event(start="2026-10-\u0662\u0663", end="2026-10-24")
        assert rejected_at(with_events(item)) == event_error("start", "value_error")


class TestPlausibleYears:
    @pytest.mark.parametrize(
        "fetched",
        [
            "1999-12-31T23:59:59+00:00",
            "2101-01-01T00:00:00Z",
            "0001-01-01T00:00:00Z",
            "9999-12-31T23:59:59Z",
        ],
    )
    def test_fetched_at_outside_2000_to_2100_is_refused(self, fetched):
        body = json.dumps(payload(fetched_at=fetched))
        with pytest.raises(ValidationError) as info:
            HaSnapshotPayload.model_validate_json(body)
        assert fetched not in str(info.value)
        assert safe_error_locations(info.value) == error(["fetched_at"], "value_error")

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            ("0001-01-01", "9999-12-31"),
            ("1999-12-31", "2000-01-02"),
            ("2100-12-31", "2101-01-01"),
            ("0001-01-01T00:00:00+05:00", "2026-10-23T00:00:00Z"),
            ("2026-10-23T00:00:00Z", "9999-12-31T23:59:59+00:00"),
        ],
    )
    def test_event_times_outside_2000_to_2100_are_refused(self, start, end):
        body = json.dumps(with_events(event(start=start, end=end)))
        with pytest.raises(ValidationError) as info:
            HaSnapshotPayload.model_validate_json(body)
        assert start not in str(info.value)
        assert end not in str(info.value)
        locations = safe_error_locations(info.value)
        assert locations
        assert all(
            item["type"] == "value_error"
            and item["loc"][:4] == ["calendars", "*", "events", 0]
            and item["loc"][4] in {"start", "end"}
            for item in locations
        )

    def test_the_years_2000_and_2100_are_accepted(self):
        snapshot = parse(
            with_events(
                event(start="2000-01-01", end="2000-01-02"),
                event(start="2100-12-30", end="2100-12-31"),
            )
            | {"fetched_at": "2000-01-01T00:00:00Z"}
        )
        assert snapshot.fetched_at.year == 2000


class TestSafeErrorLocations:
    def test_locations_never_carry_payload_text(self):
        data = payload(
            calendars={
                "calendar.private_name_here": {
                    "events": [event(start="Private Summary Text")]
                }
            },
            zones=["zone.Private_Zone"],
        )
        locations = rejected_at(data)
        text = json.dumps(locations)
        assert "private" not in text.lower()
        assert {
            "loc": ["calendars", "*", "events", 0, "start"],
            "type": "value_error",
        } in locations
        assert all(set(item) == {"loc", "type"} for item in locations)

    def test_a_malformed_zone_is_reported_without_its_text(self):
        locations = rejected_at(payload(zones=["zone.Private_Zone"]))
        assert locations == error(["zones", 0], "string_pattern_mismatch")

    def test_a_rejected_calendar_key_is_redacted(self):
        data = payload(calendars={"person.sam_private": {"events": []}})
        locations = rejected_at(data)
        assert locations == error(["calendars", "*", "*"], "string_pattern_mismatch")
        assert "sam_private" not in json.dumps(locations)

    @pytest.mark.parametrize("name", ["events", "start", "summary", "calendars"])
    def test_a_rejected_key_equal_to_a_field_name_is_redacted(self, name):
        locations = rejected_at(payload(calendars={name: {"events": []}}))
        assert locations == error(["calendars", "*", "*"], "string_pattern_mismatch")

    def test_an_unknown_key_is_redacted(self):
        locations = rejected_at(payload(sam_private_note=1))
        assert locations == error(["*"], "extra_forbidden")

    def test_locations_are_capped(self):
        zones = ["bad"] * MAX_ZONES
        assert MAX_ZONES > MAX_REPORTED_ERRORS
        locations = rejected_at(payload(zones=zones))
        assert len(locations) == MAX_REPORTED_ERRORS


class TestEventModel:
    def test_event_is_immutable(self):
        item = HaCalendarEvent.model_validate_json(json.dumps(event()))
        with pytest.raises(ValidationError) as info:
            item.summary = "changed"
        assert info.value.errors(include_url=False)[0]["type"] == "frozen_instance"

    def test_description_and_location_are_checked_then_discarded(self):
        item = HaCalendarEvent.model_validate_json(
            json.dumps(event(description="Staff day", location="Room 4"))
        )
        assert item.description is None
        assert item.location is None
        assert "Staff day" not in repr(item)
        assert "Room 4" not in repr(item)


class TestValidationPaths:
    """Both entry points validate alike; only the JSON list to tuple is relaxed."""

    def test_the_dict_json_loads_returns_validates(self):
        data = json.loads(json.dumps(payload()))
        assert HaSnapshotPayload.model_validate(data) == parse(payload())

    def test_the_dict_path_still_refuses_the_wrong_scalar_type(self):
        data = payload(zones=[b"zone.home"])
        with pytest.raises(ValidationError) as info:
            HaSnapshotPayload.model_validate(data)
        assert safe_error_locations(info.value) == error(["zones", 0], "string_type")

    def test_the_dict_path_refuses_a_bytes_summary(self):
        data = with_events(event(summary=b"No School"))
        with pytest.raises(ValidationError) as info:
            HaSnapshotPayload.model_validate(data)
        assert safe_error_locations(info.value) == event_error("summary", "string_type")

    def test_a_string_is_not_a_zone_list(self):
        with pytest.raises(ValidationError) as info:
            HaSnapshotPayload.model_validate(payload(zones="zone.home"))
        assert safe_error_locations(info.value) == error(["zones"], "tuple_type")

    def test_the_last_of_two_equal_calendar_keys_wins(self):
        # Documented limit: duplicate JSON keys are not detected by the schema.
        body = (
            '{"fetched_at": "2026-10-23T06:00:00-07:00", "calendars": {'
            '"calendar.a": {"events": [{"start": "2026-10-23", "end": "2026-10-24",'
            ' "summary": "No School"}]}, "calendar.a": {"events": []}}}'
        )
        snapshot = HaSnapshotPayload.model_validate_json(body)
        assert snapshot.calendars["calendar.a"].events == ()


class TestImmutability:
    def test_calendars_cannot_be_changed_after_validation(self):
        snapshot = parse(payload())
        calendars = cast("MutableMapping[str, HaCalendar]", snapshot.calendars)
        with pytest.raises(TypeError):
            calendars["calendar.extra"] = calendars["calendar.district"]
        assert "calendar.extra" not in snapshot.calendars

    def test_attributes_cannot_be_reassigned(self):
        snapshot = parse(payload())
        with pytest.raises(ValidationError) as info:
            snapshot.zones = ()
        assert info.value.errors(include_url=False)[0]["type"] == "frozen_instance"


def school(index: int, calendars: list[str]) -> dict[str, object]:
    half = len(calendars) // 2
    return {
        "location_id": f"school-{index}",
        "person_ids": [f"person-{index}"],
        "bell_calendars": calendars[:half],
        "district_calendars": calendars[half:],
    }


class TestCalendarCapMatchesSettings:
    def test_the_cap_is_derived_from_the_settings_limits(self):
        assert MAX_CALENDARS == MAX_HA_SCHOOLS * 2 * MAX_SCHOOL_CALENDARS

    def test_every_configuration_settings_accepts_can_be_ingested(self):
        width = 2 * MAX_SCHOOL_CALENDARS
        names = [
            f"calendar.s{index}_{n}"
            for index in range(MAX_HA_SCHOOLS)
            for n in range(width)
        ]
        schools = [
            school(index, names[index * width : (index + 1) * width])
            for index in range(MAX_HA_SCHOOLS)
        ]
        settings = Settings.model_validate({"ha_schools": schools})
        configured = {
            entity for item in settings.ha_schools for entity in item.entities()
        }
        assert len(configured) == MAX_CALENDARS

        body = json.dumps(
            payload(calendars={entity: {"events": []} for entity in sorted(configured)})
        )
        assert len(body) < MAX_SNAPSHOT_BYTES
        snapshot = HaSnapshotPayload.model_validate_json(body)
        assert set(snapshot.calendars) == configured

    def test_one_more_calendar_than_settings_can_name_is_refused(self):
        names = [f"calendar.c{n}" for n in range(MAX_CALENDARS + 1)]
        assert rejected_at(
            payload(calendars={name: {"events": []} for name in names})
        ) == error(["calendars"], "too_long")
