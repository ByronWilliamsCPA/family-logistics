"""Deterministic mapping of HA calendar events to school days (ADR 0007).

Pure functions: the same payload, schools, zone and window always give the
same rows, and nothing here reads a clock, the database or the network.

Each event summary is classified by :data:`SCHOOL_DAY_KEYWORDS` and
:data:`CONTEXT_WORDS`, then each school's events are clipped to the window,
grouped by local day and decided:

* Kind: any unrecognized event makes the day UNKNOWN; a closure that does not
  cover the whole local day, or a closure beside anything but regular-day
  events, or an early release beside a late start, is UNKNOWN; otherwise the
  one adjusted kind wins over NORMAL.
* Times: only a bell-calendar timed event of positive length that falls
  within one local day supplies bell times. A NORMAL, EARLY_RELEASE or LATE_START day needs at
  least one such event of its own kind, or it becomes UNKNOWN; the times are
  the span of those events. An UNKNOWN day spans every bell timed event of
  the day, else it is a zero-length row at local midnight. A NO_SCHOOL day is
  a zero-length row at local midnight.
* A day without events has no row, which the engine reports as a missing
  school schedule (SCHOOL-009) rather than assuming a normal day.

An event is never assumed to be a normal school day: a summary the table
does not recognize, or one qualified by a context word, yields UNKNOWN.

#CRITICAL: Data integrity: a false NO_SCHOOL is the dangerous direction (the
engine builds no school block and suppresses transport defaults, so a child
drops out of the checks on a real school day); a false UNKNOWN only costs a
SCHOOL-009 warning. Every ambiguity therefore resolves to UNKNOWN.
#VERIFY: tests/unit/test_ha_school_days.py::TestClassifySummary pins the
reviewed regression summaries; TestNoSchool pins the closure rules.
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
    "CONTEXT_WORDS",
    "SCHOOL_DAY_KEYWORDS",
    "DayWindow",
    "SnapshotMappingError",
    "classify_summary",
    "map_school_days",
]

# #ASSUME: external resources: school and district calendars title their
# events in English with one of these phrases. Matching is on whole words of
# the summary after case folding and replacing every non-alphanumeric
# character with a space ("No-School Day" reads as "no school day"). The
# words a matched phrase covers are set aside, and if any CONTEXT_WORDS entry
# remains among the other words the summary is UNKNOWN. Otherwise a summary
# matching exactly one adjusted kind (no school, early release, late start)
# and no NORMAL phrase takes that kind; one matching several kinds, or an
# adjusted kind and a NORMAL phrase together ("Regular Day (no school
# Friday)"), is UNKNOWN; one matching only NORMAL phrases is NORMAL; one
# matching nothing is UNKNOWN. Plain "holiday" is deliberately absent:
# "Holiday Concert" is not a closure. Bare "bell schedule" is absent too:
# "Modified Bell Schedule" is not a regular day.
# #VERIFY: before production, run the deployment's real calendars through
# classify_summary and review every UNKNOWN and every NORMAL day against the
# published school calendar; extend this table (never a fallback to NORMAL)
# for phrases the district actually uses.
SCHOOL_DAY_KEYWORDS: Final[Mapping[SchoolDayKind, tuple[str, ...]]] = {
    SchoolDayKind.NO_SCHOOL: (
        "no school",
        "no school for students",
        "no school for all students",
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
        "normal bell schedule",
        "school day",
        "full day",
        "regular bells",
    ),
}
"""The one classification table: summary phrases for each school-day kind."""

# #ASSUME: external resources: a summary that qualifies its phrase (negates,
# cancels, bounds in time, limits to some students, reschedules) uses one of
# these words or word pairs outside the matched phrase. Words inside a matched
# phrase never count, so "No School" and "School Closed" still classify while
# "No early release", "Winter Break Ends" and "School Closed Early" do not.
# The list errs wide on purpose: an entry here can only turn a day UNKNOWN
# (a SCHOOL-009 warning), never into a closure.
# #VERIFY: with the keyword table, run the deployment's real calendars through
# classify_summary and review every UNKNOWN; widen SCHOOL_DAY_KEYWORDS with
# the district's exact phrase rather than removing a context word.
CONTEXT_WORDS: Final = frozenset(
    {
        # negation and cancellation
        "no",
        "not",
        "non",
        "without",
        "cancelled",
        "canceled",
        "closed",
        "closure",
        "lifted",
        "open",
        "except",
        "unless",
        "instead",
        "rescheduled",
        "postponed",
        # a boundary of a period rather than a day inside it
        "resume",
        "resumes",
        "resuming",
        "end",
        "ends",
        "ending",
        "before",
        "after",
        "back",
        "return",
        "returns",
        "make up",
        "makeup",
        "in session",
        # a partial day or only some students
        "early",
        "late",
        "for",
        "only",
        "attend",
        "attends",
    }
)
"""Words (or word pairs) that, outside the matched phrase, make a summary
UNKNOWN. Entries are normalized like summaries."""

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_BELL = "bell"


class SnapshotMappingError(ValidationError):
    """The payload's calendars do not match the configured schools.

    The error code is ``UNKNOWN_CALENDAR``, ``MISSING_CALENDAR``,
    ``NO_SCHOOLS`` or ``INVALID_TIME`` (a time outside the representable
    range); the message never names a calendar entity or echoes a value.
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


def _occurrences(tokens: Sequence[str], phrase: str) -> Iterator[range]:
    """Yield the token positions of each whole-word occurrence of ``phrase``."""
    words = phrase.split()
    size = len(words)
    for at in range(len(tokens) - size + 1):
        if list(tokens[at : at + size]) == words:
            yield range(at, at + size)


def classify_summary(summary: str) -> SchoolDayKind:
    """Classify one event summary with :data:`SCHOOL_DAY_KEYWORDS`.

    Args:
        summary: The event title.

    Returns:
        The one kind the summary names, or UNKNOWN when it names none or
        several, or when a :data:`CONTEXT_WORDS` entry qualifies it.
    """
    tokens = _normalize(summary).split()
    found = [
        (kind, set(positions))
        for kind, phrases in SCHOOL_DAY_KEYWORDS.items()
        for phrase in phrases
        for positions in _occurrences(tokens, phrase)
    ]
    covered: set[int] = set()
    adjusted: set[int] = set()
    for kind, positions in found:
        covered |= positions
        if kind is not SchoolDayKind.NORMAL:
            adjusted |= positions
    # A NORMAL phrase sharing a word with an adjusted phrase is part of it, not
    # a second reading: "No-School Day" reads "no school" + "school day".
    matched = {
        kind
        for kind, positions in found
        if kind is not SchoolDayKind.NORMAL or adjusted.isdisjoint(positions)
    }
    qualified = any(
        not covered.issuperset(positions)
        for word in CONTEXT_WORDS
        for positions in _occurrences(tokens, word)
    )
    if qualified or len(matched) != 1:
        return SchoolDayKind.UNKNOWN
    return matched.pop()


@dataclass(frozen=True, slots=True)
class _Mark:
    """One event's contribution to one local day."""

    kind: SchoolDayKind
    bell_span: tuple[datetime, datetime] | None
    full_day: bool


def _as_day(value: date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _utc_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _Bounds:
    """The window as local days and as UTC instants (end exclusive)."""

    window: DayWindow
    tz: ZoneInfo
    start: datetime
    end: datetime

    @classmethod
    def of(cls, window: DayWindow, tz: ZoneInfo) -> _Bounds:
        """Compute the UTC bounds; raises OverflowError at the edge of time."""
        return cls(
            window,
            tz,
            _utc_midnight(window.first, tz),
            _utc_midnight(window.last + timedelta(days=1), tz),
        )


def _days_between(first: date, last: date) -> Iterator[date]:
    for offset in range((last - first).days + 1):
        yield first + timedelta(days=offset)


def _event_marks(
    event: HaCalendarEvent, role: str, bounds: _Bounds
) -> Iterator[tuple[date, _Mark]]:
    """Yield the event's mark for each local day it covers inside the window.

    The event is clipped to the window before any day is listed, so the work
    is bounded by the window, never by the event's length.
    """
    # #CRITICAL: security: event bounds are untrusted. Clipping first (by
    # comparison, which cannot overflow) keeps a 0001 to 9999 event to the
    # window's few days and keeps every time zone conversion inside it.
    # #VERIFY: tests/unit/test_ha_school_days.py::TestExtremeEvents.
    kind = classify_summary(event.summary)
    start, end = event.start, event.end
    window = bounds.window
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        first = max(_as_day(start), window.first)
        last = min(_as_day(end) - timedelta(days=1), window.last)
        for day in _days_between(first, last) if first <= last else ():
            yield day, _Mark(kind, None, full_day=True)
        return
    if end == start:
        overlaps = bounds.start <= start < bounds.end
    else:
        overlaps = start < bounds.end and end > bounds.start
    if not overlaps:
        return
    clipped_start, clipped_end = max(start, bounds.start), min(end, bounds.end)
    tz = bounds.tz
    first = clipped_start.astimezone(tz).date()
    # #EDGE: timing: the end is exclusive, so an event ending exactly at local
    # midnight does not touch the next day. Subtracting a microsecond from the
    # UTC instant (not the wall clock) keeps this right across DST changes.
    # #VERIFY: TestMultiDayAndDst covers midnight ends and the fall-back day.
    last = first
    if clipped_end > clipped_start:
        last_instant = clipped_end.astimezone(UTC) - timedelta(microseconds=1)
        last = last_instant.astimezone(tz).date()
    span: tuple[datetime, datetime] | None = None
    if (
        role == _BELL
        and end > start
        and first == last
        and (clipped_start, clipped_end) == (start, end)
    ):
        span = (start.astimezone(UTC), end.astimezone(UTC))
    for day in _days_between(first, last):
        full_day = start <= _utc_midnight(day, tz) and end >= _utc_midnight(
            day + timedelta(days=1), tz
        )
        yield day, _Mark(kind, span, full_day=full_day)


def _decide_kind(marks: Sequence[_Mark]) -> SchoolDayKind:
    kinds = {mark.kind for mark in marks}
    if SchoolDayKind.UNKNOWN in kinds:
        return SchoolDayKind.UNKNOWN
    if SchoolDayKind.NO_SCHOOL in kinds:
        # #ASSUME: data integrity: only a closure covering the whole local day
        # (an all-day event, or a timed one from midnight to midnight)
        # overrides a regular bell event; a timed closure is a partial day
        # ("School Closed" 12:00 to 15:00) and is UNKNOWN, not NO_SCHOOL.
        # #VERIFY: TestNoSchool covers both, beside a bell day and alone.
        others = kinds - {SchoolDayKind.NO_SCHOOL, SchoolDayKind.NORMAL}
        partial = any(
            not mark.full_day for mark in marks if mark.kind is SchoolDayKind.NO_SCHOOL
        )
        if others or partial:
            return SchoolDayKind.UNKNOWN
        return SchoolDayKind.NO_SCHOOL
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
    kind = _decide_kind(marks)
    midnight = _utc_midnight(day, tz)
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
        SnapshotMappingError: If no school is configured, the payload's
            calendars are not exactly the configured ones, or a time falls
            outside the representable range (``INVALID_TIME``).
    """
    # #CRITICAL: security: only calendars named in FL_HA_SCHOOLS may enter;
    # anything else (a personal calendar exposed by mistake) is refused, and
    # the error never names the entity.
    # #VERIFY: TestEntities asserts the refusal and the absence of the ID.
    problem = _entity_problem(snapshot, schools)
    if problem is not None:
        code, msg = problem
        raise SnapshotMappingError(msg, error_code=code)
    try:
        rows = _map_rows(snapshot, schools, _Bounds.of(window, tz))
    except OverflowError as exc:
        msg = "a snapshot time is outside the representable range"
        raise SnapshotMappingError(msg, error_code="INVALID_TIME") from exc
    return tuple(sorted(rows, key=lambda row: (row.date, row.person_id)))


def _map_rows(
    snapshot: HaSnapshotPayload, schools: Sequence[HaSchool], bounds: _Bounds
) -> list[SchoolDay]:
    rows: list[SchoolDay] = []
    for school in schools:
        by_day: defaultdict[date, list[_Mark]] = defaultdict(list)
        for entity, role in school.entities().items():
            for event in snapshot.calendars[entity].events:
                for day, mark in _event_marks(event, role, bounds):
                    by_day[day].append(mark)
        for day, marks in by_day.items():
            kind, start, end = _decide_day(day, marks, bounds.tz)
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
    return rows
