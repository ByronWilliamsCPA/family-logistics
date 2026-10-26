"""The ``ha_relay`` route provider: cached Home Assistant travel times (ADR 0006).

Home Assistant prices legs with ``google_travel_time.get_travel_times`` and
posts the results to ``POST /api/v1/route-results``. This provider only reads
what was posted. ``estimate`` is synchronous and never calls Home Assistant: a
valid cache hit is returned, anything else is unknown (``None``) and, for a
real miss, queues one request for Home Assistant to pick up.

#CRITICAL: External resources: the cache is empty until Home Assistant answers
and Home Assistant may be down for days. Every miss, stale entry, busy store or
storage error returns None, which the engine turns into ROUTE-010 and an
INCOMPLETE run. A duration is never guessed, defaulted or borrowed from another
weekday or bucket, and ``estimate`` never raises into the engine.
#VERIFY: tests/unit/test_ha_relay_provider.py::TestFailClosed and ::TestEngine.

#ASSUME: External resources: Home Assistant resolves a bare time-of-day
``departure_time`` to the next occurrence of that time, so a result files
under the weekday and bucket the request names. This is NOT verified.
#VERIFY: docs/guides/ha-route-relay.md, deployment checklist; first confirm the
integration returns a time-dependent duration at all, then compare the same
request in the evening and again before 08:00, and keep shadow mode until the
durations match.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from family_logistics.adapters.routing.base import RouteEstimate
from family_logistics.core.exceptions import DatabaseError, ValidationError
from family_logistics.db.route_relay import (
    CachedLeg,
    EnqueueStatus,
    Leg,
    RouteRelayRepository,
)
from family_logistics.domain.stop_order import OrderResult, Stop, choose_stop_order
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import timedelta, tzinfo

    from family_logistics.adapters.routing.base import RouteConstraints

logger = get_logger(__name__)

_DRIVING = "driving"
"""The only travel mode Home Assistant is asked to price."""


def _utc_now() -> datetime:
    """Return the current instant (aware, UTC)."""
    return datetime.now(UTC)


class HaRelayRouteProvider:
    """Route provider backed by results Home Assistant posted.

    Attributes:
        name: Provider name recorded on every estimate.

    Args:
        repository: The route cache and request queue.
        zone: The household's time zone (``FL_TIMEZONE``); the weekday and
            15-minute bucket of a departure are local.
        clock: Returns the current instant (aware); injectable for tests.
    """

    name = "ha_relay"

    def __init__(
        self,
        repository: RouteRelayRepository,
        zone: tzinfo,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._zone = zone
        self._clock = clock

    def estimate(
        self, origin: str, destination: str, departure_time: datetime, mode: str
    ) -> RouteEstimate | None:
        """Return a cached leg, or None and queue it for Home Assistant.

        Args:
            origin: Origin location ID.
            destination: Destination location ID.
            departure_time: Planned departure (aware).
            mode: Travel mode; only ``driving`` is modelled.

        Returns:
            The estimate when a valid observation for the same weekday and
            15-minute bucket exists, else None. Never raises.
        """
        if mode != _DRIVING or origin == destination:
            return None
        cached = self._cached_or_queued(origin, destination, departure_time)
        if cached is None:
            return None
        return RouteEstimate(
            origin,
            destination,
            departure_time,
            cached.duration,
            self.name,
            cached.observed_at,
        )

    def _cached_or_queued(
        self, origin: str, destination: str, departure: datetime
    ) -> CachedLeg | None:
        """Look a leg up; on a miss queue it. Storage failures mean unknown."""
        try:
            leg = Leg.at(origin, destination, departure, self._zone)
            now = self._clock()
            cached = self._repository.lookup(leg, now=now)
            queueable = cached is None and not self._unroutable(leg)
            if queueable and (
                self._repository.enqueue(leg, now=now) is EnqueueStatus.FULL
            ):
                logger.warning(
                    "route_relay.queue_full", weekday=leg.weekday, bucket=leg.bucket
                )
        except (SQLAlchemyError, DatabaseError, ValidationError) as error:
            # Only the error class is logged: a message could carry SQL
            # parameters, and no location text belongs in a log line.
            logger.warning(
                "route_relay.estimate_unavailable", error=type(error).__name__
            )
            return None
        return cached

    def _unroutable(self, leg: Leg) -> bool:
        """Report whether an endpoint of the leg can never be given to Home Assistant.

        A location with neither a zone entity id nor a stored address has no
        endpoint to send, so queueing the leg would only occupy the queue until
        it expires. A location with no row is left to ``enqueue``, which
        refuses it. Nothing is decrypted: an address that is stored but later
        proves unreadable is handled when the poll is built.

        Args:
            leg: The leg about to be queued.

        Returns:
            True when an endpoint has neither a zone nor an address.
        """
        sources = self._repository.location_sources({leg.origin, leg.destination})
        # Only location ids are logged, never address text.
        unresolvable = [
            location_id
            for location_id in (leg.origin, leg.destination)
            if (source := sources.get(location_id)) is not None
            and not source.zone_entity_id
            and source.address_token is None
        ]
        if unresolvable:
            logger.warning(
                "route_relay.leg_unroutable",
                origin=leg.origin,
                destination=leg.destination,
                unresolvable=unresolvable,
            )
        return bool(unresolvable)

    def _duration(
        self, origin: str, destination: str, departure: datetime
    ) -> timedelta | None:
        """Return the driving time for a leg, or None when unknown."""
        found = self.estimate(origin, destination, departure, _DRIVING)
        return found.duration if found else None

    def optimize(
        self,
        origin: str,
        stops: Sequence[Stop],
        end: str | None,
        constraints: RouteConstraints,
    ) -> OrderResult:
        """Choose a stop order from cached legs.

        Legs missing from the cache are queued for Home Assistant and make the
        result INCOMPLETE until they are answered.

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
