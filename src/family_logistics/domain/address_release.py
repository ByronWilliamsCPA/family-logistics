"""When a stop's address may leave the app for Home Assistant (pure, no I/O).

Owner decision of 2026-10-25 (ADR 0006, privacy spec section 5), restrict
other families only: the ``ha_relay`` route provider sends Home Assistant a
zone entity id for every leg endpoint that has one. A stop with no zone may be
sent as its decrypted address unless it is another family's home. Another
family's home without a zone never leaves the app: the endpoint is withheld,
the leg's travel time is unknown, and the engine reports ROUTE-010 so the run
is ``incomplete``, never clear. The household's own home and venues (a school,
a music studio) may be sent as addresses.

Other-family homes reuse the registry's existing ownership notions:

- the home of any ``CONTACT`` person (an adult from another family, such as
  Driver S), even when a household member lists the same home;
- the default pickup and default drop-off location of any external passenger
  (Passenger E, Passenger R), which is how the registry records where that
  child's household is.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from family_logistics.domain.models import PersonRole

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_logistics.domain.models import ExternalPassenger, Person


class HaRelayEndpoint(StrEnum):
    """What the ``ha_relay`` provider may send Home Assistant for one stop."""

    ZONE = "zone"
    """Send the location's ``ha_zone_entity_id``; never the address."""
    ADDRESS = "address"
    """Send the decrypted address (no zone, not another family's home)."""
    WITHHELD = "withheld"
    """Send nothing; the leg is unknown and raises ROUTE-010."""


def is_other_family_home(
    location_id: str,
    *,
    people: Iterable[Person],
    external_passengers: Iterable[ExternalPassenger],
) -> bool:
    """Return whether a location is another family's home.

    Fails closed: a home shared with a contact, or any default pickup or
    drop-off of an external passenger, counts as another family's home even
    when a household member also uses it. Active flags are ignored, because an
    inactive contact's or passenger's home is still another family's address.

    Args:
        location_id: Registry location ID.
        people: Registered people (household members and contacts).
        external_passengers: Registered children of other families.

    Returns:
        True when a ``CONTACT`` person lists the location as home, or an
        external passenger uses it as default pickup or drop-off.
    """
    # #EDGE: Security: an external passenger's default pickup or drop-off is
    # usually that child's home, but can be a venue; treating it as a home
    # withholds a zone-less venue (incomplete, not a leak).
    # #VERIFY: if a real default is a venue, give that location a zone.
    if any(
        p.role is PersonRole.CONTACT and p.home_location_id == location_id
        for p in people
    ):
        return True
    return any(
        location_id in (e.default_pickup_location_id, e.default_dropoff_location_id)
        for e in external_passengers
    )


def ha_relay_endpoint(*, has_ha_zone: bool, other_family_home: bool) -> HaRelayEndpoint:
    """Decide what the ``ha_relay`` provider may send for one stop.

    Args:
        has_ha_zone: Whether the stop's location has an ``ha_zone_entity_id``.
        other_family_home: Whether the location is another family's home
            (see ``is_other_family_home``).

    Returns:
        ``ZONE`` when a zone exists, ``WITHHELD`` for another family's home
        with no zone, and ``ADDRESS`` for any other zone-less stop.
    """
    # #CRITICAL: Security: this is the only gate that keeps other families'
    # home addresses (privacy class R) inside the app. The future ha_relay
    # provider must call it for every leg endpoint before building a route
    # request, and must turn WITHHELD into an unknown estimate (ROUTE-010).
    # #VERIFY: PR 2 of the routing series tests that a zone-less other-family
    # home never appears in a route-request payload and yields ROUTE-010, and
    # that a stop with a zone is sent as its zone id, never as an address.
    if has_ha_zone:
        return HaRelayEndpoint.ZONE
    if other_family_home:
        return HaRelayEndpoint.WITHHELD
    return HaRelayEndpoint.ADDRESS


def may_send_address_to_home_assistant(
    *, has_ha_zone: bool, other_family_home: bool
) -> bool:
    """Return whether a stop's decrypted address may be sent to Home Assistant.

    Args:
        has_ha_zone: Whether the stop's location has an ``ha_zone_entity_id``.
        other_family_home: Whether the location is another family's home.

    Returns:
        True only for a zone-less stop that is not another family's home; a
        stop with a zone is sent as its zone id, so its address is never needed.
    """
    endpoint = ha_relay_endpoint(
        has_ha_zone=has_ha_zone, other_family_home=other_family_home
    )
    return endpoint is HaRelayEndpoint.ADDRESS
