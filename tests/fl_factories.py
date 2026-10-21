"""Shared test data for Family Logistics.

Privacy rule for this repository: family first names may appear; external
children appear ONLY as pseudonyms ("Passenger E", id ``ext-e``). No addresses,
calendar IDs, contact details or real ride histories.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from family_logistics.domain.models import (
    ExternalPassenger,
    OccurrenceKind,
    Person,
    PersonRole,
    SourceEventKey,
    Trip,
    Vehicle,
)
from family_logistics.domain.parser import ParseResult, parse_trip
from family_logistics.domain.registry import Registry

CALENDAR = "cal-family-transportation"  # placeholder, not a real calendar ID

# Friday 2026-10-23 15:30 local (America/Los_Angeles is UTC-7 in October).
FRIDAY_START = datetime(2026, 10, 23, 22, 30, tzinfo=UTC)
FRIDAY_END = FRIDAY_START + timedelta(minutes=45)

REGISTRY = Registry(
    people=(
        Person("morgan", "Morgan", PersonRole.PARENT, can_drive=True),
        Person(
            "riley",
            "Riley",
            PersonRole.PARENT,
            aliases=("V",),
            can_drive=True,
            default_vehicle_id="family-suv",
        ),
        Person("alex", "Alex", PersonRole.CHILD),
        Person("sam", "Sam", PersonRole.CHILD),
        Person("jordan", "Jordan", PersonRole.CHILD),
        Person("casey", "Casey", PersonRole.CHILD, monitoring_enabled=False),
        Person("ext-driver-s", "Driver S", PersonRole.CONTACT, can_drive=True),
        Person("teacher-a", "Teacher A", PersonRole.CONTACT, can_drive=True),
    ),
    external_passengers=(
        ExternalPassenger(
            "ext-e",
            "Passenger E",
            "household-e",
            default_dropoff_location_id="loc-e-home",
        ),
        ExternalPassenger("ext-r", "Passenger R", "household-r"),
        ExternalPassenger("ext-j1", "Passenger J", "household-j1"),
        ExternalPassenger("ext-j2", "Passenger J", "household-j2"),
    ),
    vehicles=(
        Vehicle("family-suv", "family SUV", seat_capacity=None),
        Vehicle("sedan", "Sedan", seat_capacity=5),
    ),
)


def notes(
    *,
    trip_type: str = "pickup",
    status: str = "confirmed",
    driver: str = "Riley",
    family: str = "Sam",
    external: str = "",
    pickup: str = "Music Studio",
    destinations: tuple[tuple[str, str], ...] = (("Sam", "HOME"),),
    stop_order: str = "AUTO",
    confirm: str | None = None,
    free: str = "",
    newline: str = "\n",
) -> str:
    """Build a Template v1 notes block."""
    lines = [
        "FL_VERSION: 1",
        f"TRIP_TYPE: {trip_type}",
        f"STATUS: {status}",
        f"DRIVER: {driver}",
        f"FAMILY_PASSENGERS: {family}",
        f"EXTERNAL_PASSENGERS: {external}",
        f"PICKUP: {pickup}",
        "DESTINATIONS:",
        *(f"{name}: {dest}" for name, dest in destinations),
        f"STOP_ORDER: {stop_order}",
    ]
    if confirm is not None:
        lines.append(f"CONFIRM: {confirm}")
    lines.append(f"NOTES: {free}")
    return newline.join(lines)


def key(uid: str = "evt-1", recurrence_id: str | None = None) -> SourceEventKey:
    """Build a source event key."""
    return SourceEventKey(CALENDAR, uid, recurrence_id)


def parse(
    text: str,
    *,
    uid: str = "evt-1",
    recurrence_id: str | None = None,
    start: datetime = FRIDAY_START,
    end: datetime = FRIDAY_END,
    occurrence: OccurrenceKind = OccurrenceKind.SINGLE,
) -> ParseResult:
    """Parse notes against the shared registry."""
    return parse_trip(
        text,
        key=key(uid, recurrence_id),
        start=start,
        end=end,
        registry=REGISTRY,
        occurrence=occurrence,
    )


def trip(text: str, **kwargs: object) -> Trip:
    """Parse notes that must be valid and return the trip."""
    result = parse(text, **kwargs)  # type: ignore[arg-type]
    assert result.trip is not None, [f.message for f in result.findings]
    return result.trip
