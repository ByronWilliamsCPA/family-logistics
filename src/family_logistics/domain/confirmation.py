"""Confirmation revision mechanism (conditionally approved, round-2 decision 3).

The calendar notes stay authoritative and the application never writes to
iCloud. A plain ``STATUS: confirmed`` cannot show whether someone re-confirmed
after a material edit, so a confirmation is bound to the occurrence and the
content it confirmed:

- The canonical text (``FLC1``) holds an occurrence identity plus the material
  fields: start, end, driver, passengers with destinations, pickup and stop
  order. The confirmation code is the first six hex characters (uppercase) of
  SHA-256 over that text.
- Parents do not compute or type codes in the normal flow: the iOS Shortcut
  writes the ``CONFIRM:`` line. A documented manual fallback exists if the
  Shortcut proves unreliable. A ride without a valid code raises CONF-008.

Occurrence rules:

- **Single event** and **override** (an occurrence with its own RECURRENCE-ID
  VEVENT): the code must be the *occurrence code*, whose identity is the
  occurrence's local start date and time. A series code copied into an
  override's notes never matches, so an override always needs its own CONFIRM.
- **Unmodified series instance**: the code must be the *series code*, whose
  identity is ``SERIES`` and whose times are local time-of-day only. An instance
  inherits the master's notes, so the master's CONFIRM confirms exactly the
  unmodified instances whose material fields equal the master's. A series edit
  changes the series code, so nothing is re-confirmed automatically, and it
  never touches overrides.
- **Past occurrences** keep their historical state: ``evaluate_confirmation``
  returns the recorded snapshot unchanged once the occurrence has ended.

Recorded confirmation (a parent marked it) is distinct from driver acceptance
(the driver independently agreed). v1 has no way to verify acceptance, so
``driver_acceptance`` is always ``"unverified"`` and reports must not imply the
driver accepted.

#ASSUME: The occurrence identity uses the occurrence start rather than the
iCalendar UID because iOS Shortcuts exposes event dates but (#VERIFY) not the
UID. M0 check 4 confirms whether the UID is readable; if it is, adding it is a
canonical-format version bump (FLC2), not a silent change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from family_logistics.domain.models import (
    DriverAcceptance,
    Finding,
    OccurrenceKind,
    SourceEventKey,
    Trip,
    TripStatus,
)
from family_logistics.domain.registry import normalize_name
from family_logistics.domain.rules import make_finding

if TYPE_CHECKING:
    from datetime import datetime

CODE_LENGTH = 6
CANONICAL_VERSION = "FLC1"
DEFAULT_TZ = "America/Los_Angeles"


class RecordedConfirmation(StrEnum):
    """What the calendar records about confirmation for one occurrence."""

    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    MISSING_CODE = "missing_code"
    STALE_CODE = "stale_code"


@dataclass(frozen=True, slots=True)
class ConfirmationState:
    """Confirmation snapshot for one occurrence (stored in the audit log).

    Attributes:
        trip_key: Occurrence identity.
        recorded: What the calendar records.
        driver_acceptance: Always ``"unverified"`` in v1.
        expected_code: Code that would confirm the current content.
        fingerprint: Full SHA-256 of the canonical text evaluated.
        evaluated_at: When this snapshot was taken.
        occurrence_end: End of the occurrence; after it the snapshot is frozen.
    """

    trip_key: SourceEventKey
    recorded: RecordedConfirmation
    driver_acceptance: DriverAcceptance
    expected_code: str
    fingerprint: str
    evaluated_at: datetime
    occurrence_end: datetime

    @property
    def is_current(self) -> bool:
        """Report whether the occurrence counts as confirmed.

        Returns:
            True only for a recorded confirmation with a matching code.
        """
        return self.recorded is RecordedConfirmation.CONFIRMED


def _local(value: datetime, tz: ZoneInfo, fmt: str) -> str:
    """Format a datetime in the given zone."""
    return value.astimezone(tz).strftime(fmt)


def canonical_material_text(trip: Trip, timezone: str = DEFAULT_TZ) -> str:
    """Build the canonical ``FLC1`` text for the code this trip requires.

    Unmodified series instances use the series form (``OCCURRENCE=SERIES`` and
    time-of-day only); single events and overrides use the occurrence form
    (local start as identity and full local date-times).

    Args:
        trip: The parsed trip.
        timezone: IANA zone used for local times.

    Returns:
        Canonical multi-line text.
    """
    tz = ZoneInfo(timezone)
    if trip.occurrence is OccurrenceKind.SERIES_INSTANCE:
        identity = "SERIES"
        start = _local(trip.start, tz, "%H:%M")
        end = _local(trip.end, tz, "%H:%M")
    else:
        identity = _local(trip.start, tz, "%Y-%m-%dT%H:%M")
        start = identity
        end = _local(trip.end, tz, "%Y-%m-%dT%H:%M")
    passenger_lines = sorted(
        f"PASSENGER={p.passenger_id}>{normalize_name(p.destination.raw)}"
        for p in trip.passengers
    )
    stop_order = (
        "AUTO"
        if trip.stop_order is None
        else ",".join(normalize_name(s) for s in trip.stop_order)
    )
    lines = [
        CANONICAL_VERSION,
        f"OCCURRENCE={identity}",
        f"START={start}",
        f"END={end}",
        f"DRIVER={trip.driver_id}",
        f"PICKUP={normalize_name(trip.pickup)}",
        *passenger_lines,
        f"STOP_ORDER={stop_order}",
    ]
    return "\n".join(lines)


def material_fingerprint(trip: Trip, timezone: str = DEFAULT_TZ) -> str:
    """Return the SHA-256 fingerprint of the canonical text.

    Args:
        trip: The parsed trip.
        timezone: IANA zone for local times.

    Returns:
        64 lowercase hex characters.
    """
    text = canonical_material_text(trip, timezone)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def confirmation_code(trip: Trip, timezone: str = DEFAULT_TZ) -> str:
    """Return the code the Shortcut writes as ``CONFIRM:`` for this content.

    Args:
        trip: The parsed trip.
        timezone: IANA zone for local times.

    Returns:
        Six uppercase hex characters.
    """
    return material_fingerprint(trip, timezone)[:CODE_LENGTH].upper()


def _recorded(trip: Trip, expected: str) -> RecordedConfirmation:
    """Classify what the calendar records against the expected code."""
    if trip.status is TripStatus.TENTATIVE:
        return RecordedConfirmation.TENTATIVE
    if trip.confirm_code is None:
        return RecordedConfirmation.MISSING_CODE
    if trip.confirm_code.upper() != expected:
        return RecordedConfirmation.STALE_CODE
    return RecordedConfirmation.CONFIRMED


def evaluate_confirmation(
    trip: Trip,
    *,
    now: datetime,
    previous: ConfirmationState | None = None,
    timezone: str = DEFAULT_TZ,
) -> ConfirmationState:
    """Evaluate one occurrence, keeping history frozen for past occurrences.

    Args:
        trip: The parsed trip for this occurrence.
        now: Current time (timezone-aware).
        previous: The last stored snapshot for the same occurrence, if any.
        timezone: IANA zone for local times.

    Returns:
        The stored snapshot unchanged when the occurrence has ended, otherwise
        a fresh snapshot.
    """
    if (
        previous is not None
        and previous.trip_key == trip.key
        and previous.occurrence_end <= now
    ):
        return previous
    fingerprint = material_fingerprint(trip, timezone)
    expected = fingerprint[:CODE_LENGTH].upper()
    return ConfirmationState(
        trip_key=trip.key,
        recorded=_recorded(trip, expected),
        driver_acceptance="unverified",
        expected_code=expected,
        fingerprint=fingerprint,
        evaluated_at=now,
        occurrence_end=trip.end,
    )


def is_confirmation_current(trip: Trip, timezone: str = DEFAULT_TZ) -> bool:
    """Report whether the trip is confirmed for its current content.

    Args:
        trip: The parsed trip.
        timezone: IANA zone for local times.

    Returns:
        True only when STATUS is confirmed and CONFIRM matches the code the
        occurrence kind requires.
    """
    return _recorded(trip, confirmation_code(trip, timezone)) is (
        RecordedConfirmation.CONFIRMED
    )


_MESSAGES = {
    RecordedConfirmation.TENTATIVE: "Ride is tentative.",
    RecordedConfirmation.MISSING_CODE: (
        "Ride is marked confirmed but has no CONFIRM code."
    ),
    RecordedConfirmation.STALE_CODE: (
        "Ride changed since it was confirmed, or this occurrence needs its own "
        "confirmation; the CONFIRM code does not match."
    ),
}


def confirmation_findings(state: ConfirmationState) -> list[Finding]:
    """Emit CONF-008 for an occurrence that is not currently confirmed.

    The entity key is the trip key, so this finding de-duplicates with the
    CONF-008 that change-impact analysis emits for the same trip.

    Args:
        state: Confirmation snapshot for the occurrence.

    Returns:
        Zero or one CONF-008 finding.
    """
    if state.is_current:
        return []
    return [
        make_finding(
            "CONF-008",
            _MESSAGES[state.recorded],
            (state.trip_key.as_str(),),
            suggestion="Re-confirm the ride with the Family Ride shortcut.",
        )
    ]
