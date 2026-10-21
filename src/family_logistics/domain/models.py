"""Core domain records for Family Logistics.

These are plain, immutable value objects. They carry no persistence or I/O
concerns, so the deterministic engine (parser, change impact, run status) can be
tested without a database or network.

Identity rules:

- A trip is identified by its source event: calendar, iCalendar UID and
  RECURRENCE-ID (``SourceEventKey``). A recurring series and each of its moved
  or cancelled occurrences therefore have distinct keys.
- A passenger leg is identified by its trip key plus the passenger ID. Each leg
  is an independent obligation: editing or removing one passenger never changes
  another passenger's leg.
- External children are referenced only by a stable internal ``passenger_id``
  and a first-name display label. Addresses never live on these records.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, override

if TYPE_CHECKING:
    from datetime import date, datetime


class TripType(StrEnum):
    """User-facing trip label; never sufficient on its own to infer destinations."""

    PICKUP = "pickup"
    DROP_OFF = "drop_off"
    TRANSFER = "transfer"


class TripStatus(StrEnum):
    """Confirmation status as written in the calendar notes."""

    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"


class Severity(StrEnum):
    """Finding severity, kept independent of the overall run status."""

    CRITICAL = "critical"
    WARNING = "warning"
    INCOMPLETE = "incomplete"


class RunStatus(StrEnum):
    """Overall assessment status shown on the family-wide card."""

    CLEAR = "clear"
    ATTENTION_REQUIRED = "attention_required"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class PersonRole(StrEnum):
    """Role of a registered person."""

    PARENT = "parent"
    CHILD = "child"
    CONTACT = "contact"


class PassengerKind(StrEnum):
    """Whether a passenger is a family member or an external child."""

    FAMILY = "family"
    EXTERNAL = "external"


class OccurrenceKind(StrEnum):
    """How a trip relates to a recurring series.

    SINGLE is a non-recurring event; SERIES_INSTANCE is an occurrence expanded
    from the series master with no override of its own; OVERRIDE is an
    occurrence with its own RECURRENCE-ID VEVENT (edited, moved or with its
    own notes).
    """

    SINGLE = "single"
    SERIES_INSTANCE = "series_instance"
    OVERRIDE = "override"


class DestinationKind(StrEnum):
    """How a per-passenger destination row was expressed."""

    HOME = "home"
    DEFAULT = "default"
    NAMED = "named"


class SensitiveStr:
    """A string that never appears in ``repr()``, ``str()`` or logs.

    Used for addresses and coordinates of family and external households.
    Call ``reveal()`` only where the value is genuinely needed (routing).

    Args:
        value: The sensitive text.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the underlying value.

        Returns:
            The sensitive text.
        """
        return self._value

    @override
    def __repr__(self) -> str:
        """Return a masked representation."""
        return "SensitiveStr('***')"

    @override
    def __str__(self) -> str:
        """Return a mask, never the value."""
        return "***"

    @override
    def __eq__(self, other: object) -> bool:
        """Compare underlying values of two SensitiveStr instances."""
        return isinstance(other, SensitiveStr) and other.reveal() == self._value

    @override
    def __hash__(self) -> int:
        """Hash the underlying value."""
        return hash(self._value)


@dataclass(frozen=True, slots=True)
class Person:
    """A family member or a registered external driver (contact).

    Driver eligibility is a registry flag. Appearing in a DRIVER field never
    makes a person a driver; children are not drivers unless explicitly
    flagged (handoff round-2 decision 6).

    Attributes:
        person_id: Stable internal identifier (lowercase slug).
        display_name: Canonical display name; parser matches against it.
        role: Parent, child or contact.
        aliases: Alternative spellings accepted as exact matches.
        active: Whether the person is currently in use.
        can_drive: Whether the person is an authorized driver.
        default_vehicle_id: Vehicle normally used when this person drives.
        monitoring_enabled: Whether transportation checks run for the person.
        monitoring_start: First day monitoring applies (inclusive), optional.
        monitoring_end: Last day monitoring applies (inclusive), optional.
    """

    person_id: str
    display_name: str
    role: PersonRole
    aliases: tuple[str, ...] = ()
    active: bool = True
    can_drive: bool = False
    default_vehicle_id: str | None = None
    monitoring_enabled: bool = True
    monitoring_start: date | None = None
    monitoring_end: date | None = None

    def is_monitored_on(self, day: date) -> bool:
        """Report whether transportation checks apply on a day.

        Monitoring is never enabled automatically: a disabled person stays
        disabled regardless of dates.

        Args:
            day: Local calendar day.

        Returns:
            True when enabled and the day is within the optional window.
        """
        if not (self.active and self.monitoring_enabled):
            return False
        if self.monitoring_start is not None and day < self.monitoring_start:
            return False
        return self.monitoring_end is None or day <= self.monitoring_end


@dataclass(frozen=True, slots=True)
class Vehicle:
    """A registered vehicle.

    Attributes:
        vehicle_id: Stable identifier.
        display_name: Label, e.g. ``family SUV``.
        seat_capacity: Seats including the driver; None when not yet verified.
    """

    vehicle_id: str
    display_name: str
    seat_capacity: int | None = None


@dataclass(frozen=True, slots=True)
class ExternalPassenger:
    """A child from another family with independent ride obligations.

    Attributes:
        passenger_id: Stable internal identifier (never a full name).
        display_first_name: First-name label used in notes and UI.
        household_id: Household the child belongs to.
        contact_id: Linked household contact (a registered Person), optional.
        default_pickup_location_id: Usual pickup location.
        default_dropoff_location_id: Location used when a row says ``DEFAULT``.
        alternate_destination_ids: Other permitted destinations.
        aliases: Alternative spellings accepted as exact matches.
        active: Whether the passenger is currently in use.
    """

    passenger_id: str
    display_first_name: str
    household_id: str
    contact_id: str | None = None
    default_pickup_location_id: str | None = None
    default_dropoff_location_id: str | None = None
    alternate_destination_ids: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    active: bool = True


@dataclass(frozen=True, slots=True)
class Location:
    """A resolvable place. The address is sensitive household data.

    The address and coordinates are excluded from ``repr()`` and comparison
    and wrapped in ``SensitiveStr``; they are used for routing only and never
    sent to the LLM, push notifications or general reports.

    Attributes:
        location_id: Stable internal identifier.
        name: Display name (safe to show).
        aliases: Alternative names accepted in notes.
        verified: Whether the address/coordinates were confirmed by a human.
        ha_zone_entity_id: Matching Home Assistant zone, if any.
        address: Street address (sensitive).
        coordinates: ``lat,lon`` text (sensitive).
    """

    location_id: str
    name: str
    aliases: tuple[str, ...] = ()
    verified: bool = False
    ha_zone_entity_id: str | None = None
    address: SensitiveStr | None = field(default=None, repr=False, compare=False)
    coordinates: SensitiveStr | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True, order=True)
class SourceEventKey:
    """Identity of one calendar occurrence.

    Attributes:
        calendar_id: Source calendar identifier.
        uid: iCalendar UID of the series or single event.
        recurrence_id: RECURRENCE-ID of the occurrence, None for a single event.
    """

    calendar_id: str
    uid: str
    recurrence_id: str | None = None

    def as_str(self) -> str:
        """Return a stable string form usable as an entity key.

        Returns:
            ``calendar_id|uid|recurrence_id`` with ``-`` for no recurrence.
        """
        return f"{self.calendar_id}|{self.uid}|{self.recurrence_id or '-'}"


@dataclass(frozen=True, slots=True)
class Destination:
    """A per-passenger destination as written in the notes.

    Attributes:
        raw: The trimmed value after the passenger name.
        kind: HOME, DEFAULT or a named alias/explicit place.
    """

    raw: str
    kind: DestinationKind


@dataclass(frozen=True, slots=True)
class TripPassenger:
    """One passenger on a trip with their own destination.

    Attributes:
        passenger_id: Registry ID, or ``unregistered:<name>`` when unknown.
        kind: Family or external.
        display_name: Name as resolved for display.
        destination: Where this passenger must be delivered.
    """

    passenger_id: str
    kind: PassengerKind
    display_name: str
    destination: Destination


@dataclass(frozen=True, slots=True)
class Trip:
    """A transportation commitment parsed from one calendar occurrence.

    Attributes:
        key: Source event identity.
        trip_type: Pickup, drop off or transfer.
        status: Tentative or confirmed, as written in the calendar.
        driver_id: Registered driver's person ID.
        pickup: Common pickup point as written.
        passengers: Passengers with their destinations.
        stop_order: None for AUTO, else explicit ordered stop labels.
        start: Planned transport window start (timezone-aware).
        end: Planned transport window end (timezone-aware).
        confirm_code: ``CONFIRM`` value from the notes, if any.
        free_notes: Preserved free text after ``NOTES:``.
        occurrence: Single event, unmodified series instance or override.
    """

    key: SourceEventKey
    trip_type: TripType
    status: TripStatus
    driver_id: str
    pickup: str
    passengers: tuple[TripPassenger, ...]
    stop_order: tuple[str, ...] | None
    start: datetime
    end: datetime
    confirm_code: str | None = None
    free_notes: str = ""
    occurrence: OccurrenceKind = OccurrenceKind.SINGLE

    def legs(self) -> tuple[TripPassengerLeg, ...]:
        """Expand the trip into independent per-passenger legs.

        Returns:
            One leg per passenger, each carrying the trip's shared fields.
        """
        return tuple(
            TripPassengerLeg(
                trip_key=self.key,
                passenger_id=p.passenger_id,
                passenger_kind=p.kind,
                destination=p.destination,
                driver_id=self.driver_id,
                pickup=self.pickup,
                start=self.start,
                end=self.end,
                status=self.status,
            )
            for p in self.passengers
        )


@dataclass(frozen=True, slots=True)
class TripPassengerLeg:
    """One passenger's obligation within a trip.

    Attributes:
        trip_key: Source event identity of the carrying trip.
        passenger_id: Passenger identifier.
        passenger_kind: Family or external.
        destination: Destination for this passenger.
        driver_id: Driver of the carrying trip.
        pickup: Pickup point of the carrying trip.
        start: Window start.
        end: Window end.
        status: Status of the carrying trip.
    """

    trip_key: SourceEventKey
    passenger_id: str
    passenger_kind: PassengerKind
    destination: Destination
    driver_id: str
    pickup: str
    start: datetime
    end: datetime
    status: TripStatus

    @property
    def leg_key(self) -> tuple[SourceEventKey, str]:
        """Return the identity of this leg.

        Returns:
            (trip key, passenger ID).
        """
        return (self.trip_key, self.passenger_id)


@dataclass(frozen=True, slots=True)
class Finding:
    """A deterministic validation result.

    The ``issue_id`` is a stable hash of the rule ID and the sorted entity keys,
    so the same problem keeps the same ID across runs (lifecycle tracking and
    notification de-duplication depend on this).

    Attributes:
        rule_id: Catalog rule, e.g. ``PARSE-001``.
        severity: Critical, warning or incomplete.
        message: Human-readable explanation; must not contain addresses.
        entity_keys: Identifiers of the affected trip/passenger/field.
        suggestion: Optional suggested correction; never applied automatically.
    """

    rule_id: str
    severity: Severity
    message: str
    entity_keys: tuple[str, ...] = field(default_factory=tuple)
    suggestion: str | None = None

    @property
    def issue_id(self) -> str:
        """Return the stable issue identifier.

        Returns:
            16 hex characters of SHA-256 over the rule ID and sorted entity keys.
        """
        material = "\x1f".join([self.rule_id, *sorted(self.entity_keys)])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


DriverAcceptance = Literal["unverified"]
"""Driver acceptance is never verified in v1; reports must not imply it."""
