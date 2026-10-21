"""Passenger-level change-impact analysis (handoff section 3.3).

Each passenger leg is an independent obligation keyed by (source calendar,
UID, RECURRENCE-ID, passenger ID). Comparing the previous and current sets of
legs yields:

- **PASS-003** when an external passenger's leg disappears and no replacement
  leg covers the same passenger in an equivalent time window. Critical when the
  lost leg was confirmed; Warning when it was tentative (a tentative ride can
  still be the only plan for that child).
- **CONF-008** when a confirmed leg's material fields (driver, pickup,
  destination, start, end) changed. The entity key is the trip key so it
  de-duplicates with the confirmation module's CONF-008 for the same trip.
- No PASS-003 for family passengers: their gaps are found by location
  continuity (RIDE-005, M2), so removing a family child never raises a false
  external-obligation alarm.

Legs of trips whose notes failed to parse this run are carried forward rather
than treated as removed. The parse failure already raised a critical finding;
reporting the passengers as lost as well would be a false alarm.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from family_logistics.domain.models import (
    Finding,
    PassengerKind,
    Severity,
    SourceEventKey,
    TripPassengerLeg,
    TripStatus,
)
from family_logistics.domain.registry import normalize_name
from family_logistics.domain.rules import make_finding

if TYPE_CHECKING:
    from collections.abc import Iterable

DEFAULT_REPLACEMENT_TOLERANCE = timedelta(minutes=60)
"""How far a replacement leg's start may move and still count as equivalent.

#ASSUME: Timing: 60 minutes is a proposed default pending owner approval.
"""


@dataclass(frozen=True, slots=True)
class ChangeImpact:
    """Result of comparing two snapshots of passenger legs.

    Attributes:
        findings: PASS-003 and CONF-008 findings, de-duplicated by issue ID.
        removed_family_legs: Family legs that disappeared (input to continuity).
        replaced_legs: (previous, replacement) pairs for external passengers.
        carried_forward: Previous legs kept because their trip failed to parse.
    """

    findings: tuple[Finding, ...]
    removed_family_legs: tuple[TripPassengerLeg, ...]
    replaced_legs: tuple[tuple[TripPassengerLeg, TripPassengerLeg], ...]
    carried_forward: tuple[TripPassengerLeg, ...]


def _equivalent_window(
    a: TripPassengerLeg, b: TripPassengerLeg, tolerance: timedelta
) -> bool:
    """Return True when two legs overlap or start within the tolerance."""
    overlaps = a.start < b.end and b.start < a.end
    return overlaps or abs(a.start - b.start) <= tolerance


def _material_changed(previous: TripPassengerLeg, current: TripPassengerLeg) -> bool:
    """Return True when driver, pickup, destination or times differ."""
    return (
        previous.driver_id != current.driver_id
        or normalize_name(previous.pickup) != normalize_name(current.pickup)
        or normalize_name(previous.destination.raw)
        != normalize_name(current.destination.raw)
        or previous.start != current.start
        or previous.end != current.end
    )


def _find_replacement(
    lost: TripPassengerLeg,
    current: list[TripPassengerLeg],
    tolerance: timedelta,
) -> TripPassengerLeg | None:
    """Return a current leg covering the same passenger in an equivalent window."""
    for leg in current:
        if (
            leg.passenger_id == lost.passenger_id
            and leg.leg_key != lost.leg_key
            and _equivalent_window(lost, leg, tolerance)
        ):
            return leg
    return None


def _lost_external_finding(lost: TripPassengerLeg) -> Finding:
    """Build PASS-003 for a lost external leg (critical if it was confirmed)."""
    confirmed = lost.status is TripStatus.CONFIRMED
    state = "confirmed" if confirmed else "tentative"
    # Message carries the pseudonymous ID and time only, never a destination
    # (a destination row may contain an address).
    return make_finding(
        "PASS-003",
        (
            f"The {state} ride for external passenger {lost.passenger_id} "
            f"starting {lost.start.isoformat()} disappeared with no replacement."
        ),
        (lost.trip_key.as_str(), f"passenger:{lost.passenger_id}"),
        suggestion="Restore the ride or record who now transports this child.",
        severity=Severity.CRITICAL if confirmed else Severity.WARNING,
    )


def analyze_change_impact(
    previous: Iterable[TripPassengerLeg],
    current: Iterable[TripPassengerLeg],
    *,
    unparsed_trip_keys: frozenset[SourceEventKey] | None = None,
    replacement_tolerance: timedelta = DEFAULT_REPLACEMENT_TOLERANCE,
) -> ChangeImpact:
    """Compare previous and current passenger legs.

    Args:
        previous: Legs from the last accepted snapshot.
        current: Legs from this run.
        unparsed_trip_keys: Trips whose notes failed to parse this run.
        replacement_tolerance: Start-time tolerance for an equivalent window.

    Returns:
        Findings plus the removed, replaced and carried-forward legs.
    """
    unparsed = unparsed_trip_keys or frozenset[SourceEventKey]()
    current_legs = list(current)
    current_by_key = {leg.leg_key: leg for leg in current_legs}
    findings: dict[str, Finding] = {}
    removed_family: list[TripPassengerLeg] = []
    replaced: list[tuple[TripPassengerLeg, TripPassengerLeg]] = []
    carried: list[TripPassengerLeg] = []

    for prev in previous:
        if prev.trip_key in unparsed:
            carried.append(prev)
            continue
        now = current_by_key.get(prev.leg_key)
        if now is not None:
            if prev.status is TripStatus.CONFIRMED and _material_changed(prev, now):
                finding = make_finding(
                    "CONF-008",
                    "Ride changed since it was confirmed.",
                    (prev.trip_key.as_str(),),
                    suggestion="Re-confirm the ride in the calendar.",
                )
                findings.setdefault(finding.issue_id, finding)
            continue
        if prev.passenger_kind is PassengerKind.FAMILY:
            removed_family.append(prev)
            continue
        replacement = _find_replacement(prev, current_legs, replacement_tolerance)
        if replacement is not None:
            replaced.append((prev, replacement))
            continue
        finding = _lost_external_finding(prev)
        findings.setdefault(finding.issue_id, finding)

    return ChangeImpact(
        findings=tuple(findings.values()),
        removed_family_legs=tuple(removed_family),
        replaced_legs=tuple(replaced),
        carried_forward=tuple(carried),
    )
