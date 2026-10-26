"""Deterministic mapping of HA calendar events to school days (ADR 0007).

Pure functions: the same payload, schools, zone and window always give the
same result, and nothing here reads a clock, the database or the network.

Each event that touches the window is classified by its summary
(:data:`SCHOOL_DAY_KEYWORDS`, :data:`CONTEXT_WORDS` and :data:`NEUTRAL_WORDS`)
and by the local days it covers: a weekday or date the summary names must be
one of the event's own days and must name its first and last day, a clock
time is allowed only beside an early release or a late start, and any other
number makes the summary UNKNOWN. Each school's events are then clipped to
the window, grouped by local day and decided:

* Kind: any unrecognized event makes the day UNKNOWN; a closure that does not
  cover the whole local day, or a closure beside anything but regular-day
  events, or an early release beside a late start, is UNKNOWN; otherwise the
  one adjusted kind wins over NORMAL.
* Times: only a bell-calendar timed event of positive length that falls
  within one local day and lies wholly inside the window supplies bell times.
  A NORMAL, EARLY_RELEASE or LATE_START day needs at least one such event of
  its own kind, or it becomes UNKNOWN. An EARLY_RELEASE or LATE_START day
  needs exactly one distinct span of its kind and a NORMAL day's spans (bell
  periods) must not overlap, or the day is UNKNOWN; the times are the span of
  those events. An UNKNOWN day spans every bell timed event of the day, else
  it is a zero-length row at local midnight. A NO_SCHOOL day is a zero-length
  row at local midnight.
* A day without events has no row. The engine reports a missing row as
  SCHOOL-009 only for a child with an applicable transport default, so
  :class:`SchoolDayMapping` names every school that received no row at all
  and leaves the decision to the caller.

An event is never assumed to be a normal school day: a summary the table
does not recognize, one qualified by a context word, or one with any word the
tables do not account for, yields UNKNOWN.

The timeline builds no school block from a zero-length row, so an UNKNOWN day
without bell times loses its school-based move checks just as a closure does;
the SCHOOL-009 warning every UNKNOWN day raises is then its only signal.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from itertools import combinations
from typing import TYPE_CHECKING, Final

from family_logistics.core.exceptions import ValidationError
from family_logistics.domain.context import HA_SNAPSHOT_SOURCE
from family_logistics.domain.events import SchoolDay, SchoolDayKind

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping, Sequence
    from zoneinfo import ZoneInfo

    from family_logistics.adapters.home_assistant.schemas import (
        HaCalendarEvent,
        HaSnapshotPayload,
    )
    from family_logistics.core.config import HaSchool

__all__ = [
    "CONTEXT_WORDS",
    "MAX_WINDOW_DAYS",
    "NEUTRAL_WORDS",
    "SCHOOL_DAY_KEYWORDS",
    "DayWindow",
    "SchoolDayMapping",
    "SnapshotMappingCode",
    "SnapshotMappingError",
    "classify_summary",
    "map_school_snapshot",
]

# #CRITICAL: data integrity: a false NO_SCHOOL is the dangerous direction (the
# engine builds no school block and suppresses transport defaults, so a child
# drops out of the checks on a real school day). Every ambiguity therefore
# resolves to UNKNOWN, which raises SCHOOL-009. An UNKNOWN day without bell
# times is still a zero-length row that the timeline skips like a closure, so
# for that day SCHOOL-009 is the only signal that its school moves went
# unchecked.
# #VERIFY: tests/unit/test_ha_school_days.py::TestClassifySummary and
# TestDatedSummaries pin the reviewed summaries, TestNoSchool the closure
# rules; the engine owner confirms that SCHOOL-009 alone meets design rule 4
# for a zero-length UNKNOWN day.

# #ASSUME: external resources: school and district calendars title their
# events in English with one of these phrases. Matching is on whole words of
# the summary after NFKC normalization, case folding and replacing every
# non-word character with a space ("No-School Day" reads as "no school day").
# The words a matched phrase covers are set aside. The summary is UNKNOWN if
# any CONTEXT_WORDS entry remains among the other words, if any remaining word
# is neither neutral (NEUTRAL_WORDS) nor part of a weekday, date or clock time
# the event confirms, or if two matched phrases overlap without one containing
# the other. A NORMAL phrase that shares a word with an adjusted phrase is part
# of it and is ignored ("No-School Day" is NO_SCHOOL). Otherwise a summary
# matching exactly one adjusted kind (no school, early release, late start)
# takes that kind; one matching several adjusted kinds, or an adjusted kind
# and a separate NORMAL phrase ("Regular Day (no school Friday)"), is UNKNOWN;
# one matching only NORMAL phrases is NORMAL; one matching nothing is UNKNOWN.
# Plain "holiday" is deliberately absent: "Holiday Concert" is not a closure.
# Bare "bell schedule" is absent too: "Modified Bell Schedule" is not a
# regular day.
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
# (a SCHOOL-009 warning), never into a closure. It is a second line behind the
# NEUTRAL_WORDS allow-list: it still holds if one of these words is ever made
# neutral by mistake.
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

# #ASSUME: external resources: outside the matched phrases, a plain closure,
# short-day or regular-day title adds only filler, the name of the holiday, a
# weekday or date, or a clock time ("No School - Labor Day", "Early Release
# 12:30 PM", "Winter Break Dec 22 - Jan 2"). Every other leftover word makes
# the summary UNKNOWN: a veto list can never name every qualifier ("Tentative",
# "Grades 6-8", "Celebration", "Summer School"), so the default is inverted and
# only these reviewed words are allowed to remain. Weekdays, dates and clock
# times are not in this table: they are read separately and must agree with
# the event (a weekday or date must be one of the event's own days, a clock
# time only qualifies an early release or a late start). A bare number or
# ordinal ("No School 6-8", "No School 9th") names grades as often as a day,
# so it is never neutral. "break", "holiday", "school", "student" and the
# season names are deliberately absent: the phrase table covers them where
# they name a closure, and outside a phrase they name another program ("No
# School Summer School") or let "Regular Day Holiday" read as a regular day.
# #VERIFY: before the shadow period, run the deployment's real calendars
# through classify_summary and review every UNKNOWN; add a word here only
# when it cannot change which day kind a title names, for which students, or
# on which day.
NEUTRAL_WORDS: Final = frozenset(
    {
        # filler
        "a",
        "an",
        "the",
        "of",
        "and",
        "or",
        "all",
        "day",
        "days",
        # holiday names
        "labor",
        "columbus",
        "indigenous",
        "peoples",
        "veterans",
        "thanksgiving",
        "christmas",
        "new",
        "year",
        "years",
        "eve",
        "martin",
        "luther",
        "king",
        "jr",
        "dr",
        "mlk",
        "presidents",
        "president",
        "memorial",
        "independence",
        "juneteenth",
        "easter",
        "good",
        # possessive fragment ("New Year's" reads "new year s")
        "s",
    }
)
"""Single words that may remain beside a matched phrase without making the
summary UNKNOWN. Weekdays, dates and clock times are checked separately."""

MAX_WINDOW_DAYS: Final = 128
"""Most local days one mapping covers; the work is events times window days."""

_SEPARATOR = re.compile(r"\W+")
_CLOCK = re.compile(
    r"""
    (?<![\w:/])
    (?: (?P<hour>[0-9]{1,2}) (?: : (?P<minute>[0-9]{2}) )?
      | (?P<compact>[0-9]{3,4}) )
    \s* [ap] \.? \s? m \b \.?
    | \b noon \b
    """,
    re.VERBOSE,
)
_NUMERIC_DATE = re.compile(
    r"""
    (?<![\w/:])
    (?P<month>[0-9]{1,2}) / (?P<day>[0-9]{1,2}) (?: / (?P<year>[0-9]{4}|[0-9]{2}) )?
    (?![\w/])
    """,
    re.VERBOSE,
)
_DAY_NUMBER = re.compile(r"(?P<day>[0-9]{1,2})(?:st|nd|rd|th)?")
_WEEKDAYS: Final[Mapping[str, int]] = {
    name: index
    for index, names in enumerate(
        (
            ("monday", "mon"),
            ("tuesday", "tue", "tues"),
            ("wednesday", "wed", "weds"),
            ("thursday", "thu", "thur", "thurs"),
            ("friday", "fri"),
            ("saturday", "sat"),
            ("sunday", "sun"),
        )
    )
    for name in names
}
_MONTHS: Final[Mapping[str, int]] = {
    name: index
    for index, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_TIMED_KINDS: Final = frozenset({SchoolDayKind.EARLY_RELEASE, SchoolDayKind.LATE_START})
_MAX_DATED_SPAN_DAYS = 366
_LAST_HOUR = 12
_LAST_MINUTE = 59
_LAST_MONTH = 12
_LAST_DAY = 31
_BELL = "bell"


class SnapshotMappingCode(StrEnum):
    """Why a snapshot cannot be mapped (the ``error_code`` of the error)."""

    NO_SCHOOLS = "NO_SCHOOLS"
    DUPLICATE_PERSON = "DUPLICATE_PERSON"
    UNKNOWN_CALENDAR = "UNKNOWN_CALENDAR"
    MISSING_CALENDAR = "MISSING_CALENDAR"
    INVALID_TIME = "INVALID_TIME"


class SnapshotMappingError(ValidationError):
    """The payload's calendars do not match the configured schools.

    The error code is a :class:`SnapshotMappingCode` value (``INVALID_TIME``
    is a time outside the representable range); the message never names a
    calendar entity or a person, or echoes a value.
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
        """Refuse instants, an inverted window and an over-long one.

        Raises:
            ValidationError: If a bound is a datetime, ``last`` is before
                ``first``, or the window holds more than
                :data:`MAX_WINDOW_DAYS` days.
        """
        for name in ("first", "last"):
            if isinstance(getattr(self, name), datetime):
                msg = "the snapshot window takes local days, not instants"
                raise ValidationError(msg, field=name)
        if self.last < self.first:
            msg = "the snapshot window ends before it starts"
            raise ValidationError(msg, field="last")
        if (self.last - self.first).days >= MAX_WINDOW_DAYS:
            msg = "the snapshot window holds too many days"
            raise ValidationError(msg, field="last")

    def __contains__(self, day: object) -> bool:
        """Whether ``day`` is a date (not a datetime) inside the window.

        Args:
            day: The value tested.

        Returns:
            True for a date from ``first`` to ``last`` inclusive.
        """
        return (
            isinstance(day, date)
            and not isinstance(day, datetime)
            and self.first <= day <= self.last
        )


@dataclass(frozen=True, slots=True)
class SchoolDayMapping:
    """The rows one snapshot yields, and what the caller must know about them.

    A quiet period (summer) and an outage both leave a school without a row;
    the mapper cannot tell them apart, so it never raises for either and names
    the school instead. Every field is a count or a configured identifier, so
    the whole object is safe to log.

    Attributes:
        days: One row per person per local day with an event, sorted by day
            then person.
        schools_without_days: The configured schools, in configuration order,
            that received no row at all.
        events_total: Events in the snapshot's calendars.
        events_in_window: Events that touched at least one day of the window.
    """

    days: tuple[SchoolDay, ...]
    schools_without_days: tuple[HaSchool, ...]
    events_total: int
    events_in_window: int

    @property
    def is_empty(self) -> bool:
        """Whether no school received any row."""
        return not self.days

    def kind_counts(self) -> dict[SchoolDayKind, int]:
        """Count the rows of each kind.

        Returns:
            Rows per kind; a kind without rows is absent.
        """
        return dict(Counter(day.kind for day in self.days))


@dataclass(frozen=True, slots=True)
class _Reading:
    """What a summary's words say before the event's own days are known."""

    kind: SchoolDayKind
    dates: frozenset[tuple[int, int, int | None]]
    weekdays: frozenset[int]
    clock: bool


def _fold(text: str) -> str:
    """Apply NFKC and case folding.

    NFKC composes accents, so a letter outside ASCII stays inside its word
    ("Scho\u00f3l") and makes that word unknown instead of vanishing, and it
    folds full-width letters to their ASCII forms.

    Args:
        text: Raw summary.

    Returns:
        The folded text.
    """
    return unicodedata.normalize("NFKC", text).casefold()


def _words(text: str) -> list[str]:
    """Split folded text on every non-word character.

    Args:
        text: Folded text.

    Returns:
        The words; punctuation, symbols and emoji are separators.
    """
    return _SEPARATOR.sub(" ", text).split()


def _tangled(occurrences: Sequence[set[int]]) -> bool:
    """Whether two phrase occurrences share a word without one holding the other.

    "No School Closed" reads "no school" and "school closed"; each phrase hides
    the other's context word, so the reading is ambiguous.

    Args:
        occurrences: Token positions of each kept phrase occurrence.

    Returns:
        True when two occurrences cross.
    """
    return any(
        a & b and not (a <= b or b <= a) for a, b in combinations(occurrences, 2)
    )


def _occurrences(tokens: Sequence[str], phrase: str) -> Iterator[range]:
    """Yield the token positions of each whole-word occurrence of ``phrase``.

    Args:
        tokens: The summary's words.
        phrase: A normalized phrase.

    Yields:
        range: The positions one occurrence covers.
    """
    words = phrase.split()
    size = len(words)
    for at in range(len(tokens) - size + 1):
        if list(tokens[at : at + size]) == words:
            yield range(at, at + size)


def _take_clock_times(text: str) -> tuple[str, bool]:
    """Remove every valid 12-hour clock time from folded text.

    "12:30 p.m.", "1230PM", "9am" and "noon" are clock times; an invalid one
    ("13:75 pm") stays in the text, so its digits make the summary UNKNOWN.

    Args:
        text: Folded text.

    Returns:
        The text without its clock times, and whether it had one.
    """
    found = False

    def take(match: re.Match[str]) -> str:
        nonlocal found
        compact = match["compact"]
        if compact is not None:
            hour, minute = int(compact[:-2]), int(compact[-2:])
        elif match["hour"] is not None:
            hour, minute = int(match["hour"]), int(match["minute"] or 0)
        else:
            hour, minute = _LAST_HOUR, 0
        if not (1 <= hour <= _LAST_HOUR and minute <= _LAST_MINUTE):
            return match.group(0)
        found = True
        return " "

    return _CLOCK.sub(take, text), found


def _take_numeric_dates(
    text: str,
) -> tuple[str, set[tuple[int, int, int | None]]]:
    """Remove every month/day date ("11/26", "11/26/27") from folded text.

    A slash is required: "6-8" names grades as often as a date and is left in
    place, so its digits make the summary UNKNOWN.

    Args:
        text: Folded text.

    Returns:
        The text without its dates, and each date as (month, day, year).
    """
    found: set[tuple[int, int, int | None]] = set()

    def take(match: re.Match[str]) -> str:
        month, day = int(match["month"]), int(match["day"])
        if not (1 <= month <= _LAST_MONTH and 1 <= day <= _LAST_DAY):
            return match.group(0)
        year = match["year"]
        full = None if year is None else int(year) + (2000 if len(year) == 2 else 0)
        found.add((month, day, full))
        return " "

    return _NUMERIC_DATE.sub(take, text), found


def _day_number(token: str) -> int | None:
    """Return the day of month a token names ("22", "27th"), if any.

    Args:
        token: One word.

    Returns:
        The day from 1 to 31, or None.
    """
    match = _DAY_NUMBER.fullmatch(token)
    if match is None:
        return None
    day = int(match["day"])
    return day if 1 <= day <= _LAST_DAY else None


def _read_dates(
    tokens: Sequence[str], covered: set[int]
) -> tuple[set[int], set[tuple[int, int, int | None]], set[int]]:
    """Read weekdays and month-day dates among the words outside the phrases.

    A month name is a date only when a day number follows it ("Dec 22",
    "Nov. 25-27", "Jan 27th"); a bare month is not accounted for.

    Args:
        tokens: The summary's words.
        covered: Positions inside matched phrases.

    Returns:
        The positions read, the dates as (month, day, None) and the weekdays.
    """
    read: set[int] = set()
    dates: set[tuple[int, int, int | None]] = set()
    weekdays: set[int] = set()
    at = 0
    while at < len(tokens):
        token = tokens[at]
        if at not in covered and token in _WEEKDAYS:
            weekdays.add(_WEEKDAYS[token])
            read.add(at)
        elif at not in covered and token in _MONTHS:
            nxt = at + 1
            while nxt < len(tokens) and nxt not in covered:
                day = _day_number(tokens[nxt])
                if day is None:
                    break
                dates.add((_MONTHS[token], day, None))
                read.add(nxt)
                nxt += 1
            if nxt > at + 1:
                read.add(at)
                at = nxt
                continue
        at += 1
    return read, dates, weekdays


def _read(summary: str) -> _Reading:
    """Read a summary's kind, dates, weekdays and clock times.

    Args:
        summary: The event title.

    Returns:
        The reading; its kind is UNKNOWN when the words alone are ambiguous.
    """
    text, clock = _take_clock_times(_fold(summary))
    text, dates = _take_numeric_dates(text)
    tokens = _words(text)
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
    kept = [
        (kind, positions)
        for kind, positions in found
        if kind is not SchoolDayKind.NORMAL or adjusted.isdisjoint(positions)
    ]
    matched = {kind for kind, _ in kept}
    qualified = any(
        not covered.issuperset(positions)
        for word in CONTEXT_WORDS
        for positions in _occurrences(tokens, word)
    )
    dated, named, weekdays = _read_dates(tokens, covered)
    # #CRITICAL: data integrity: the default is inverted. A leftover word that
    # is not reviewed as neutral ("Tentative", "Drill", "Grades", "6", "9th")
    # makes the summary UNKNOWN, so a qualifier nobody listed can never yield
    # a false NO_SCHOOL. The CONTEXT_WORDS veto stays as a second line.
    # #VERIFY: the TestClassifySummary cases for stray words and numbers, the
    # context-veto case that makes a context word neutral, and the realistic
    # plain-summary corpus that must keep its kind.
    stray = any(
        at not in covered and at not in dated and token not in NEUTRAL_WORDS
        for at, token in enumerate(tokens)
    )
    tangled = _tangled([positions for _, positions in kept])
    kind = SchoolDayKind.UNKNOWN
    if not (qualified or stray or tangled or len(matched) != 1):
        kind = matched.pop()
    return _Reading(kind, frozenset(dates | named), frozenset(weekdays), clock)


def _resolve(
    named: tuple[int, int, int | None], first: date, last: date
) -> date | None:
    """Return the date inside ``first``..``last`` that a month and day name.

    Args:
        named: Month, day and (if given) year.
        first: First local day of the event.
        last: Last local day of the event.

    Returns:
        The date, or None when no such date lies inside the event.
    """
    month, day, year = named
    years = (year,) if year is not None else range(first.year, last.year + 1)
    for candidate in years:
        try:
            hit = date(candidate, month, day)
        except ValueError:
            continue
        if first <= hit <= last:
            return hit
    return None


def _names_its_days(reading: _Reading, span: tuple[date, date] | None) -> bool:
    """Whether the summary's weekdays and dates agree with the event's days.

    Every named date must be one of the event's days, every named weekday the
    weekday of one of them, and together they must name the event's first and
    last day: "No School Monday" on a Thursday, or on a Monday-to-Friday
    event, does not agree.

    Args:
        reading: The summary's reading.
        span: First and last local day of the event, or None when unknown.

    Returns:
        True when they agree.
    """
    if span is None:
        return False
    first, last = span
    length = (last - first).days + 1
    if not 1 <= length <= _MAX_DATED_SPAN_DAYS:
        return False
    named: set[date] = set()
    for named_date in reading.dates:
        hit = _resolve(named_date, first, last)
        if hit is None:
            return False
        named.add(hit)
    weekdays_held = {
        (first + timedelta(days=n)).weekday() for n in range(min(7, length))
    }
    if not reading.weekdays <= weekdays_held:
        return False
    return all(
        day in named or day.weekday() in reading.weekdays for day in (first, last)
    )


def _confirm(reading: _Reading, span: tuple[date, date] | None) -> SchoolDayKind:
    """Check a reading against the days its event covers.

    Args:
        reading: The summary's reading.
        span: First and last local day of the event, or None when unknown.

    Returns:
        The reading's kind, or UNKNOWN when the event disagrees with it.
    """
    kind = reading.kind
    if kind is SchoolDayKind.UNKNOWN:
        return kind
    # #CRITICAL: data integrity: a clock time beside a closure ("No School
    # 12:30 PM") means a partial day, and a weekday or date that is not the
    # event's own ("No School Monday" posted on Thursday) is an announcement
    # of another day; both are UNKNOWN, never a closure of the event's day.
    # #VERIFY: TestDatedSummaries covers matching, mismatched and missing
    # days, multi-day ranges and clock times beside each kind.
    if reading.clock and kind not in _TIMED_KINDS:
        return SchoolDayKind.UNKNOWN
    if (reading.dates or reading.weekdays) and not _names_its_days(reading, span):
        return SchoolDayKind.UNKNOWN
    return kind


def classify_summary(
    summary: str, span: tuple[date, date] | None = None
) -> SchoolDayKind:
    """Classify one event summary with :data:`SCHOOL_DAY_KEYWORDS`.

    Args:
        summary: The event title.
        span: First and last local day the event covers. Without it, a
            summary that names a weekday or a date cannot be confirmed and is
            UNKNOWN.

    Returns:
        The one kind the summary names, or UNKNOWN when it names none or
        several, when a :data:`CONTEXT_WORDS` entry qualifies it, when any
        word outside the matched phrases is neither neutral
        (:data:`NEUTRAL_WORDS`) nor part of a weekday, date or clock time,
        when two matched phrases overlap without one containing the other,
        when a clock time qualifies anything but an early release or a late
        start, or when a weekday or date it names disagrees with ``span``.
    """
    return _confirm(_read(summary), span)


@dataclass(frozen=True, slots=True)
class _Mark:
    """One event's contribution to one local day."""

    kind: SchoolDayKind
    bell_span: tuple[datetime, datetime] | None
    full_day: bool


def _as_day(value: date) -> date:
    """Return the calendar day of a date or datetime.

    Args:
        value: A date or datetime.

    Returns:
        The date part.
    """
    return value.date() if isinstance(value, datetime) else value


def _utc_midnight(day: date, tz: ZoneInfo) -> datetime:
    """Return the UTC instant of local midnight at the start of ``day``.

    Args:
        day: Local day.
        tz: Household zone.

    Returns:
        The instant in UTC.
    """
    return datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _Bounds:
    """The window as local days and as UTC midnights (end exclusive)."""

    window: DayWindow
    tz: ZoneInfo
    midnights: tuple[datetime, ...]

    @classmethod
    def of(cls, window: DayWindow, tz: ZoneInfo) -> _Bounds:
        """Compute each window day's UTC midnight once, plus the end.

        The date arithmetic and zone conversion propagate ``OverflowError``
        at the edge of representable time; ``map_school_snapshot`` turns it
        into ``INVALID_TIME``.

        Args:
            window: Local days kept.
            tz: Household zone.

        Returns:
            The bounds.
        """
        count = (window.last - window.first).days + 2
        midnights = tuple(
            _utc_midnight(window.first + timedelta(days=n), tz) for n in range(count)
        )
        return cls(window, tz, midnights)

    @property
    def start(self) -> datetime:
        """UTC midnight starting the first day."""
        return self.midnights[0]

    @property
    def end(self) -> datetime:
        """UTC midnight ending the last day (exclusive)."""
        return self.midnights[-1]

    def midnight(self, day: date) -> datetime:
        """Return the UTC midnight starting ``day`` (a window day or the next).

        Args:
            day: Local day from ``first`` to ``last`` plus one.

        Returns:
            The precomputed instant.
        """
        return self.midnights[(day - self.window.first).days]


def _days_between(first: date, last: date) -> Iterator[date]:
    """Yield every day from ``first`` to ``last`` inclusive.

    Args:
        first: First day.
        last: Last day.

    Yields:
        date: Each day in order.
    """
    for offset in range((last - first).days + 1):
        yield first + timedelta(days=offset)


def _event_span(event: HaCalendarEvent, tz: ZoneInfo) -> tuple[date, date] | None:
    """Return the first and last local day of the whole (unclipped) event.

    Args:
        event: The event.
        tz: Household zone.

    Returns:
        The days, or None for an event longer than a dated summary can name.
    """
    start, end = event.start, event.end
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        first, last = _as_day(start), _as_day(end) - timedelta(days=1)
    else:
        if end - start > timedelta(days=_MAX_DATED_SPAN_DAYS):
            return None
        first = start.astimezone(tz).date()
        last = first
        if end > start:
            last_instant = end.astimezone(UTC) - timedelta(microseconds=1)
            last = last_instant.astimezone(tz).date()
    if (last - first).days >= _MAX_DATED_SPAN_DAYS:
        return None
    return first, last


def _reading(readings: dict[str, _Reading], summary: str) -> _Reading:
    """Read a summary once per mapping; identical titles repeat in calendars.

    Args:
        readings: This mapping's readings by summary.
        summary: The event title.

    Returns:
        The reading.
    """
    reading = readings.get(summary)
    if reading is None:
        reading = readings[summary] = _read(summary)
    return reading


def _event_marks(
    event: HaCalendarEvent,
    role: str,
    bounds: _Bounds,
    readings: dict[str, _Reading],
) -> Iterator[tuple[date, _Mark]]:
    """Yield the event's mark for each local day it covers inside the window.

    The event is clipped to the window before any day is listed or its
    summary is read, so the work is bounded by the window, never by the
    event's length.

    Args:
        event: The event.
        role: The calendar's role, ``"bell"`` or ``"district"``.
        bounds: The window.
        readings: This mapping's readings by summary.

    Yields:
        tuple[date, _Mark]: A local day and the event's mark on it.
    """
    # #CRITICAL: security: event bounds are untrusted. Clipping first (by
    # comparison, which cannot overflow) keeps a 0001 to 9999 event, which
    # the schema's year bounds refuse but a caller bypassing validation could
    # still pass, to the window's few days and keeps every time zone
    # conversion inside it.
    # #VERIFY: tests/unit/test_ha_school_days.py::TestExtremeEvents.
    start, end = event.start, event.end
    window = bounds.window
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        first = max(_as_day(start), window.first)
        last = min(_as_day(end) - timedelta(days=1), window.last)
        if first > last:
            return
        reading = _reading(readings, event.summary)
        kind = _confirm(reading, _event_span(event, bounds.tz))
        for day in _days_between(first, last):
            yield day, _Mark(kind, None, full_day=True)
        return
    if end == start:
        overlaps = bounds.start <= start < bounds.end
    else:
        overlaps = start < bounds.end and end > bounds.start
    if not overlaps:
        return
    tz = bounds.tz
    kind = _confirm(_reading(readings, event.summary), _event_span(event, tz))
    clipped_start, clipped_end = max(start, bounds.start), min(end, bounds.end)
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
        full_day = start <= bounds.midnight(day) and end >= bounds.midnight(
            day + timedelta(days=1)
        )
        yield day, _Mark(kind, span, full_day=full_day)


def _decide_kind(marks: Collection[_Mark]) -> SchoolDayKind:
    """Decide one day's kind from the marks of every event on it.

    Args:
        marks: The day's distinct marks.

    Returns:
        UNKNOWN for any unrecognized event, a partial or mixed closure, or an
        early release beside a late start; else the one adjusted kind, or
        NORMAL.
    """
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


def _span(spans: Collection[tuple[datetime, datetime]]) -> tuple[datetime, datetime]:
    """Return the earliest start and latest end of the spans.

    Args:
        spans: Bell spans (at least one).

    Returns:
        The covering span.
    """
    return min(s for s, _ in spans), max(e for _, e in spans)


def _spans_agree(
    kind: SchoolDayKind, spans: Collection[tuple[datetime, datetime]]
) -> bool:
    """Whether a day's own bell spans give one trustworthy schedule.

    Args:
        kind: The day's kind.
        spans: The distinct bell spans of that kind.

    Returns:
        For a short or late day, True only for exactly one span (its times
        drive the decision); for a NORMAL day, True when no two bell periods
        overlap (a stale and an updated event overlap).
    """
    if kind in _TIMED_KINDS:
        return len(spans) == 1
    return not any(a[0] < b[1] and b[0] < a[1] for a, b in combinations(spans, 2))


def _decide_day(
    marks: Collection[_Mark], midnight: datetime
) -> tuple[SchoolDayKind, datetime, datetime]:
    """Decide one school's kind and bell times for one local day.

    Args:
        marks: The day's distinct marks.
        midnight: UTC instant of the day's local midnight.

    Returns:
        The kind, start and end.
    """
    kind = _decide_kind(marks)
    if kind is SchoolDayKind.NO_SCHOOL:
        return kind, midnight, midnight
    if kind is not SchoolDayKind.UNKNOWN:
        own = {m.bell_span for m in marks if m.kind is kind and m.bell_span}
        if own and _spans_agree(kind, own):
            return (kind, *_span(own))
        # #ASSUME: data integrity: a classified day without a bell event of
        # its own kind has no trustworthy times (a district "Early Release"
        # beside a stale regular bell schedule), and two conflicting bell
        # events of its kind (a stale and an updated "Early Release") give two
        # dismissal times, so either is reported UNKNOWN rather than given the
        # wrong times.
        # #VERIFY: TestAdjustedDays and TestNormalDay cover both downgrades.
        kind = SchoolDayKind.UNKNOWN
    spans = {m.bell_span for m in marks if m.bell_span}
    if spans:
        return (kind, *_span(spans))
    return kind, midnight, midnight


def _entity_problem(
    snapshot: HaSnapshotPayload, schools: Sequence[HaSchool]
) -> tuple[SnapshotMappingCode, str] | None:
    """Return the error code and message when the input cannot be mapped.

    Args:
        snapshot: The validated payload.
        schools: The configured schools.

    Returns:
        The code and message, or None. The message never names a calendar
        entity or a person.
    """
    if not schools:
        return (
            SnapshotMappingCode.NO_SCHOOLS,
            "no school is configured for the Home Assistant snapshot",
        )
    people = [person for school in schools for person in school.person_ids]
    if len(set(people)) != len(people):
        return (
            SnapshotMappingCode.DUPLICATE_PERSON,
            "a person is configured for more than one school",
        )
    configured = {entity for school in schools for entity in school.entities()}
    supplied = set(snapshot.calendars)
    if supplied - configured:
        return (
            SnapshotMappingCode.UNKNOWN_CALENDAR,
            "the snapshot holds a calendar that is not configured",
        )
    if configured - supplied:
        return (
            SnapshotMappingCode.MISSING_CALENDAR,
            "the snapshot is missing a configured calendar",
        )
    return None


def map_school_snapshot(
    snapshot: HaSnapshotPayload,
    schools: Sequence[HaSchool],
    *,
    tz: ZoneInfo,
    window: DayWindow,
) -> SchoolDayMapping:
    """Map a validated snapshot to one school day per person per local day.

    Args:
        snapshot: The validated payload.
        schools: The configured schools (``FL_HA_SCHOOLS``).
        tz: The household zone (``FL_TIMEZONE``) that defines a local day.
        window: The local days kept; events outside it are dropped.

    Returns:
        The rows, every school that received none, and event counts. A
        snapshot without a single row is a valid result, never an error: the
        caller decides what an empty school means.

    Raises:
        SnapshotMappingError: If no school is configured, a person is
            configured for two schools, the payload's calendars are not
            exactly the configured ones, or a time falls outside the
            representable range (``INVALID_TIME``).
    """
    # #CRITICAL: security: only calendars named in FL_HA_SCHOOLS may enter;
    # anything else (a personal calendar exposed by mistake) is refused, and
    # the error never names the entity.
    # #VERIFY: TestEntities asserts the refusal and the absence of the ID.
    problem = _entity_problem(snapshot, schools)
    if problem is not None:
        code, msg = problem
        raise SnapshotMappingError(msg, error_code=code.value)
    try:
        return _map(snapshot, schools, _Bounds.of(window, tz))
    except OverflowError as exc:
        msg = "a snapshot time is outside the representable range"
        raise SnapshotMappingError(
            msg, error_code=SnapshotMappingCode.INVALID_TIME.value
        ) from exc


def _map(
    snapshot: HaSnapshotPayload, schools: Sequence[HaSchool], bounds: _Bounds
) -> SchoolDayMapping:
    """Map every school's events inside the window.

    Args:
        snapshot: The validated payload.
        schools: The configured schools.
        bounds: The window.

    Returns:
        The mapping.
    """
    readings: dict[str, _Reading] = {}
    rows: list[SchoolDay] = []
    empty: list[HaSchool] = []
    touched: set[tuple[str, int]] = set()
    for school in schools:
        # Identical marks cannot change a decision, so each day keeps a set.
        by_day: defaultdict[date, set[_Mark]] = defaultdict(set)
        for entity, role in school.entities().items():
            for index, event in enumerate(snapshot.calendars[entity].events):
                for day, mark in _event_marks(event, role, bounds, readings):
                    by_day[day].add(mark)
                    touched.add((entity, index))
        if not by_day:
            empty.append(school)
        for day, marks in by_day.items():
            kind, start, end = _decide_day(marks, bounds.midnight(day))
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
    return SchoolDayMapping(
        days=tuple(sorted(rows, key=lambda row: (row.date, row.person_id))),
        schools_without_days=tuple(empty),
        events_total=sum(len(c.events) for c in snapshot.calendars.values()),
        events_in_window=len(touched),
    )
