"""When a stop's address may leave the app for Home Assistant (pure, no I/O).

Owner decision of 2026-10-25 (ADR 0006, privacy spec section 5), restrict
other families only: the ``ha_relay`` route provider sends Home Assistant a
zone entity id for every leg endpoint that has one. A stop with no zone may be
sent as its decrypted address unless it is another family's home. Another
family's home without a zone never leaves the app: the endpoint is withheld,
the leg's travel time is unknown, and the engine reports ROUTE-010 so the run
is ``incomplete``, never clear. The household's own home and venues (a school,
a music studio) may be sent as addresses.

The ``ha_relay`` provider applies it when it queues a leg and again when the
route-request poll is served (ADR 0006).

Another family's home reuses the registry's existing ownership notion: the
``home_location_id`` of any ``CONTACT`` person (an adult from another family,
such as Driver S), even when a household member lists the same home. An
external passenger's default pickup or drop-off is treated as a venue.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from family_logistics.domain.models import PersonRole

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_logistics.domain.models import Person


class HaRelayEndpoint(StrEnum):
    """What the ``ha_relay`` provider may send Home Assistant for one stop."""

    ZONE = "zone"
    """Send the location's ``ha_zone_entity_id``; never the address."""
    ADDRESS = "address"
    """Send the decrypted address (no zone, not another family's home)."""
    WITHHELD = "withheld"
    """Send nothing; the leg is unknown and raises ROUTE-010."""


def is_other_family_home(location_id: str, people: Iterable[Person]) -> bool:
    """Return whether a location is another family's home.

    Fails closed: a home shared with a contact counts as another family's home
    even when a household member also lists it. Active flags are ignored,
    because an inactive contact's home is still another family's address.

    Args:
        location_id: Registry location ID.
        people: Registered people (household members and contacts).

    Returns:
        True when any ``CONTACT`` person lists the location as home.
    """
    # #ASSUME: Security: an external passenger's home is protected only when
    # someone from that household is registered as a CONTACT person with
    # home_location_id set; a passenger's default pickup or drop-off alone is
    # treated as a venue and may be sent to Home Assistant as an address.
    # #VERIFY: when registering Passenger E or R, register a contact from that
    # household with its home, or give the home a Home Assistant zone.
    return any(
        p.role is PersonRole.CONTACT and p.home_location_id == location_id
        for p in people
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
    # home addresses (privacy class R) inside the app. The ha_relay provider
    # applies it to every leg endpoint through
    # ``RouteRelayRepository.location_sources``: at enqueue time (a leg with a
    # WITHHELD endpoint is not queued, so it is unknown, ROUTE-010) and again
    # when ``GET /api/v1/route-requests`` serves a request, against the registry
    # as it is then, before any address is decrypted.
    # #VERIFY: tests/unit/test_route_relay_api.py::TestAddressRelease (a
    # zone-less other-family home never appears in a route-request payload, a
    # registry change after enqueue stops release at GET time, a stop with a
    # zone is sent as its zone id) and
    # tests/unit/test_ha_relay_provider.py::TestAddressRelease and
    # ::TestEngine::test_a_zone_less_other_family_home_is_withheld_and_the_run_incomplete
    # (ROUTE-010 and an INCOMPLETE run through the provider and the engine).
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
