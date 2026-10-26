"""A route provider that knows nothing, so every travel time is unknown.

This is the placeholder the factory returns for the ``ha_relay`` provider
until the relay exists. Unknown travel time flows to ROUTE-010 and an
INCOMPLETE run (ADR 0006); the provider never returns a guessed duration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_logistics.domain.stop_order import OrderResult, Stop, choose_stop_order

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, timedelta

    from family_logistics.adapters.routing.base import RouteConstraints, RouteEstimate


# #CRITICAL: External resources: an unknown travel time must never be turned
# into a duration, or a ride would be judged feasible on a guess.
# #VERIFY: tests/unit/test_route_provider_factory.py::TestEngineFailsClosed
# pins ROUTE-010 and an INCOMPLETE run for the default provider.
def _unknown_duration(
    origin: str, destination: str, departure: datetime
) -> timedelta | None:
    """Return no duration for any leg."""
    del origin, destination, departure
    return None


class NullRouteProvider:
    """Route provider whose every estimate is unknown (``None``).

    Attributes:
        name: The implementation name (``"null"``), as ``StaticRouteProvider``
            reports ``"static"``. It identifies this class, not the configured
            ``FL_ROUTE_PROVIDER`` value, so a result recorded while the
            ``ha_relay`` placeholder is in use is visibly degraded.
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

        No leg has a known travel time, so this behaves exactly like a static
        provider with an empty matrix. The manual order in
        ``constraints.fixed_order`` is passed to ``choose_stop_order``, but with
        at least one stop it cannot be checked against deadlines: the result is
        INCOMPLETE with an empty order, as is any free ordering. A manual order
        that does not cover every stop is INCOMPLETE for that reason. Only an
        empty stop list with no ``end`` is FEASIBLE (nothing to drive); with an
        ``end`` the final leg is unknown and the result is INCOMPLETE.

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
