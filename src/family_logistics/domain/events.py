"""Contract types shared by the collector, the registry and the engine.

These immutable records are the hand-off points between packages:

- The CalDAV collector (package 1) produces ``SourceEvent`` occurrences inside a
  ``CollectionResult`` for each ``CalendarSource``.
- The registry (package 2) supplies people, passengers, locations, vehicles and
  ``TransportDefault`` rows.
- The engine (package 3) consumes one ``AssessmentInput`` and never performs I/O.

All datetimes are timezone-aware UTC. Local-day reasoning uses the IANA zone in
``AssessmentInput.timezone``.

Occurrence identity is ``(calendar_id, uid, recurrence_id)``, where
``recurrence_id`` is the UTC ISO-8601 RECURRENCE-ID of an occurrence of a
recurring series and ``""`` for a single event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from family_logistics.domain.models import SourceEventKey

if TYPE_CHECKING:
    from datetime import date, datetime

    from family_logistics.domain.models import (
        ExternalPassenger,
        Finding,
        Location,
        Person,
        Trip,
        Vehicle,
    )

EventStatus = Literal["confirmed", "tentative", "cancelled"]
"""iCalendar STATUS values the collector keeps (others map to confirmed)."""

OccurrenceKey = tuple[str, str, str]
"""``(calendar_id, uid, recurrence_id)``; recurrence_id is ``""`` when absent."""


class CalendarKind(StrEnum):
    """What a calendar source contributes to an assessment."""

    TRANSPORT = "transport"
    """The shared transportation calendar holding ride events."""
    PERSON = "person"
    """An individual's calendar; its activities belong to ``person_id``."""
    SCHOOL_DISTRICT = "school_district"
    """District closures and short days (Home Assistant snapshot, M3)."""
    SCHOOL_BELL = "school_bell"
    """School-specific bell schedules (Home Assistant snapshot, M3)."""


class ActivityClassification(StrEnum):
    """Coarse kind of an activity, used by timeline and coverage rules."""

    SCHOOL = "school"
    SPORT = "sport"
    LESSON = "lesson"
    WORK = "work"
    OTHER = "other"


class TransportDirection(StrEnum):
    """Direction of a recurring school transport default."""

    TO_SCHOOL = "to_school"
    FROM_SCHOOL = "from_school"


class TransportMode(StrEnum):
    """How a person normally travels for a transport default."""

    BIKE = "bike"
    BUS = "bus"
    WALK = "walk"
    PARENT = "parent"
    CARPOOL = "carpool"


class SchoolDayKind(StrEnum):
    """Bell-schedule variant for one school day."""

    NORMAL = "normal"
    EARLY_RELEASE = "early_release"
    LATE_START = "late_start"
    NO_SCHOOL = "no_school"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceEvent:
    """One normalized calendar occurrence as read from CalDAV.

    Attributes:
        calendar_id: Opaque identifier of the source calendar (an href hash).
        uid: iCalendar UID of the series or single event.
        recurrence_id: UTC ISO-8601 RECURRENCE-ID, ``""`` for a single event.
        start: Occurrence start (UTC).
        end: Occurrence end (UTC).
        all_day: Whether the event is a date-only (all-day) event.
        summary: Event title.
        location: LOCATION text, if any.
        description: DESCRIPTION text (the notes), if any.
        status: Confirmed, tentative or cancelled.
        etag: CalDAV ETag of the calendar object, if known.
        raw_hash: SHA-256 hex of the normalized VEVENT for change detection.
        is_override: Whether the occurrence has its own RECURRENCE-ID VEVENT.
        series_uid_has_rrule: Whether the UID belongs to a recurring series.
        last_seen: When the collector last saw the occurrence (UTC).
    """

    calendar_id: str
    uid: str
    recurrence_id: str
    start: datetime
    end: datetime
    all_day: bool
    # Raw calendar text can hold addresses or other household detail, so it
    # never appears in repr() (and therefore never in logs or tracebacks).
    summary: str = field(repr=False)
    location: str | None = field(repr=False)
    description: str | None = field(repr=False)
    status: EventStatus
    etag: str | None
    raw_hash: str
    is_override: bool
    series_uid_has_rrule: bool
    last_seen: datetime

    @property
    def occurrence_key(self) -> OccurrenceKey:
        """Return the occurrence identity.

        Returns:
            ``(calendar_id, uid, recurrence_id)``.
        """
        return (self.calendar_id, self.uid, self.recurrence_id)

    def to_key(self) -> SourceEventKey:
        """Return the parser's ``SourceEventKey`` for this occurrence.

        Returns:
            The key, with ``recurrence_id`` None for a single event.
        """
        return SourceEventKey(self.calendar_id, self.uid, self.recurrence_id or None)


@dataclass(frozen=True, slots=True)
class CalendarSource:
    """A calendar the collector reads.

    Attributes:
        calendar_id: Opaque identifier (an href hash, never the raw href).
        display_name: Calendar name as shown in iCloud.
        kind: What the calendar contributes.
        person_id: Owner of an individual calendar; None otherwise.
        enabled: Whether the calendar is collected.
        last_success: Last complete collection (UTC), None if never.
        last_error: Last collection error message, None after a success.
    """

    calendar_id: str
    display_name: str
    kind: CalendarKind
    person_id: str | None = None
    enabled: bool = True
    last_success: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Outcome of collecting one calendar for one time window.

    A network or protocol failure yields ``complete=False`` with ``error`` set.
    It never means "the calendar is empty", so missing occurrences must not be
    treated as deleted unless the result is complete.

    Attributes:
        source: The calendar collected.
        events: Occurrences in the window.
        window_start: Window start (UTC).
        window_end: Window end (UTC).
        fetched_at: When the collection finished (UTC).
        complete: Whether every occurrence in the window was read.
        error: Error message when incomplete.
    """

    source: CalendarSource
    events: tuple[SourceEvent, ...]
    window_start: datetime
    window_end: datetime
    fetched_at: datetime
    complete: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Activity:
    """Where one or more people are expected to be, and when.

    An activity does not imply transportation; it places participants at a
    location for an interval.

    Attributes:
        activity_id: Stable identifier (derived from the source occurrence).
        participant_ids: Person IDs expected at the activity.
        location_id: Resolved registry location, None when unresolved.
        location_text: Location as written, for unresolved display.
        start: Start (UTC).
        end: End (UTC).
        source_key: Occurrence key of the source event.
        classification: Coarse activity kind.
        stays_until_end: Whether participants remain until ``end``.
    """

    activity_id: str
    participant_ids: tuple[str, ...]
    location_id: str | None
    location_text: str | None = field(repr=False)
    start: datetime
    end: datetime
    source_key: OccurrenceKey
    classification: ActivityClassification = ActivityClassification.OTHER
    stays_until_end: bool = True


@dataclass(frozen=True, slots=True)
class TransportDefault:
    """A recurring, effective-dated way a person travels to or from school.

    Calendar exceptions (a ride event on the day) take precedence.

    Attributes:
        person_id: Traveller.
        weekdays: Local weekdays the default applies (0 = Monday).
        direction: To or from school.
        mode: Bike, bus, walk, parent or carpool.
        origin_location_id: Start location.
        destination_location_id: End location.
        driver_id: Usual driver for parent or carpool modes.
        effective_from: First day the default applies (inclusive), optional.
        effective_to: Last day the default applies (inclusive), optional.
    """

    person_id: str
    weekdays: frozenset[int]
    direction: TransportDirection
    mode: TransportMode
    origin_location_id: str
    destination_location_id: str
    driver_id: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    def applies_on(self, day: date) -> bool:
        """Report whether the default covers a local day.

        Args:
            day: Local calendar day.

        Returns:
            True when the weekday matches and the day is inside the window.
        """
        if day.weekday() not in self.weekdays:
            return False
        if self.effective_from is not None and day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to


@dataclass(frozen=True, slots=True)
class SchoolDay:
    """One person's school schedule for one local day.

    Attributes:
        person_id: Student.
        date: Local calendar day.
        location_id: School location.
        start: Bell start (UTC); equals ``end`` for NO_SCHOOL.
        end: Bell end (UTC).
        kind: Normal, early release, late start, no school or unknown.
        source: Where the schedule came from (for provenance in reports).
    """

    person_id: str
    date: date
    location_id: str
    start: datetime
    end: datetime
    kind: SchoolDayKind
    source: str


@dataclass(frozen=True, slots=True)
class AssessmentInput:
    """Everything the engine needs for one assessment run; no I/O behind it.

    Attributes:
        window_start: Assessment window start (UTC).
        window_end: Assessment window end (UTC).
        people: Registered people (family and contacts).
        external_passengers: Registered external children.
        locations: Registered locations.
        vehicles: Registered vehicles.
        transport_defaults: Effective-dated school transport defaults.
        activities: Activities inside the window.
        trips: Parsed transportation trips inside the window.
        school_days: School schedules inside the window.
        source_freshness: ``(source id, last success UTC or None)`` pairs.
        previous_findings: Findings from the previous run (lifecycle tracking).
        timezone: IANA zone used for local-day reasoning.
    """

    window_start: datetime
    window_end: datetime
    people: tuple[Person, ...] = ()
    external_passengers: tuple[ExternalPassenger, ...] = ()
    locations: tuple[Location, ...] = ()
    vehicles: tuple[Vehicle, ...] = ()
    transport_defaults: tuple[TransportDefault, ...] = ()
    activities: tuple[Activity, ...] = ()
    trips: tuple[Trip, ...] = ()
    school_days: tuple[SchoolDay, ...] = ()
    source_freshness: tuple[tuple[str, datetime | None], ...] = field(
        default_factory=tuple
    )
    previous_findings: tuple[Finding, ...] = ()
    timezone: str = "America/Los_Angeles"

    def freshness(self, source_id: str) -> datetime | None:
        """Return the last successful collection time of a source.

        Args:
            source_id: Calendar or feed identifier.

        Returns:
            Last success (UTC), or None when never collected or unknown.
        """
        for name, when in self.source_freshness:
            if name == source_id:
                return when
        return None
