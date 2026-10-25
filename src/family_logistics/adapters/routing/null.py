"""A route provider that knows nothing, so every travel time is unknown.

This is the fail-closed fallback for a configured provider that is not
available yet. Unknown travel time flows to ROUTE-010 and an INCOMPLETE run
(ADR-006); the provider never returns a guessed duration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_logistics.domain.stop_order import OrderResult, Stop, choose_stop_order

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, timedelta

    from family_logistics.adapters.routing.base import RouteConstraints, RouteEstimate


def _unknown_duration(
    origin: str, destination: str, departure: datetime
) -> timedelta | None:
    """Return no duration for any leg."""
    del origin, destination, departure
    return None


class NullRouteProvider:
    """Route provider whose every estimate is unknown (``None``).

    Attributes:
        name: Provider name recorded on results.
    """

    name = "null"

    def estimate(
        self, origin: str, destination: str, departure_time: datetime, mode: str
    ) -> RouteEstimate | None:
        """Report the leg as unknown.

        Args:
            origin: Origin location ID.
            destination: Destination location ID.
            departure_time: Planned departure.
            mode: Travel mode.

        Returns:
            Always None.
        """
        del origin, destination, departure_time, mode
        return None

    def optimize(
        self,
        origin: str,
        stops: Sequence[Stop],
        end: str | None,
        constraints: RouteConstraints,
    ) -> OrderResult:
        """Choose a stop order with no travel times.

        A manual order is honoured and any free ordering is INCOMPLETE,
        exactly as for a static provider with an empty matrix.

        Args:
            origin: Pickup location ID.
            stops: Drop-off stops with deadlines.
            end: Optional final location.
            constraints: Departure time, manual order and dwell time.

        Returns:
            The ordering result.
        """
        return choose_stop_order(
            origin,
            stops,
            constraints.departure_time,
            _unknown_duration,
            fixed_order=constraints.fixed_order,
            end=end,
            dwell=constraints.dwell,
        )
