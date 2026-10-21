"""Engine test builders for the fictional household.

Places are fictional and carry no addresses. Times are local
(America/Los_Angeles, UTC-7 in late October 2026) converted to UTC.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fl_factories import REGISTRY

from family_logistics.adapters.routing.base import StaticRouteProvider
from family_logistics.domain.events import (
    Activity,
    ActivityClassification,
    AssessmentInput,
    SchoolDay,
    SchoolDayKind,
    TransportDefault,
    TransportDirection,
    TransportMode,
)
from family_logistics.domain.models import Location, Person, SensitiveStr, Trip

ZONE = ZoneInfo("America/Los_Angeles")
FRIDAY = date(2026, 10, 23)
THURSDAY = date(2026, 10, 22)
MINUTE = timedelta(minutes=1)


def at(hour: int, minute: int = 0, day: date = FRIDAY) -> datetime:
    """Return a UTC time from a local wall-clock time."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)
    return local.astimezone(UTC)


NOW = at(7, 0)
"""Friday 07:00 local: before every scenario event."""

LOCATIONS = (
    Location(
        "home", "Family Home", verified=True, address=SensitiveStr("1 Example Way")
    ),
    Location("high-school", "High School", aliases=("HS",), verified=True),
    Location("middle-school", "Middle School", verified=True),
    Location("studio", "Music Studio", verified=True),
    Location("xc", "XC Practice", verified=True),
    Location("away-meet", "Away Meet Park", verified=True),
    Location("game", "Sibling Game", verified=True),
    Location("new-place", "New Place", verified=False),
    Location("loc-e-home", "Passenger E Home", verified=True),
)

FAMILY = frozenset({"morgan", "riley", "alex", "sam", "jordan", "casey"})

PEOPLE = tuple(
    Person(
        p.person_id,
        p.display_name,
        p.role,
        aliases=p.aliases,
        can_drive=p.can_drive,
        default_vehicle_id=p.default_vehicle_id,
        monitoring_enabled=p.monitoring_enabled,
        home_location_id="home" if p.person_id in FAMILY else None,
    )
    for p in REGISTRY.people
)


def routes(minutes: int = 15) -> StaticRouteProvider:
    """Return a provider with the same driving time between every pair."""
    ids = [loc.location_id for loc in LOCATIONS]
    matrix = {
        (a, b): timedelta(minutes=minutes) for a, b in itertools.permutations(ids, 2)
    }
    return StaticRouteProvider(matrix)


def activity(
    activity_id: str,
    who: tuple[str, ...],
    location_id: str | None,
    start: datetime,
    end: datetime,
    *,
    text: str | None = None,
    kind: ActivityClassification = ActivityClassification.OTHER,
) -> Activity:
    """Build an activity."""
    return Activity(
        activity_id,
        who,
        location_id,
        text,
        start,
        end,
        ("cal-person", activity_id, ""),
        kind,
    )


def school(
    person_id: str,
    start: datetime,
    end: datetime,
    kind: SchoolDayKind = SchoolDayKind.NORMAL,
    location_id: str = "high-school",
) -> SchoolDay:
    """Build a school day on the start's local date."""
    return SchoolDay(
        person_id,
        start.astimezone(ZONE).date(),
        location_id,
        start,
        end,
        kind,
        "ha-bell",
    )


def default(
    person_id: str,
    direction: TransportDirection,
    mode: TransportMode,
    *,
    driver_id: str | None = None,
    school_id: str = "high-school",
) -> TransportDefault:
    """Build a weekday school transport default."""
    origin, destination = (
        ("home", school_id)
        if direction is TransportDirection.TO_SCHOOL
        else (school_id, "home")
    )
    return TransportDefault(
        person_id,
        frozenset(range(5)),
        direction,
        mode,
        origin,
        destination,
        driver_id=driver_id,
    )


def assessment(
    *,
    activities: tuple[Activity, ...] = (),
    trips: tuple[Trip, ...] = (),
    school_days: tuple[SchoolDay, ...] = (),
    defaults: tuple[TransportDefault, ...] = (),
    people: tuple[Person, ...] = PEOPLE,
    fresh: bool = True,
) -> AssessmentInput:
    """Build an assessment input for Friday with fresh sources."""
    sources = (("icloud", NOW), ("ha_snapshot", NOW)) if fresh else ()
    return AssessmentInput(
        window_start=at(0, 0),
        window_end=at(23, 59),
        people=people,
        external_passengers=REGISTRY.external_passengers,
        locations=LOCATIONS,
        vehicles=REGISTRY.vehicles,
        transport_defaults=defaults,
        activities=activities,
        trips=trips,
        school_days=school_days,
        source_freshness=sources,
    )
