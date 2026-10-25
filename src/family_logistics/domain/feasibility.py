"""Per-ride checks: destination, timing, stop order, capacity, confirmation.

- DEST-004: a passenger's destination cannot be resolved or is unverified.
  Critical for an imminent ride, a warning otherwise.
- TIME-007 / ORDER-012: the ride cannot meet a passenger's next activity
  (deadline minus the travel buffer). With ``STOP_ORDER: AUTO`` the fastest
  order that meets every deadline is chosen and TIME-007 means none exists;
  an explicit order that misses a deadline is ORDER-012. A faster route that
  misses a deadline is always rejected.
- ROUTE-010 (incomplete): a needed travel time or place is unavailable, so
  feasibility is unknown. Never reported as a pass.
- CAP-014: seats for the driver plus every passenger. An unknown capacity is
  reported only when the ride needs at least ``unverified_capacity_seats``.
- CONF-008: the ride is tentative or its confirmation code is not current.

Rides that already ended are not evaluated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_logistics.domain.capacity import capacity_findings
from family_logistics.domain.confirmation import (
    confirmation_findings,
    evaluate_confirmation,
)
from family_logistics.domain.models import PassengerKind, Severity
from family_logistics.domain.registry import normalize_name
from family_logistics.domain.rules import make_finding
from family_logistics.domain.stop_order import (
    OrderResult,
    OrderStatus,
    Stop,
    choose_stop_order,
)

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from family_logistics.domain.context import AssessmentContext
    from family_logistics.domain.locations import ResolvedPlace
    from family_logistics.domain.models import Finding, Trip, TripPassenger


def _place(ctx: AssessmentContext, passenger: TripPassenger) -> ResolvedPlace:
    """Resolve one passenger's destination."""
    return ctx.resolver.destination(
        passenger.passenger_id, passenger.kind, passenger.destination
    )


def destination_findings(ctx: AssessmentContext, trip: Trip) -> list[Finding]:
    """Flag missing, ambiguous or unverified destinations (DEST-004).

    Args:
        ctx: Run context.
        trip: An upcoming ride.

    Returns:
        One DEST-004 per affected passenger.
    """
    severity = Severity.CRITICAL if ctx.is_imminent(trip.start) else Severity.WARNING
    found: list[Finding] = []
    for passenger in trip.passengers:
        place = _place(ctx, passenger)
        if place.key is not None and place.verified:
            continue
        found.append(
            make_finding(
                "DEST-004",
                f"{passenger.display_name}, {ctx.when(trip.start)} ride: {place.reason}.",
                (
                    trip.key.as_str(),
                    f"passenger:{passenger.passenger_id}",
                    "destination",
                ),
                suggestion="Register or verify the destination in the registry.",
                severity=severity,
            )
        )
    return found


def _next_stay_start(
    ctx: AssessmentContext, person_id: str, key: str, after: datetime
) -> datetime | None:
    """Return when the person's next stay at a place starts after a time."""
    return next(
        (
            s.start
            for s in ctx.stays.get(person_id, ())
            if s.key == key and s.start >= after
        ),
        None,
    )


def _ready_time(ctx: AssessmentContext, trip: Trip, pickup_key: str) -> datetime:
    """Return when every family passenger can leave the pickup place."""
    ready = trip.start
    window = ctx.settings.pickup_tolerance
    for passenger in trip.passengers:
        for stay in ctx.stays.get(passenger.passenger_id, ()):
            near = trip.start - window <= stay.end <= trip.end
            if stay.key == pickup_key and near and stay.end > ready:
                ready = stay.end
    return ready


def _stops(
    ctx: AssessmentContext, trip: Trip
) -> tuple[list[Stop], dict[str, str], bool]:
    """Build drop-off stops keyed by location key.

    Args:
        ctx: Run context.
        trip: The ride.

    Returns:
        (stops, label to stop ID map, whether any destination is unresolved).
    """
    deadlines: dict[str, datetime | None] = {}
    location_ids: dict[str, str] = {}
    labels: dict[str, str] = {}
    unresolved = False
    buffer = ctx.settings.travel_buffer
    for passenger in trip.passengers:
        place = _place(ctx, passenger)
        if place.key is None or place.location_id is None:
            unresolved = True
            continue
        labels[normalize_name(passenger.display_name)] = place.key
        labels[normalize_name(passenger.destination.raw)] = place.key
        location_ids[place.key] = place.location_id
        deadline = None
        if passenger.kind is PassengerKind.FAMILY:
            start = _next_stay_start(ctx, passenger.passenger_id, place.key, trip.start)
            deadline = start - buffer if start is not None else None
        current = deadlines.get(place.key)
        if place.key not in deadlines or (
            deadline is not None and (current is None or deadline < current)
        ):
            deadlines[place.key] = deadline
    stops = [Stop(k, location_ids[k], d) for k, d in deadlines.items()]
    return stops, labels, unresolved


def _estimator(ctx: AssessmentContext):  # noqa: ANN202 - local TravelEstimator
    """Adapt the route provider to the stop-order estimator protocol."""

    def estimate(
        origin: str, destination: str, departure: datetime
    ) -> timedelta | None:
        """Return a driving duration, or None when unknown."""
        found = ctx.route_provider.estimate(origin, destination, departure, "driving")
        return found.duration if found is not None else None

    return estimate


def route_findings(ctx: AssessmentContext, trip: Trip) -> list[Finding]:
    """Check deadlines and stop order (TIME-007, ORDER-012, ROUTE-010).

    Args:
        ctx: Run context.
        trip: An upcoming ride.

    Returns:
        Route findings; empty when feasible or nothing constrains the ride.
    """
    stops, labels, unresolved = _stops(ctx, trip)
    has_deadline = any(s.deadline is not None for s in stops)
    if not has_deadline and trip.stop_order is None:
        return []
    keys = (trip.key.as_str(), "route")
    first = trip.passengers[0].passenger_id if trip.passengers else None
    pickup = ctx.resolver.pickup(trip.pickup, first)
    if unresolved or pickup.location_id is None or pickup.key is None:
        return [
            make_finding(
                "ROUTE-010",
                f"Ride at {ctx.when(trip.start)}: unregistered stop; deadlines unchecked.",
                keys,
            )
        ]
    fixed = None
    if trip.stop_order is not None:
        mapped = [labels.get(normalize_name(label)) for label in trip.stop_order]
        fixed = tuple(dict.fromkeys(k for k in mapped if k is not None))
    departure = _ready_time(ctx, trip, pickup.key) + ctx.settings.stop_buffer
    result = choose_stop_order(
        pickup.location_id,
        stops,
        departure,
        _estimator(ctx),
        fixed_order=fixed,
        dwell=ctx.settings.stop_buffer,
    )
    return _order_findings(ctx, trip, result, fixed)


def _order_findings(
    ctx: AssessmentContext,
    trip: Trip,
    result: OrderResult,
    fixed: tuple[str, ...] | None,
) -> list[Finding]:
    """Translate a stop-order result into findings."""
    keys = (trip.key.as_str(), "route")
    when = ctx.when(trip.start)
    if result.status is OrderStatus.FEASIBLE:
        return []
    if result.status is OrderStatus.INCOMPLETE:
        rule = "ORDER-012" if result.reason.startswith("STOP_ORDER") else "ROUTE-010"
        return [make_finding(rule, f"Ride at {when}: {result.reason}", keys)]
    places = ", ".join(ctx.resolver.label(k) for k in result.missed)
    if fixed is not None:
        return [
            make_finding(
                "ORDER-012",
                f"Ride at {when}: the explicit stop order misses {places}.",
                keys,
                suggestion="Use STOP_ORDER: AUTO or reorder the stops.",
            )
        ]
    return [
        make_finding(
            "TIME-007",
            f"Ride at {when} cannot reach {places} in time in any stop order.",
            keys,
            suggestion="Start the ride earlier or split it between drivers.",
        )
    ]


def ride_capacity_findings(ctx: AssessmentContext, trip: Trip) -> list[Finding]:
    """Check vehicle capacity when it is known or plausibly relevant (CAP-014).

    Args:
        ctx: Run context.
        trip: An upcoming ride.

    Returns:
        Zero or one CAP-014 finding.
    """
    driver = ctx.person(trip.driver_id)
    vehicle = ctx.vehicle(driver.default_vehicle_id if driver else None)
    if vehicle is not None and vehicle.seat_capacity is not None:
        return capacity_findings(trip, vehicle)
    if 1 + len(trip.passengers) >= ctx.settings.unverified_capacity_seats:
        return capacity_findings(trip, vehicle)
    return []


def trip_findings(ctx: AssessmentContext) -> list[Finding]:
    """Run every per-ride check for upcoming rides.

    Args:
        ctx: Run context.

    Returns:
        Destination, route, capacity and confirmation findings.
    """
    found: list[Finding] = []
    for trip in ctx.upcoming_trips():
        found.extend(destination_findings(ctx, trip))
        found.extend(route_findings(ctx, trip))
        found.extend(ride_capacity_findings(ctx, trip))
        state = evaluate_confirmation(trip, now=ctx.now, timezone=ctx.inp.timezone)
        found.extend(confirmation_findings(state))
    return found
