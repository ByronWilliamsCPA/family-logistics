"""Database seeding for service-level tests (fictional household, no addresses)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from fl_engine import LOCATIONS, PEOPLE, at
from fl_factories import CALENDAR, REGISTRY

from family_logistics.core.config import Settings
from family_logistics.db.registry import Household, RegistryStore
from family_logistics.db.sources import CalendarSourceRepository, SourceEventRepository
from family_logistics.domain.events import (
    CalendarKind,
    CalendarSource,
    CollectionResult,
    SourceEvent,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from family_logistics.domain.events import EventStatus

PERSON_CALENDAR = "cal-person-sam"
WINDOW_START = at(0, 0)
WINDOW_END = at(23, 59)


def make_settings(**overrides: object) -> Settings:
    """Build ``Settings`` for service tests without reading the environment.

    The person calendar is selected by default (``FL_CALDAV_CALENDARS``), so
    the assessment service judges and reads it like the transport calendar.
    Pass ``caldav_calendars=""`` to leave it unselected.

    Args:
        overrides: Setting values by field name.

    Returns:
        Validated settings (the unit-test conftest isolates ``FL_*`` and
        ``.env``).
    """
    values: dict[str, object] = {"caldav_calendars": PERSON_CALENDAR}
    return Settings.model_validate(values | overrides)


def seed_registry(engine: Engine) -> None:
    """Store the fictional household and its two calendars."""
    with engine.begin() as connection:
        store = RegistryStore(connection)
        for vehicle in REGISTRY.vehicles:
            store.upsert_vehicle(vehicle)
        for location in LOCATIONS:
            store.upsert_location(replace(location, address=None))
        for household in ("household-e", "household-r", "household-j1", "household-j2"):
            store.upsert_household(Household(household, household))
        for person in PEOPLE:
            store.upsert_person(person)
        for passenger in REGISTRY.external_passengers:
            store.upsert_external_passenger(passenger)
        store.upsert_calendar_source(
            CalendarSource(CALENDAR, "Family Transportation", CalendarKind.TRANSPORT)
        )
        store.upsert_calendar_source(
            CalendarSource(PERSON_CALENDAR, "Sam", CalendarKind.PERSON, person_id="sam")
        )


def event(
    calendar_id: str,
    uid: str,
    start: datetime,
    end: datetime,
    *,
    summary: str = "Event",
    location: str | None = None,
    description: str | None = None,
    status: EventStatus = "confirmed",
    all_day: bool = False,
    recurrence_id: str = "",
    is_override: bool = False,
    has_rrule: bool = False,
) -> SourceEvent:
    """Build a collected occurrence."""
    return SourceEvent(
        calendar_id=calendar_id,
        uid=uid,
        recurrence_id=recurrence_id,
        start=start,
        end=end,
        all_day=all_day,
        summary=summary,
        location=location,
        description=description,
        status=status,
        etag=None,
        raw_hash=f"{uid}-{recurrence_id}-{description}-{start.isoformat()}",
        is_override=is_override,
        series_uid_has_rrule=has_rrule,
        last_seen=start,
    )


def collect(
    engine: Engine,
    calendar_id: str,
    events: tuple[SourceEvent, ...],
    *,
    at_time: datetime,
    complete: bool = True,
) -> None:
    """Store a collection result and record the calendar's success."""
    sources = CalendarSourceRepository(engine)
    source = sources.get(calendar_id)
    assert source is not None
    result = CollectionResult(
        source,
        events,
        WINDOW_START - timedelta(days=1),
        WINDOW_END + timedelta(days=14),
        at_time,
        complete=complete,
        error=None if complete else "boom",
    )
    SourceEventRepository(engine).apply(result, at_time)
    if complete:
        sources.record_success(calendar_id, at_time)


__all__ = ["PERSON_CALENDAR", "collect", "event", "make_settings", "seed_registry"]
