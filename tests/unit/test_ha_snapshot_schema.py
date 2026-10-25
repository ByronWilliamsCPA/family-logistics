"""Home Assistant snapshot payload schema: strict, bounded, no extra keys."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from family_logistics.adapters.home_assistant.schemas import (
    MAX_CALENDARS,
    MAX_DESCRIPTION_LENGTH,
    MAX_EVENTS_PER_CALENDAR,
    MAX_SUMMARY_LENGTH,
    MAX_ZONES,
    HaCalendarEvent,
    HaSnapshotPayload,
    safe_error_locations,
)

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
        "data",
        [
            payload(extra=1),
            payload(fetched_at="2026-10-23T06:00:00"),
            payload(fetched_at=1_761_224_400),
            payload(fetched_at="yesterday"),
            payload(zones=["person.sam"]),
            payload(zones=["device_tracker.phone"]),
            payload(zones="zone.home"),
            payload(calendars={"person.sam": {"events": []}}),
            payload(calendars={"sensor.location": {"events": []}}),
            payload(calendars={"calendar.district": {"events": [], "extra": 1}}),
            payload(calendars={"calendar.district": []}),
            payload(calendars={}),
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
    def test_bad_payload_is_rejected(self, data):
        with pytest.raises(ValidationError):
            parse(data)

    @pytest.mark.parametrize(
        "item",
        [
            event(uid="abc"),
            event(summary=None),
            event(summary=5),
            event(start="2026-10-23T08:00:00", end="2026-10-23T09:00:00"),
            event(start="2026-10-23", end="2026-10-23T09:00:00-07:00"),
            event(start="2026-10-24", end="2026-10-23"),
            event(start="2026-10-23", end="2026-10-23"),
            event(
                start="2026-10-23T09:00:00-07:00",
                end="2026-10-23T08:00:00-07:00",
            ),
            event(start="23/10/2026"),
            event(start="2026-02-30", end="2026-03-01"),
            event(start="2026-10-23T08:00:00-07:00" + " " * 40),
            event(summary="x" * (MAX_SUMMARY_LENGTH + 1)),
            event(description="x" * (MAX_DESCRIPTION_LENGTH + 1)),
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
            "too-long-time",
            "summary-too-long",
            "description-too-long",
        ],
    )
    def test_bad_event_is_rejected(self, item):
        with pytest.raises(ValidationError):
            parse(with_events(item))

    def test_too_many_calendars(self):
        calendars = {
            f"calendar.school_{i}": {"events": []} for i in range(MAX_CALENDARS + 1)
        }
        with pytest.raises(ValidationError):
            parse(payload(calendars=calendars))

    def test_too_many_events(self):
        with pytest.raises(ValidationError):
            parse(with_events(*[event()] * (MAX_EVENTS_PER_CALENDAR + 1)))

    def test_too_many_zones(self):
        zones = [f"zone.z{i}" for i in range(MAX_ZONES + 1)]
        with pytest.raises(ValidationError):
            parse(payload(zones=zones))


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
        with pytest.raises(ValidationError) as caught:
            parse(data)
        errors = safe_error_locations(caught.value)
        text = json.dumps(errors)
        assert "private" not in text.lower()
        assert {
            "loc": ["calendars", "*", "events", 0, "start"],
            "type": "value_error",
        } in (errors)
        assert all(set(error) == {"loc", "type"} for error in errors)

    def test_locations_are_capped(self):
        zones = ["bad"] * MAX_ZONES
        with pytest.raises(ValidationError) as caught:
            parse(payload(zones=zones))
        assert len(safe_error_locations(caught.value)) <= 20


class TestEventModel:
    def test_event_is_immutable_and_strict(self):
        item = HaCalendarEvent.model_validate_json(json.dumps(event()))
        with pytest.raises(ValidationError):
            item.summary = "changed"
