"""Payload schema of the Home Assistant school snapshot (ADR 0007).

Home Assistant pushes the raw output of its ``calendar.get_events`` action for
each configured school calendar, the zone entity IDs it knows, and the time it
fetched them. Every model here is strict, refuses unknown keys and bounds every
list and string, so a malformed or oversized payload is refused before any of
it is mapped or stored. The models validate from JSON text
(``model_validate_json``) or from the dict ``json.loads`` returns
(``model_validate``); both paths apply the same checks. Duplicate JSON object
keys are not detected here: parsers keep the last value, so a caller that must
refuse them has to parse with ``object_pairs_hook`` before validating.

The payload is untrusted input from the home network. Validation errors are
reported through :func:`safe_error_locations`, which keeps only the error type
and a location made of schema field names and list indexes: no input value,
calendar entity ID or event text ever reaches a response or a log.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    field_validator,
    model_validator,
)

from family_logistics.core.config import (
    MAX_HA_SCHOOLS,
    MAX_SCHOOL_CALENDARS,
    CalendarEntityId,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import ValidationError as PydanticValidationError

__all__ = [
    "MAX_CALENDARS",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_EVENTS_PER_CALENDAR",
    "MAX_LOCATION_LENGTH",
    "MAX_REPORTED_ERRORS",
    "MAX_SNAPSHOT_BYTES",
    "MAX_SUMMARY_LENGTH",
    "MAX_TOTAL_EVENTS",
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
# three places, and none alone suffices. The byte limit and these list,
# string and total-event bounds cap what is parsed and held in memory; the year
# bounds
# (MIN_YEAR to MAX_YEAR) keep every time far from the edges of ``datetime``;
# and the mapper clips each event to the snapshot window before listing its
# days, so the mapping work is bounded by events times window days, not by
# how long an event claims to last.
# #VERIFY: the ingest route must refuse a body over MAX_SNAPSHOT_BYTES before
# parsing (this module only declares the limit); the tests in
# tests/unit/test_ha_snapshot_schema.py cover each list, string, total-event
# and year bound; tests/unit/test_ha_school_days.py::TestExtremeEvents covers
# clipping.
MAX_SNAPSHOT_BYTES = 1_048_576
"""Largest request body the ingest route may accept, in bytes (1 MiB)."""
MAX_CALENDARS = MAX_HA_SCHOOLS * 2 * MAX_SCHOOL_CALENDARS
"""Most calendars one payload may carry.

Derived from the ``FL_HA_SCHOOLS`` limits (schools, times a bell and a district
list, times calendars per list) so that every configuration ``Settings``
accepts, even one with no shared calendar, can be ingested: the mapper needs a
snapshot to hold exactly the configured calendars.
"""
MAX_EVENTS_PER_CALENDAR = 1000
MAX_TOTAL_EVENTS = 20_000
"""Most events across all calendars of one payload.

A backstop that bounds a validated payload however its bytes arrived. It sits
above what ``MAX_SNAPSHOT_BYTES`` can carry (at least 50 bytes per event), so
it never refuses a body the byte limit lets through.
"""
MAX_ZONES = 64
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

# ``re.ASCII`` keeps ``\d`` to 0-9: Unicode digits would pass the pattern and
# then fail inside ``fromisoformat``.
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})",
    re.ASCII,
)

ZoneEntityId = Annotated[str, Field(pattern=ZONE_ENTITY_PATTERN)]


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
        ValueError: If the value is not a string in that shape (the pattern
            bounds its length), or its year is implausible. The message never
            echoes the value.
    """
    msg = "expected an ISO 8601 date-time with a UTC offset"
    if not isinstance(value, str) or _INSTANT.fullmatch(value) is None:
        raise ValueError(msg)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(msg) from None
    return _plausible(parsed)


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


def _discard_text(limit: int) -> Callable[[object], None]:
    """Build a validator that checks an ignored text field, then drops it.

    Args:
        limit: Longest accepted string.

    Returns:
        A validator that returns ``None`` for ``None`` or a string of at most
        ``limit`` characters and raises ``ValueError`` otherwise. The message
        never echoes the value.
    """

    def discard(value: object) -> None:
        if value is None:
            return
        if not isinstance(value, str) or len(value) > limit:
            msg = f"expected a string of at most {limit} characters"
            raise ValueError(msg)

    return discard


Instant = Annotated[datetime, PlainValidator(_parse_instant)]
EventTime = Annotated[date | datetime, PlainValidator(_parse_event_time)]
DiscardedDescription = Annotated[
    None, PlainValidator(_discard_text(MAX_DESCRIPTION_LENGTH))
]
DiscardedLocation = Annotated[None, PlainValidator(_discard_text(MAX_LOCATION_LENGTH))]

_STRICT = ConfigDict(
    extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
)


class HaCalendarEvent(BaseModel):
    """One event as ``calendar.get_events`` returns it.

    All-day events carry dates with an exclusive end; timed events carry
    date-times with a UTC offset. Only the summary is read for
    classification: description and location are accepted (and length
    checked) because Home Assistant sends them, but their text is discarded
    at validation, so the validated event never holds it.

    Attributes:
        model_config: Strict, immutable, unknown keys refused.
        start: First day (all-day) or start instant (timed).
        end: Day after the last day (all-day) or end instant (timed).
        summary: Event title.
        description: Always ``None``; the sent text is discarded.
        location: Always ``None``; the sent text is discarded.
    """

    model_config = _STRICT

    start: EventTime
    end: EventTime
    summary: str = Field(max_length=MAX_SUMMARY_LENGTH)
    description: DiscardedDescription = None
    location: DiscardedLocation = None

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
        events: The calendar's events in the requested window. An empty
            tuple means the calendar was read and had no events; the sender
            must not post a snapshot when any calendar fetch failed, because
            the payload has no per-calendar completeness signal.
    """

    model_config = _STRICT

    # strict=False on a tuple field only lets the container be a JSON list; its
    # items stay strict.
    events: Annotated[tuple[HaCalendarEvent, ...], Field(strict=False)] = Field(
        max_length=MAX_EVENTS_PER_CALENDAR
    )


CalendarMap = Mapping[CalendarEntityId, HaCalendar]
"""Calendars by entity ID; read-only once the payload is validated."""


class HaSnapshotPayload(BaseModel):
    """Body of ``POST /api/v1/ingest/ha-snapshot``.

    Validate JSON text with ``model_validate_json`` or the parsed dict with
    ``model_validate``; scalars stay strict on both paths, only the JSON list
    to tuple conversion is allowed.

    Attributes:
        model_config: Strict, immutable, unknown keys refused.
        fetched_at: When Home Assistant read the calendars; must carry a UTC
            offset.
        calendars: ``calendar.get_events`` output keyed by calendar entity ID,
            a read-only mapping after validation. At most ``MAX_CALENDARS``
            calendars and ``MAX_TOTAL_EVENTS`` events in all.
        zones: Zone entity IDs Home Assistant knows; for the ingest route to
            check against the registry, never stored. Coordinates are not
            accepted.
    """

    model_config = _STRICT

    fetched_at: Instant
    calendars: CalendarMap = Field(min_length=1, max_length=MAX_CALENDARS)
    zones: Annotated[tuple[ZoneEntityId, ...], Field(strict=False)] = Field(
        default=(), max_length=MAX_ZONES
    )

    @field_validator("calendars", mode="after")
    @classmethod
    def _calendars_are_read_only(cls, value: CalendarMap) -> CalendarMap:
        """Freeze the calendar mapping so the bounds cannot be bypassed later.

        Args:
            value: The validated calendars.

        Returns:
            A read-only view of a copy.
        """
        return MappingProxyType(dict(value))

    @model_validator(mode="after")
    def _events_are_bounded_in_total(self) -> Self:
        """Cap the events across all calendars.

        Returns:
            The validated payload.

        Raises:
            ValueError: If the calendars hold more than ``MAX_TOTAL_EVENTS``
                events together.
        """
        if sum(len(item.events) for item in self.calendars.values()) > MAX_TOTAL_EVENTS:
            msg = f"at most {MAX_TOTAL_EVENTS} events in all calendars"
            raise ValueError(msg)
        return self


_FIELD_NAMES = frozenset(
    name
    for model in (HaSnapshotPayload, HaCalendar, HaCalendarEvent)
    for name in model.model_fields
)


def safe_error_locations(error: PydanticValidationError) -> list[dict[str, object]]:
    """Reduce a payload validation error to types and redacted locations.

    Location parts that are schema field names or list indexes are kept; any
    other part (a calendar entity ID used as a key, for instance) becomes
    ``"*"``, and so does a part that pydantic reports as a rejected dict key
    (the part before the ``[key]`` marker), even when it equals a field name.
    Input values and messages are dropped.

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
        parts = detail["loc"]
        loc: list[object] = [
            part
            if isinstance(part, int)
            or (part in _FIELD_NAMES and parts[index + 1 : index + 2] != ("[key]",))
            else "*"
            for index, part in enumerate(parts)
        ]
        reported.append({"loc": loc, "type": detail["type"]})
    return reported
