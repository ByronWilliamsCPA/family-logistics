"""Parser for the Template v1 transportation notes block (handoff section 4).

The notes of a ``Family Transportation`` event carry a versioned key-value
block::

    FL_VERSION: 1
    TRIP_TYPE: pickup
    STATUS: confirmed
    DRIVER: Riley
    FAMILY_PASSENGERS: Sam, Jordan
    EXTERNAL_PASSENGERS: Passenger E
    PICKUP: Music Studio
    DESTINATIONS:
    Sam: HOME
    Jordan: HOME
    Passenger E: DEFAULT
    STOP_ORDER: AUTO
    CONFIRM: 1A2B3C
    NOTES:
    free text, preserved verbatim

Rules the parser enforces:

- Labels are case-insensitive; spaces and hyphens in labels read as
  underscores. Values are trimmed and Unicode NFC-normalized; CRLF and LF both
  work.
- Required: version, trip type, status, driver, at least one passenger, pickup,
  one destination row per passenger, and a valid event window.
- Duplicate keys, unsupported versions, unrecognized enum values (including
  un-deleted ``a | b`` alternatives), unrecognized lines, duplicate passengers,
  destination rows that do not match a passenger, and impossible stop orders
  are rejected with PARSE-001. Malformed notes are never silently ignored.
- Family names must match the registry exactly. A near miss produces NAME-002
  with a suggestion; the parser never substitutes the suggestion.
- External names resolve to stable registry IDs. An unknown external name is a
  registry-review warning (EXT-013), not a spelling error, and the passenger is
  kept as an obligation under an ``unregistered:`` ID. A family name in the
  external list (or the reverse) is NAME-002.
- An ambiguous external name (two registered children share it) is rejected;
  ``Name [passenger_id]`` selects one explicitly.

A trip is returned only when no critical finding was raised.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from family_logistics.domain.models import (
    Destination,
    DestinationKind,
    Finding,
    OccurrenceKind,
    PassengerKind,
    Severity,
    SourceEventKey,
    Trip,
    TripPassenger,
    TripStatus,
    TripType,
)
from family_logistics.domain.registry import Registry, normalize_name
from family_logistics.domain.rules import make_finding

if TYPE_CHECKING:
    from datetime import datetime

SUPPORTED_VERSIONS = frozenset({"1"})

LABELS = frozenset(
    {
        "FL_VERSION",
        "TRIP_TYPE",
        "STATUS",
        "DRIVER",
        "FAMILY_PASSENGERS",
        "EXTERNAL_PASSENGERS",
        "PICKUP",
        "DESTINATIONS",
        "STOP_ORDER",
        "CONFIRM",
        "NOTES",
    }
)

_TRIP_TYPES = {
    "pickup": TripType.PICKUP,
    "dropoff": TripType.DROP_OFF,
    "transfer": TripType.TRANSFER,
}
_STATUSES = {"tentative": TripStatus.TENTATIVE, "confirmed": TripStatus.CONFIRMED}
_EXPLICIT_ID = re.compile(r"^(?P<name>.*?)\s*\[(?P<pid>[A-Za-z0-9_.:-]+)\]\s*$")
_CONFIRM_CODE = re.compile(r"^[0-9A-Fa-f]{6}$")
UNREGISTERED_PREFIX = "unregistered:"


@dataclass(slots=True)
class RawNotes:
    """Fields read from the notes block before validation.

    Attributes:
        fields: Top-level label to value (first occurrence).
        destination_rows: (name, value) rows under DESTINATIONS, in order.
        free_notes: Text after ``NOTES:``, preserved.
        problems: Structural problems found while reading.
    """

    fields: dict[str, str] = field(default_factory=dict[str, str])
    destination_rows: list[tuple[str, str]] = field(
        default_factory=list[tuple[str, str]]
    )
    free_notes: str = ""
    problems: list[str] = field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Outcome of parsing one event's notes.

    Attributes:
        trip: The trip, or None when a critical finding was raised.
        findings: Every finding raised, critical or not.
    """

    trip: Trip | None
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        """Return True when a trip was produced.

        Returns:
            Whether the trip is usable.
        """
        return self.trip is not None


def _normalize_text(value: str) -> str:
    """NFC-normalize, replace no-break spaces and unify line endings."""
    text = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_label(value: str) -> str:
    """Upper-case a label and turn spaces and hyphens into underscores."""
    return re.sub(r"[\s-]+", "_", value.strip()).upper()


def _enum_token(value: str) -> str:
    """Reduce an enum value to a case-folded token without separators."""
    return re.sub(r"[\s_-]+", "", value).casefold()


def read_notes(notes: str) -> RawNotes:
    """Split a notes block into fields, destination rows and free text.

    Args:
        notes: Raw notes text from the calendar event.

    Returns:
        The raw fields plus any structural problems.
    """
    raw = RawNotes()
    lines = _normalize_text(notes).split("\n")
    in_destinations = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        head, sep, tail = stripped.partition(":")
        label = _normalize_label(head)
        if sep and label == "NOTES":
            rest = [tail.strip(), *lines[index + 1 :]]
            raw.free_notes = "\n".join(rest).strip()
            break
        if sep and label in LABELS:
            in_destinations = label == "DESTINATIONS"
            if label in raw.fields:
                raw.problems.append(f"Duplicate key {label}.")
            else:
                raw.fields[label] = tail.strip()
            continue
        if sep and in_destinations:
            raw.destination_rows.append((head.strip(), tail.strip()))
            continue
        raw.problems.append(f"Unrecognized line: {stripped[:60]!r}.")
    return raw


@dataclass(slots=True)
class _Builder:
    """Validates raw notes against the registry and builds a trip."""

    key: SourceEventKey
    registry: Registry
    findings: list[Finding] = field(default_factory=list[Finding])
    passengers: list[TripPassenger] = field(default_factory=list[TripPassenger])
    # normalized identifier -> index into passengers (written name, display, id)
    passenger_index: dict[str, int] = field(default_factory=dict[str, int])
    written_names: list[str] = field(default_factory=list[str])

    @property
    def event(self) -> str:
        """Return the source event key as an entity-key string."""
        return self.key.as_str()

    def parse_error(self, message: str, detail: str) -> None:
        """Record a PARSE-001 finding for this event."""
        self.findings.append(
            make_finding("PARSE-001", message, (self.event, f"parse:{detail}"))
        )

    def required(self, raw: RawNotes, label: str) -> str | None:
        """Return a required field's value, recording PARSE-001 when missing."""
        value = raw.fields.get(label, "").strip()
        if not value:
            self.parse_error(f"Missing required field {label}.", label)
            return None
        return value

    def enum_value[E](
        self, raw: RawNotes, label: str, choices: dict[str, E]
    ) -> E | None:
        """Return the enum member for a field, recording PARSE-001 when invalid."""
        value = self.required(raw, label)
        if value is None:
            return None
        if "|" in value:
            self.parse_error(
                f"{label} still lists alternatives; keep exactly one.", label
            )
            return None
        found = choices.get(_enum_token(value))
        if found is None:
            self.parse_error(f"Unrecognized {label} value {value!r}.", label)
        return found

    def version(self, raw: RawNotes) -> None:
        """Check FL_VERSION is present and supported."""
        value = self.required(raw, "FL_VERSION")
        if value is not None and value not in SUPPORTED_VERSIONS:
            self.parse_error(f"Unsupported FL_VERSION {value!r}.", "FL_VERSION")

    def driver(self, raw: RawNotes) -> str | None:
        """Resolve DRIVER to an authorized registered driver's ID."""
        value = self.required(raw, "DRIVER")
        if value is None:
            return None
        person = self.registry.resolve_driver(value)
        if person is None:
            suggestion = self.registry.suggest_driver(value)
            self.findings.append(
                make_finding(
                    "PARSE-001",
                    f"Driver {value!r} is not a registered driver or contact.",
                    (self.event, "parse:DRIVER"),
                    suggestion=f"Did you mean {suggestion}?" if suggestion else None,
                )
            )
            return None
        if not person.can_drive:
            self.parse_error(
                f"{person.display_name!r} is not an authorized driver.",
                "DRIVER-unauthorized",
            )
            return None
        return person.person_id

    def add_passenger(self, passenger: TripPassenger, written: str) -> None:
        """Add a passenger once and index its identifiers for row lookup."""
        if any(p.passenger_id == passenger.passenger_id for p in self.passengers):
            self.parse_error(
                f"Passenger {passenger.display_name!r} is listed more than once.",
                f"duplicate:{passenger.passenger_id}",
            )
            return
        index = len(self.passengers)
        self.passengers.append(passenger)
        self.written_names.append(written)
        for ident in (written, passenger.display_name, passenger.passenger_id):
            self.passenger_index.setdefault(normalize_name(ident), index)

    def family(self, raw: RawNotes) -> None:
        """Resolve FAMILY_PASSENGERS exactly against the registry."""
        for written in _split_list(raw.fields.get("FAMILY_PASSENGERS", "")):
            name = _EXPLICIT_ID.sub(r"\g<name>", written).strip()
            child = self.registry.resolve_family_child(name)
            if child is not None:
                placeholder = Destination("", DestinationKind.NAMED)
                self.add_passenger(
                    TripPassenger(
                        child.person_id,
                        PassengerKind.FAMILY,
                        child.display_name,
                        placeholder,
                    ),
                    name,
                )
                continue
            self.unknown_family_name(name)

    def unknown_family_name(self, name: str) -> None:
        """Record NAME-002 for an unknown or misplaced family name."""
        if self.registry.resolve_external(name):
            message = (
                f"{name!r} is an external passenger listed under FAMILY_PASSENGERS."
            )
            suggestion = "Move the name to EXTERNAL_PASSENGERS."
        else:
            guess = self.registry.suggest_family_child(name)
            message = f"Unknown family passenger name {name!r}."
            suggestion = f"Did you mean {guess}?" if guess else None
        self.findings.append(
            make_finding(
                "NAME-002",
                message,
                (self.event, f"family-name:{normalize_name(name)}"),
                suggestion=suggestion,
            )
        )

    def external(self, raw: RawNotes) -> None:
        """Resolve EXTERNAL_PASSENGERS, rejecting family names in the list."""
        for written in _split_list(raw.fields.get("EXTERNAL_PASSENGERS", "")):
            match = _EXPLICIT_ID.match(written)
            name = match.group("name").strip() if match else written
            if self.registry.resolve_family_child(name) is not None:
                self.findings.append(
                    make_finding(
                        "NAME-002",
                        f"Family child {name!r} is listed under EXTERNAL_PASSENGERS.",
                        (self.event, f"external-name:{normalize_name(name)}"),
                        suggestion="Move the name to FAMILY_PASSENGERS.",
                    )
                )
                continue
            if match:
                self.explicit_external(name, match.group("pid"))
            else:
                self.lookup_external(name)

    def explicit_external(self, name: str, passenger_id: str) -> None:
        """Resolve a `Name [id]` external entry against the registry."""
        passenger = self.registry.external_by_id(passenger_id)
        if passenger is None or passenger not in self.registry.resolve_external(name):
            self.parse_error(
                f"{name!r} [{passenger_id}] does not match a registered passenger.",
                f"external-id:{passenger_id}",
            )
            return
        self.add_external(passenger.passenger_id, passenger.display_first_name, name)

    def lookup_external(self, name: str) -> None:
        """Resolve an external name: unique match, ambiguous, or unregistered."""
        matches = self.registry.resolve_external(name)
        if len(matches) > 1:
            options = ", ".join(f"{name} [{m.passenger_id}]" for m in matches)
            self.findings.append(
                make_finding(
                    "PARSE-001",
                    f"External name {name!r} matches more than one registered child.",
                    (self.event, f"external-ambiguous:{normalize_name(name)}"),
                    suggestion=f"Write one of: {options}",
                )
            )
            return
        if matches:
            self.add_external(
                matches[0].passenger_id, matches[0].display_first_name, name
            )
            return
        self.findings.append(
            make_finding(
                "EXT-013",
                f"External passenger {name!r} is not in the registry yet.",
                (self.event, f"external-unknown:{normalize_name(name)}"),
                suggestion="Add the child to the registry so the ride can be checked.",
            )
        )
        self.add_external(f"{UNREGISTERED_PREFIX}{normalize_name(name)}", name, name)

    def add_external(self, passenger_id: str, display: str, written: str) -> None:
        """Add an external passenger with a placeholder destination."""
        self.add_passenger(
            TripPassenger(
                passenger_id,
                PassengerKind.EXTERNAL,
                display,
                Destination("", DestinationKind.NAMED),
            ),
            written,
        )

    def lookup_passenger(self, written: str) -> int | None:
        """Return the passenger index a destination row or stop refers to."""
        name = _EXPLICIT_ID.sub(r"\g<name>", written).strip()
        match = _EXPLICIT_ID.match(written)
        if match and normalize_name(match.group("pid")) in self.passenger_index:
            return self.passenger_index[normalize_name(match.group("pid"))]
        return self.passenger_index.get(normalize_name(name))

    def destinations(self, raw: RawNotes) -> None:
        """Assign one destination row to each passenger, rejecting mismatches."""
        if "DESTINATIONS" not in raw.fields:
            self.parse_error("Missing required field DESTINATIONS.", "DESTINATIONS")
        assigned: set[int] = set()
        for row_name, value in raw.destination_rows:
            index = self.lookup_passenger(row_name)
            if index is None:
                self.parse_error(
                    f"Destination row {row_name!r} does not match a passenger.",
                    f"destination-row:{normalize_name(row_name)}",
                )
                continue
            if index in assigned:
                self.parse_error(
                    f"Duplicate destination row for {row_name!r}.",
                    f"destination-dup:{self.passengers[index].passenger_id}",
                )
                continue
            if not value:
                self.parse_error(
                    f"Empty destination for {row_name!r}.",
                    f"destination-empty:{self.passengers[index].passenger_id}",
                )
                continue
            assigned.add(index)
            old = self.passengers[index]
            self.passengers[index] = TripPassenger(
                old.passenger_id, old.kind, old.display_name, _destination(value)
            )
        for index, passenger in enumerate(self.passengers):
            if index not in assigned:
                self.parse_error(
                    f"No destination row for {passenger.display_name!r}.",
                    f"destination-missing:{passenger.passenger_id}",
                )

    def stop_order(self, raw: RawNotes) -> tuple[str, ...] | None:
        """Return the explicit stop order, None for AUTO, or reject it."""
        value = raw.fields.get("STOP_ORDER", "").strip()
        if not value or normalize_name(value) == "auto":
            return None
        items = _split_list(value)
        known = set(self.passenger_index) | {
            normalize_name(p.destination.raw) for p in self.passengers
        }
        normalized = [normalize_name(i) for i in items]
        unknown = [i for i, n in zip(items, normalized, strict=True) if n not in known]
        if unknown or len(set(normalized)) != len(normalized):
            self.parse_error(
                "STOP_ORDER names an unknown or repeated stop.", "STOP_ORDER"
            )
            return None
        return tuple(items)

    def confirm(self, raw: RawNotes) -> str | None:
        """Return the CONFIRM code upper-cased, or reject a malformed one."""
        value = raw.fields.get("CONFIRM", "").strip()
        if not value:
            return None
        if not _CONFIRM_CODE.match(value):
            self.parse_error("CONFIRM must be six hex characters.", "CONFIRM")
            return None
        return value.upper()

    def window(self, start: datetime, end: datetime) -> None:
        """Reject a window that is naive or not strictly increasing."""
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            self.parse_error(
                "Event start/end are missing a zone or out of order.", "window"
            )


def _split_list(value: str) -> list[str]:
    """Split a comma-separated list, dropping blank items."""
    return [item.strip() for item in value.split(",") if item.strip()]


def _destination(value: str) -> Destination:
    """Classify a destination value as HOME, DEFAULT or a named place."""
    token = value.strip()
    upper = token.upper()
    if upper == "HOME":
        return Destination("HOME", DestinationKind.HOME)
    if upper == "DEFAULT":
        return Destination("DEFAULT", DestinationKind.DEFAULT)
    return Destination(token, DestinationKind.NAMED)


def parse_trip(  # noqa: PLR0913 - keyword-only event context
    notes: str,
    *,
    key: SourceEventKey,
    start: datetime,
    end: datetime,
    registry: Registry,
    occurrence: OccurrenceKind = OccurrenceKind.SINGLE,
) -> ParseResult:
    """Parse and validate one Family Transportation event.

    Args:
        notes: Raw notes text of the event.
        key: Source event identity (calendar, UID, RECURRENCE-ID).
        start: Event start, timezone-aware.
        end: Event end, timezone-aware.
        registry: People and external passengers to resolve names against.
        occurrence: Single event, unmodified series instance or override.

    Returns:
        The trip (when no critical finding was raised) and all findings.
    """
    raw = read_notes(notes)
    builder = _Builder(key=key, registry=registry)
    for problem in raw.problems:
        builder.parse_error(problem, f"structure:{problem}")
    builder.version(raw)
    trip_type = builder.enum_value(raw, "TRIP_TYPE", _TRIP_TYPES)
    status = builder.enum_value(raw, "STATUS", _STATUSES)
    driver_id = builder.driver(raw)
    builder.family(raw)
    builder.external(raw)
    no_passengers = not raw.fields.get("FAMILY_PASSENGERS", "").strip() and not (
        raw.fields.get("EXTERNAL_PASSENGERS", "").strip()
    )
    if no_passengers:
        builder.parse_error("At least one passenger is required.", "passengers")
    pickup = builder.required(raw, "PICKUP")
    builder.destinations(raw)
    stop_order = builder.stop_order(raw)
    confirm = builder.confirm(raw)
    builder.window(start, end)

    findings = tuple(builder.findings)
    blocked = any(f.severity is Severity.CRITICAL for f in findings)
    # Every missing required value already raised a critical finding; the None
    # checks only narrow types for the constructor below.
    if (
        blocked
        or trip_type is None
        or status is None
        or driver_id is None
        or pickup is None
    ):
        return ParseResult(trip=None, findings=findings)
    trip = Trip(
        key=key,
        trip_type=trip_type,
        status=status,
        driver_id=driver_id,
        pickup=pickup,
        passengers=tuple(builder.passengers),
        stop_order=stop_order,
        start=start,
        end=end,
        confirm_code=confirm,
        free_notes=raw.free_notes,
        occurrence=occurrence,
    )
    return ParseResult(trip=trip, findings=findings)
