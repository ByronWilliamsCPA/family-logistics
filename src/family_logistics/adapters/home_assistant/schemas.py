"""Payload schema of the Home Assistant school snapshot (ADR 0007).

Home Assistant pushes the raw output of its ``calendar.get_events`` action for
each configured school calendar, the zone entity IDs it knows, and the time it
fetched them. Every model here is strict, refuses unknown keys and bounds every
list and string, so a malformed or oversized payload is refused before any of
it is mapped or stored.

The payload is untrusted input from the home network. Validation errors are
reported through :func:`safe_error_locations`, which keeps only the error type
and a location made of schema field names and list indexes: no input value,
calendar entity ID or event text ever reaches a response or a log.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, PlainValidator, model_validator

from family_logistics.core.config import HA_CALENDAR_ENTITY_PATTERN

if TYPE_CHECKING:
    from pydantic import ValidationError as PydanticValidationError

__all__ = [
    "MAX_CALENDARS",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_EVENTS_PER_CALENDAR",
    "MAX_LOCATION_LENGTH",
    "MAX_REPORTED_ERRORS",
    "MAX_SNAPSHOT_BYTES",
    "MAX_SUMMARY_LENGTH",
    "MAX_TIME_LENGTH",
    "MAX_YEAR",
    "MAX_ZONES",
    "MIN_YEAR",
    "ZONE_ENTITY_PATTERN",
    "HaCalendar",
    "HaCalendarEvent",
    "HaSnapshotPayload",
    "safe_error_locations",
]

# #CRITICAL: security: the work one untrusted request can cause is bounded in
# three places, and none alone suffices. The byte limit and these list and
# string bounds cap what is parsed and held in memory; the year bounds
# (MIN_YEAR to MAX_YEAR) keep every time far from the edges of ``datetime``;
# and the mapper clips each event to the snapshot window before listing its
# days, so the mapping work is bounded by events times window days, not by
# how long an event claims to last.
# #VERIFY: the route refuses a body over MAX_SNAPSHOT_BYTES before parsing;
# tests/unit/test_ha_snapshot_schema.py covers each list, string and year
# bound; tests/unit/test_ha_school_days.py::TestExtremeEvents covers clipping.
MAX_SNAPSHOT_BYTES = 1_048_576
"""Largest request body accepted, in bytes (1 MiB)."""
MAX_CALENDARS = 16
MAX_EVENTS_PER_CALENDAR = 1000
MAX_ZONES = 64
MAX_TIME_LENGTH = 40
MAX_SUMMARY_LENGTH = 500
MAX_DESCRIPTION_LENGTH = 4000
MAX_LOCATION_LENGTH = 500
MAX_REPORTED_ERRORS = 20
"""Most validation errors reported for one request."""
MIN_YEAR = 2000
MAX_YEAR = 2100
"""Plausible years for ``fetched_at`` and event bounds (inclusive)."""

ZONE_ENTITY_PATTERN = r"^zone\.[a-z0-9_]{1,200}$"
"""A Home Assistant zone entity ID; person and device trackers never match."""

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})"
)

ZoneEntityId = Annotated[str, Field(pattern=ZONE_ENTITY_PATTERN)]
CalendarEntityId = Annotated[str, Field(pattern=HA_CALENDAR_ENTITY_PATTERN)]


def _plausible[T: date](value: T) -> T:
    """Return ``value`` if its year is within ``MIN_YEAR`` to ``MAX_YEAR``.

    Args:
        value: A parsed date or date-time.

    Returns:
        The same value.

    Raises:
        ValueError: If the year is outside the bounds. The message never
            echoes the value.
    """
    if not MIN_YEAR <= value.year <= MAX_YEAR:
        msg = f"expected a year from {MIN_YEAR} to {MAX_YEAR}"
        raise ValueError(msg)
    return value


def _parse_instant(value: object) -> datetime:
    """Parse an ISO 8601 date-time that carries a UTC offset.

    Args:
        value: The raw JSON value.

    Returns:
        The timezone-aware date-time.

    Raises:
        ValueError: If the value is not a bounded string in that shape, or
            its year is implausible. The message never echoes the value.
    """
    if (
        not isinstance(value, str)
        or len(value) > MAX_TIME_LENGTH
        or _INSTANT.fullmatch(value) is None
    ):
        msg = "expected an ISO 8601 date-time with a UTC offset"
        raise ValueError(msg)
    return _plausible(datetime.fromisoformat(value))


def _parse_event_time(value: object) -> date | datetime:
    """Parse an event bound: a date (all-day event) or an aware date-time.

    Args:
        value: The raw JSON value.

    Returns:
        A ``date`` for ``YYYY-MM-DD``, otherwise an aware ``datetime``.

    Raises:
        ValueError: If the value is neither shape, or its year is
            implausible. The message never echoes the value.
    """
    if isinstance(value, str) and _DATE.fullmatch(value) is not None:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            msg = "expected a valid calendar date"
            raise ValueError(msg) from None
        return _plausible(parsed)
    try:
        return _parse_instant(value)
    except ValueError:
        msg = "expected a date or an ISO 8601 date-time with a UTC offset"
        raise ValueError(msg) from None


Instant = Annotated[datetime, PlainValidator(_parse_instant)]
EventTime = Annotated[date | datetime, PlainValidator(_parse_event_time)]

_STRICT = ConfigDict(
    extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
)


class HaCalendarEvent(BaseModel):
    """One event as ``calendar.get_events`` returns it.

    All-day events carry dates with an exclusive end; timed events carry
    date-times with a UTC offset. Only the summary is read for
    classification: description and location are accepted because Home
    Assistant sends them, but they are never used, logged or stored.

    Attributes:
        model_config: Strict, immutable, unknown keys refused.
        start: First day (all-day) or start instant (timed).
        end: Day after the last day (all-day) or end instant (timed).
        summary: Event title.
        description: Ignored.
        location: Ignored.
    """

    model_config = _STRICT

    start: EventTime
    end: EventTime
    summary: str = Field(max_length=MAX_SUMMARY_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    location: str | None = Field(default=None, max_length=MAX_LOCATION_LENGTH)

    @property
    def all_day(self) -> bool:
        """Whether the event is an all-day event (dates, not date-times)."""
        return not isinstance(self.start, datetime)

    @model_validator(mode="after")
    def _bounds_are_coherent(self) -> Self:
        """Require matching bound types and a non-negative duration.

        Returns:
            The validated event.

        Raises:
            ValueError: If one bound is a date and the other a date-time, an
                all-day event does not cover at least one day, or a timed
                event ends before it starts.
        """
        start_timed = isinstance(self.start, datetime)
        if start_timed != isinstance(self.end, datetime):
            msg = "start and end must both be dates or both be date-times"
            raise ValueError(msg)
        if start_timed:
            if self.end < self.start:
                msg = "a timed event cannot end before it starts"
                raise ValueError(msg)
        elif self.end <= self.start:
            msg = "an all-day event must end after the day it starts"
            raise ValueError(msg)
        return self


class HaCalendar(BaseModel):
    """The ``calendar.get_events`` result for one calendar entity.

    Attributes:
        model_config: Strict, immutable, unknown keys refused.
        events: The calendar's events in the requested window.
    """

    model_config = _STRICT

    events: tuple[HaCalendarEvent, ...] = Field(max_length=MAX_EVENTS_PER_CALENDAR)


class HaSnapshotPayload(BaseModel):
    """Body of ``POST /api/v1/ingest/ha-snapshot``.

    Attributes:
        model_config: Strict, immutable, unknown keys refused.
        fetched_at: When Home Assistant read the calendars; must carry a UTC
            offset.
        calendars: ``calendar.get_events`` output keyed by calendar entity ID.
        zones: Zone entity IDs Home Assistant knows; checked against the
            registry and never stored. Coordinates are not accepted.
    """

    model_config = _STRICT

    fetched_at: Instant
    calendars: dict[CalendarEntityId, HaCalendar] = Field(
        min_length=1, max_length=MAX_CALENDARS
    )
    zones: tuple[ZoneEntityId, ...] = Field(default=(), max_length=MAX_ZONES)


_FIELD_NAMES = frozenset(
    name
    for model in (HaSnapshotPayload, HaCalendar, HaCalendarEvent)
    for name in model.model_fields
)


def safe_error_locations(error: PydanticValidationError) -> list[dict[str, object]]:
    """Reduce a payload validation error to types and redacted locations.

    Location parts that are schema field names or list indexes are kept; any
    other part (a calendar entity ID used as a key, for instance) becomes
    ``"*"``. Input values and messages are dropped.

    Args:
        error: The pydantic validation error raised for the payload.

    Returns:
        At most ``MAX_REPORTED_ERRORS`` entries of ``{"loc", "type"}``.
    """
    # #CRITICAL: security: a 422 body or log line must never echo the payload
    # (children's school calendars). Only field names, indexes and the error
    # type survive this function.
    # #VERIFY: TestSafeErrorLocations feeds private-looking keys and values and
    # asserts none of them appear in the output.
    reported: list[dict[str, object]] = []
    for detail in error.errors(
        include_url=False, include_context=False, include_input=False
    )[:MAX_REPORTED_ERRORS]:
        loc: list[object] = [
            part if isinstance(part, int) or part in _FIELD_NAMES else "*"
            for part in detail["loc"]
        ]
        reported.append({"loc": loc, "type": detail["type"]})
    return reported
