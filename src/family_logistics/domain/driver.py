"""Driver availability (DRIVER-006).

A driver's commitments are the rides they drive plus their own activities: a
driver attending an event consumes availability even though it is not a ride
(handoff 3.1). A finding is raised for **every passenger** of an affected ride,
because each passenger leg is an independent obligation (ADR 0004).

- Unknown or unauthorized driver: the ride has no valid driver (critical).
- Overlap: a ride overlaps another ride or the driver's own activity at a
  different place (critical; a warning when either place is unregistered and
  the overlap might be the same place).
- Reachability: back-to-back commitments whose gap is shorter than the drive
  between them plus the travel buffer (critical). An unknown travel time is
  reported as ROUTE-010 (incomplete), never as a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_logistics.domain.locations import text_key
from family_logistics.domain.models import Severity
from family_logistics.domain.rules import make_finding

if TYPE_CHECKING:
    from datetime import datetime

    from family_logistics.domain.context import AssessmentContext
    from family_logistics.domain.models import Finding, Trip


@dataclass(frozen=True, slots=True)
class Commitment:
    """One interval in which a driver is busy.

    Attributes:
        driver_id: Who.
        start: Start (UTC).
        end: End (UTC).
        start_key: Location key where the commitment begins, None if unknown.
        end_key: Location key where it ends, None if unknown.
        start_location_id: Registry location where it begins, for routing.
        end_location_id: Registry location where it ends, for routing.
        trip: The ride, None for the driver's own activity.
        label: Short description for messages.
    """

    driver_id: str
    start: datetime
    end: datetime
    start_key: str | None
    end_key: str | None
    start_location_id: str | None
    end_location_id: str | None
    trip: Trip | None
    label: str


def _trip_commitment(ctx: AssessmentContext, trip: Trip) -> Commitment:
    """Describe a ride as a driver commitment."""
    pickup = ctx.resolver.pickup(trip.pickup, None)
    places = {
        ctx.resolver.destination(p.passenger_id, p.kind, p.destination)
        for p in trip.passengers
    }
    end = next(iter(places)) if len(places) == 1 else None
    return Commitment(
        trip.driver_id,
        trip.start,
        trip.end,
        pickup.key,
        end.key if end else None,
        pickup.location_id,
        end.location_id if end else None,
        trip,
        f"ride from {trip.pickup}",
    )


def commitments(ctx: AssessmentContext, driver_id: str) -> tuple[Commitment, ...]:
    """Collect a driver's rides and own activities in time order.

    Args:
        ctx: Run context.
        driver_id: Driver's person identifier.

    Returns:
        Commitments sorted by start.
    """
    found = [
        _trip_commitment(ctx, t) for t in ctx.inp.trips if t.driver_id == driver_id
    ]
    for activity in ctx.inp.activities:
        if driver_id not in activity.participant_ids:
            continue
        location_id = activity.location_id or ctx.resolver.resolve_text(
            activity.location_text
        )
        key = (
            ctx.resolver.key_for(location_id, driver_id)
            if location_id
            else text_key(activity.location_text or activity.activity_id)
        )
        found.append(
            Commitment(
                driver_id,
                activity.start,
                activity.end,
                key if location_id else None,
                key if location_id else None,
                location_id,
                location_id,
                None,
                "their own calendar event",
            )
        )
    return tuple(sorted(found, key=lambda c: (c.start, c.end)))


def _leg_findings(
    ctx: AssessmentContext,
    trip: Trip,
    message: str,
    severity: Severity,
) -> list[Finding]:
    """Raise one DRIVER-006 per passenger of a ride."""
    return [
        make_finding(
            "DRIVER-006",
            f"{ctx.display_name(p.passenger_id)}: {message}",
            (
                trip.key.as_str(),
                f"passenger:{p.passenger_id}",
                f"driver:{trip.driver_id}",
            ),
            suggestion="Assign another driver or move the conflicting commitment.",
            severity=severity,
        )
        for p in trip.passengers
    ]


def _overlap(a: Commitment, b: Commitment) -> bool:
    """Report whether two commitments overlap in time."""
    return a.start < b.end and b.start < a.end


def _overlap_findings(
    ctx: AssessmentContext, ride: Commitment, other: Commitment
) -> list[Finding]:
    """Check one ride against one overlapping commitment."""
    if ride.trip is None:  # pragma: no cover - callers pass rides only
        return []
    known = ride.start_key is not None and other.start_key is not None
    if known and ride.start_key in {other.start_key, other.end_key}:
        return []
    who = ctx.display_name(ride.driver_id)
    message = f"{who} is also committed to {other.label} at {ctx.when(other.start)}."
    severity = Severity.CRITICAL if known else Severity.WARNING
    if not known:
        message = (
            f"{who} may be double-booked with {other.label} at {ctx.when(other.start)}."
        )
    found = _leg_findings(ctx, ride.trip, message, severity)
    if other.trip is not None and other.trip.key != ride.trip.key:
        found.extend(_leg_findings(ctx, other.trip, message, severity))
    return found


def _reachability_findings(
    ctx: AssessmentContext, prev: Commitment, nxt: Commitment
) -> list[Finding]:
    """Check that a driver can get from one commitment to the next ride."""
    if nxt.trip is None or prev.end_key is None or nxt.start_key is None:
        return []
    gap = nxt.start - prev.end
    if prev.end_key == nxt.start_key or not (
        gap.total_seconds() >= 0 and gap <= ctx.settings.reachability_window
    ):
        return []
    if prev.end_location_id is None or nxt.start_location_id is None:
        return []
    estimate = ctx.route_provider.estimate(
        prev.end_location_id, nxt.start_location_id, prev.end, "driving"
    )
    if estimate is None:
        return [
            make_finding(
                "ROUTE-010",
                "Travel time to a ride pickup is unavailable; timing is unchecked.",
                (nxt.trip.key.as_str(), f"driver:{nxt.driver_id}", "reachability"),
            )
        ]
    if estimate.duration + ctx.settings.travel_buffer <= gap:
        return []
    who = ctx.display_name(nxt.driver_id)
    minutes = int(estimate.duration.total_seconds() // 60)
    return _leg_findings(
        ctx,
        nxt.trip,
        f"{who} cannot reach the pickup after {prev.label} ({minutes} min drive).",
        Severity.CRITICAL,
    )


def _authorization_findings(ctx: AssessmentContext, trip: Trip) -> list[Finding]:
    """Flag a ride whose driver is unknown or not an authorized driver."""
    driver = ctx.person(trip.driver_id)
    if driver is not None and driver.active and driver.can_drive:
        return []
    return _leg_findings(
        ctx,
        trip,
        "the ride's driver is not a registered, authorized driver.",
        Severity.CRITICAL,
    )


def driver_findings(ctx: AssessmentContext) -> list[Finding]:
    """Evaluate every driver with an upcoming ride.

    Args:
        ctx: Run context.

    Returns:
        DRIVER-006 and ROUTE-010 findings.
    """
    upcoming = ctx.upcoming_trips()
    found: list[Finding] = []
    for trip in upcoming:
        found.extend(_authorization_findings(ctx, trip))
    for driver_id in sorted({t.driver_id for t in upcoming}):
        items = commitments(ctx, driver_id)
        for i, ride in enumerate(items):
            if ride.trip is None or ride.end < ctx.now:
                continue
            for other in items[:i] + items[i + 1 :]:
                if _overlap(ride, other) and not (
                    other.trip is not None and other.start < ride.start
                ):
                    found.extend(_overlap_findings(ctx, ride, other))
            if i:
                found.extend(_reachability_findings(ctx, items[i - 1], ride))
    return found
