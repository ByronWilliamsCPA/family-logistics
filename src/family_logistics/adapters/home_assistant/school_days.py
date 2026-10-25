"""Deterministic mapping of HA calendar events to school days (ADR 0007).

Pure functions: the same payload, schools, zone and window always give the
same rows, and nothing here reads a clock, the database or the network.

Each event summary is classified by :data:`SCHOOL_DAY_KEYWORDS`, then each
school's events are grouped by local day and decided:

* Kind: any unrecognized event makes the day UNKNOWN; a closure beside
  anything but regular-day events, or an early release beside a late start,
  is UNKNOWN; otherwise the one adjusted kind wins over NORMAL.
* Times: only a bell-calendar timed event that falls within one local day
  supplies bell times. A NORMAL, EARLY_RELEASE or LATE_START day needs at
  least one such event of its own kind, or it becomes UNKNOWN; the times are
  the span of those events. An UNKNOWN day spans every bell timed event of
  the day, else it is a zero-length row at local midnight. A NO_SCHOOL day is
  a zero-length row at local midnight.
* A day without events has no row, which the engine reports as a missing
  school schedule (SCHOOL-009) rather than assuming a normal day.

An event is never assumed to be a normal school day: a summary the table
does not recognize yields UNKNOWN.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from family_logistics.core.exceptions import ValidationError
from family_logistics.domain.context import HA_SNAPSHOT_SOURCE
from family_logistics.domain.events import SchoolDay, SchoolDayKind

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from zoneinfo import ZoneInfo

    from family_logistics.adapters.home_assistant.schemas import (
        HaCalendarEvent,
        HaSnapshotPayload,
    )
    from family_logistics.core.config import HaSchool

__all__ = [
    "NEGATION_WORDS",
    "SCHOOL_DAY_KEYWORDS",
    "DayWindow",
    "SnapshotMappingError",
    "classify_summary",
    "map_school_days",
]

# #ASSUME: external resources: school and district calendars title their
# events in English with one of these phrases. Matching is on whole words of
# the summary after case folding and replacing every non-alphanumeric
# character with a space ("No-School Day" reads as "no school day"). A summary
# matching exactly one adjusted kind (no school, early release, late start)
# takes that kind; one matching several adjusted kinds is UNKNOWN; one matching
# only the NORMAL phrases is NORMAL unless it also holds a word from
# NEGATION_WORDS; one matching nothing is UNKNOWN. Plain "holiday" is
# deliberately absent: "Holiday Concert" is not a closure.
# #VERIFY: before production, run the deployment's real calendars through
# classify_summary and review every UNKNOWN and every NORMAL day against the
# published school calendar; extend this table (never a fallback to NORMAL)
# for phrases the district actually uses.
SCHOOL_DAY_KEYWORDS: Final[Mapping[SchoolDayKind, tuple[str, ...]]] = {
    SchoolDayKind.NO_SCHOOL: (
        "no school",
        "school closed",
        "schools closed",
        "district closed",
        "no students",
        "non student day",
        "student free day",
        "teacher work day",
        "teacher workday",
        "professional development day",
        "staff development day",
        "winter break",
        "spring break",
        "fall break",
        "thanksgiving break",
        "mid winter break",
        "summer break",
        "holiday break",
        "school holiday",
        "no classes",
        "snow day",
        "emergency closure",
    ),
    SchoolDayKind.EARLY_RELEASE: (
        "early release",
        "early dismissal",
        "minimum day",
        "half day",
        "short day",
        "early out",
        "shortened day",
    ),
    SchoolDayKind.LATE_START: (
        "late start",
        "late arrival",
        "delayed start",
        "delayed opening",
        "two hour delay",
        "2 hour delay",
    ),
    SchoolDayKind.NORMAL: (
        "regular day",
        "regular schedule",
        "regular bell schedule",
        "normal day",
        "normal schedule",
        "school day",
        "full day",
        "bell schedule",
        "regular bells",
    ),
}
"""The one classification table: summary phrases for each school-day kind."""

NEGATION_WORDS: Final = frozenset(
    {"no", "not", "non", "cancelled", "canceled", "closed", "closure", "without"}
)
"""Words that stop a summary matching only NORMAL phrases from being NORMAL
("Not a school day", "Regular day cancelled")."""

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_ADJUSTED = (
    SchoolDayKind.NO_SCHOOL,
    SchoolDayKind.EARLY_RELEASE,
    SchoolDayKind.LATE_START,
)
_BELL = "bell"


class SnapshotMappingError(ValidationError):
    """The payload's calendars do not match the configured schools.

    The error code is ``UNKNOWN_CALENDAR``, ``MISSING_CALENDAR`` or
    ``NO_SCHOOLS``; the message never names a calendar entity.
    """


@dataclass(frozen=True, slots=True)
class DayWindow:
    """Inclusive range of local days the mapping keeps.

    Attributes:
        first: First local day kept.
        last: Last local day kept.
    """

    first: date
    last: date

    def __post_init__(self) -> None:
        """Refuse a window that ends before it starts.

        Raises:
            ValidationError: If ``last`` is before ``first``.
        """
        if self.last < self.first:
            msg = "the snapshot window ends before it starts"
            raise ValidationError(msg, field="last")

    def __contains__(self, day: object) -> bool:
        """Whether ``day`` is a date inside the window.

        Args:
            day: The value tested.

        Returns:
            True for a date from ``first`` to ``last`` inclusive.
        """
        return isinstance(day, date) and self.first <= day <= self.last


def _normalize(text: str) -> str:
    return " ".join(_NON_ALNUM.sub(" ", text.casefold()).split())


def classify_summary(summary: str) -> SchoolDayKind:
    """Classify one event summary with :data:`SCHOOL_DAY_KEYWORDS`.

    Args:
        summary: The event title.

    Returns:
        The kind the summary names, or UNKNOWN when it names none or several.
    """
    padded = f" {_normalize(summary)} "
    matched = {
        kind
        for kind, phrases in SCHOOL_DAY_KEYWORDS.items()
        if any(f" {phrase} " in padded for phrase in phrases)
    }
    adjusted = matched.intersection(_ADJUSTED)
    if len(adjusted) == 1:
        return adjusted.pop()
    if adjusted or SchoolDayKind.NORMAL not in matched:
        return SchoolDayKind.UNKNOWN
    if NEGATION_WORDS.intersection(padded.split()):
        return SchoolDayKind.UNKNOWN
    return SchoolDayKind.NORMAL


@dataclass(frozen=True, slots=True)
class _Mark:
    """One event's contribution to one local day."""

    kind: SchoolDayKind
    bell_span: tuple[datetime, datetime] | None


def _as_day(value: date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _local_days(event: HaCalendarEvent, tz: ZoneInfo) -> list[date]:
    """Return the local days an event covers (end exclusive)."""
    start, end = event.start, event.end
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        first_day = _as_day(start)
        count = (_as_day(end) - first_day).days
        return [first_day + timedelta(days=n) for n in range(count)]
    first = start.astimezone(tz).date()
    if end == start:
        return [first]
    # #EDGE: timing: the end is exclusive, so an event ending exactly at local
    # midnight does not touch the next day. Subtracting a microsecond from the
    # UTC instant (not the wall clock) keeps this right across DST changes.
    # #VERIFY: TestMultiDayAndDst covers midnight ends and the fall-back day.
    last = (end.astimezone(UTC) - timedelta(microseconds=1)).astimezone(tz).date()
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _marks(
    events: Sequence[HaCalendarEvent], role: str, tz: ZoneInfo
) -> Iterator[tuple[date, _Mark]]:
    for event in events:
        kind = classify_summary(event.summary)
        days = _local_days(event, tz)
        span: tuple[datetime, datetime] | None = None
        if (
            role == _BELL
            and len(days) == 1
            and isinstance(event.start, datetime)
            and isinstance(event.end, datetime)
        ):
            span = (event.start.astimezone(UTC), event.end.astimezone(UTC))
        for day in days:
            yield day, _Mark(kind, span)


def _decide_kind(kinds: set[SchoolDayKind]) -> SchoolDayKind:
    if SchoolDayKind.UNKNOWN in kinds:
        return SchoolDayKind.UNKNOWN
    if SchoolDayKind.NO_SCHOOL in kinds:
        others = kinds - {SchoolDayKind.NO_SCHOOL, SchoolDayKind.NORMAL}
        return SchoolDayKind.UNKNOWN if others else SchoolDayKind.NO_SCHOOL
    if {SchoolDayKind.EARLY_RELEASE, SchoolDayKind.LATE_START} <= kinds:
        return SchoolDayKind.UNKNOWN
    for kind in (SchoolDayKind.EARLY_RELEASE, SchoolDayKind.LATE_START):
        if kind in kinds:
            return kind
    return SchoolDayKind.NORMAL


def _span(spans: list[tuple[datetime, datetime]]) -> tuple[datetime, datetime]:
    return min(s for s, _ in spans), max(e for _, e in spans)


def _decide_day(
    day: date, marks: list[_Mark], tz: ZoneInfo
) -> tuple[SchoolDayKind, datetime, datetime]:
    """Decide one school's kind and bell times for one local day."""
    kind = _decide_kind({mark.kind for mark in marks})
    midnight = datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)
    if kind is SchoolDayKind.NO_SCHOOL:
        return kind, midnight, midnight
    if kind is not SchoolDayKind.UNKNOWN:
        own = [m.bell_span for m in marks if m.kind is kind and m.bell_span]
        if own:
            return (kind, *_span(own))
        # #ASSUME: data integrity: a classified day without a bell event of
        # its own kind has no trustworthy times (a district "Early Release"
        # beside a stale regular bell schedule), so it is reported UNKNOWN
        # rather than given the wrong dismissal time.
        # #VERIFY: TestAdjustedDays and TestNormalDay cover this downgrade.
        kind = SchoolDayKind.UNKNOWN
    spans = [m.bell_span for m in marks if m.bell_span]
    if spans:
        return (kind, *_span(spans))
    return kind, midnight, midnight


def _entity_problem(
    snapshot: HaSnapshotPayload, schools: Sequence[HaSchool]
) -> tuple[str, str] | None:
    """Return the error code and message when the calendars do not match.

    The message never names a calendar entity.
    """
    if not schools:
        return "NO_SCHOOLS", "no school is configured for the Home Assistant snapshot"
    configured = {entity for school in schools for entity in school.entities()}
    supplied = set(snapshot.calendars)
    if supplied - configured:
        return (
            "UNKNOWN_CALENDAR",
            "the snapshot holds a calendar that is not configured",
        )
    if configured - supplied:
        return "MISSING_CALENDAR", "the snapshot is missing a configured calendar"
    return None


def map_school_days(
    snapshot: HaSnapshotPayload,
    schools: Sequence[HaSchool],
    *,
    tz: ZoneInfo,
    window: DayWindow,
) -> tuple[SchoolDay, ...]:
    """Map a validated snapshot to one school day per person per local day.

    Args:
        snapshot: The validated payload.
        schools: The configured schools (``FL_HA_SCHOOLS``).
        tz: The household zone (``FL_TIMEZONE``) that defines a local day.
        window: The local days kept; events outside it are dropped.

    Returns:
        The school days, sorted by day then person.

    Raises:
        SnapshotMappingError: If no school is configured, or the payload's
            calendars are not exactly the configured ones.
    """
    # #CRITICAL: security: only calendars named in FL_HA_SCHOOLS may enter;
    # anything else (a personal calendar exposed by mistake) is refused, and
    # the error never names the entity.
    # #VERIFY: TestEntities asserts the refusal and the absence of the ID.
    problem = _entity_problem(snapshot, schools)
    if problem is not None:
        code, msg = problem
        raise SnapshotMappingError(msg, error_code=code)
    rows: list[SchoolDay] = []
    for school in schools:
        by_day: defaultdict[date, list[_Mark]] = defaultdict(list)
        for entity, role in school.entities().items():
            for day, mark in _marks(snapshot.calendars[entity].events, role, tz):
                if day in window:
                    by_day[day].append(mark)
        for day, marks in by_day.items():
            kind, start, end = _decide_day(day, marks, tz)
            rows.extend(
                SchoolDay(
                    person_id=person,
                    date=day,
                    location_id=school.location_id,
                    start=start,
                    end=end,
                    kind=kind,
                    source=HA_SNAPSHOT_SOURCE,
                )
                for person in school.person_ids
            )
    return tuple(sorted(rows, key=lambda row: (row.date, row.person_id)))
