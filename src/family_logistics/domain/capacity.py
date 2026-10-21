"""Vehicle capacity check (CAP-014; round-2 decision 10).

Capacity counts the driver plus every passenger. An unknown capacity (no
vehicle, or a vehicle whose seats are not yet verified) is reported as
unverified with severity ``incomplete``; it is never assumed sufficient.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_logistics.domain.models import Finding, Severity
from family_logistics.domain.rules import make_finding

if TYPE_CHECKING:
    from family_logistics.domain.models import Trip, Vehicle


def capacity_findings(trip: Trip, vehicle: Vehicle | None) -> list[Finding]:
    """Check that the vehicle seats the driver and every passenger.

    Args:
        trip: The parsed trip.
        vehicle: Vehicle used for the trip, or None when unknown.

    Returns:
        Empty when capacity is verified sufficient; otherwise one CAP-014
        finding (critical when exceeded, incomplete when unverified).
    """
    needed = 1 + len(trip.passengers)
    keys = (trip.key.as_str(), "capacity")
    if vehicle is None or vehicle.seat_capacity is None:
        return [
            make_finding(
                "CAP-014",
                f"Seat capacity is unverified for a ride needing {needed} seats.",
                keys,
                suggestion="Register the vehicle and its verified seat count.",
                severity=Severity.INCOMPLETE,
            )
        ]
    if needed > vehicle.seat_capacity:
        return [
            make_finding(
                "CAP-014",
                (
                    f"{vehicle.display_name} seats {vehicle.seat_capacity} but the "
                    f"ride needs {needed} including the driver."
                ),
                keys,
                severity=Severity.CRITICAL,
            )
        ]
    return []
