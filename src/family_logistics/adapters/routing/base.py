"""Route provider abstraction (handoff section 10; ADR route provider).

Providers:

- ``ha_relay`` (M3): estimates come from Home Assistant, which polls
  ``GET /api/v1/route-requests``, runs ``waze_travel_time.get_travel_times`` and
  posts the results back. The app never calls Home Assistant.
- ``google_routes`` (M3, optional): Routes API ``computeRoutes`` with an
  in-app monthly budget cap.
- ``static``: a fixed matrix for tests and offline simulation.

Every estimate records its source and age. A missing estimate is ``None``,
which the domain treats as unknown (ROUTE-010 / incomplete), never as zero.

#ASSUME: External resources: Waze (via HA) and Google Routes may be
unavailable or rate-limited at any time; callers must handle ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from family_logistics.domain.stop_order import (
    OrderResult,
    Stop,
    choose_stop_order,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class RouteEstimate:
    """One pairwise travel estimate.

    Attributes:
        origin: Origin location ID.
        destination: Destination location ID.
        departure: Departure time used for the estimate.
        duration: Estimated driving time.
        source: Provider name.
        fetched_at: When the estimate was obtained.
    """

    origin: str
    destination: str
    departure: datetime
    duration: timedelta
    source: str
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class RouteConstraints:
    """Constraints for multi-stop optimization.

    Attributes:
        departure_time: Planned departure from the origin.
        fixed_order: Manual stop order, overriding optimization.
        dwell: Load/unload buffer per stop.
    """

    departure_time: datetime
    fixed_order: tuple[str, ...] | None = None
    dwell: timedelta = timedelta(minutes=5)


class RouteProvider(Protocol):
    """Interface every routing provider implements."""

    name: str

    def estimate(
        self, origin: str, destination: str, departure_time: datetime, mode: str
    ) -> RouteEstimate | None:
        """Estimate one leg.

        Args:
            origin: Origin location ID.
            destination: Destination location ID.
            departure_time: Planned departure.
            mode: Travel mode, e.g. ``driving``.

        Returns:
            The estimate, or None when unknown.
        """
        ...

    def optimize(
        self,
        origin: str,
        stops: Sequence[Stop],
        end: str | None,
        constraints: RouteConstraints,
    ) -> OrderResult:
        """Choose a stop order subject to deadlines and manual order.

        Args:
            origin: Pickup location ID.
            stops: Drop-off stops with deadlines.
            end: Optional final location.
            constraints: Departure time, manual order and dwell time.

        Returns:
            The ordering result.
        """
        ...


class StaticRouteProvider:
    """Fixed travel-time matrix for tests and offline simulation.

    Attributes:
        name: Provider name recorded on every estimate.

    Args:
        matrix: (origin, destination) to driving time. Missing pairs are unknown.
    """

    name = "static"

    def __init__(self, matrix: Mapping[tuple[str, str], timedelta]) -> None:
        self._matrix = dict(matrix)

    def estimate(
        self, origin: str, destination: str, departure_time: datetime, mode: str
    ) -> RouteEstimate | None:
        """Look up a leg in the matrix.

        Args:
            origin: Origin location ID.
            destination: Destination location ID.
            departure_time: Planned departure.
            mode: Travel mode; only ``driving`` is modelled.

        Returns:
            The estimate, or None when the pair or mode is unknown.
        """
        duration = self._matrix.get((origin, destination))
        if duration is None or mode != "driving":
            return None
        return RouteEstimate(
            origin, destination, departure_time, duration, self.name, datetime.now(UTC)
        )

    def _duration(
        self, origin: str, destination: str, departure: datetime
    ) -> timedelta | None:
        """Return the matrix duration for a driving leg, or None when unknown."""
        found = self.estimate(origin, destination, departure, "driving")
        return found.duration if found else None

    def optimize(
        self,
        origin: str,
        stops: Sequence[Stop],
        end: str | None,
        constraints: RouteConstraints,
    ) -> OrderResult:
        """Choose a stop order using the matrix.

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
            self._duration,
            fixed_order=constraints.fixed_order,
            end=end,
            dwell=constraints.dwell,
        )
