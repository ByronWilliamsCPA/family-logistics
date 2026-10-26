"""Which stop addresses may leave the app for Home Assistant (owner decision)."""

from __future__ import annotations

import pytest

from family_logistics.domain.address_release import (
    HaRelayEndpoint,
    ha_relay_endpoint,
    is_other_family_home,
    may_send_address_to_home_assistant,
)
from family_logistics.domain.models import ExternalPassenger, Person, PersonRole

MORGAN = Person("morgan", "Morgan", PersonRole.PARENT, home_location_id="loc-home")
ALEX = Person("alex", "Alex", PersonRole.CHILD, home_location_id="loc-home")
DRIVER_S = Person("driver-s", "Driver S", PersonRole.CONTACT, home_location_id="loc-s")
PEOPLE = (MORGAN, ALEX, DRIVER_S)
PASSENGER_E = ExternalPassenger(
    "ext-e",
    "E",
    "household-e",
    default_pickup_location_id="loc-e",
    default_dropoff_location_id="loc-e",
    alternate_destination_ids=("loc-school",),
)
PASSENGER_R = ExternalPassenger(
    "ext-r", "R", "household-r", default_dropoff_location_id="loc-r"
)
EXTERNALS = (PASSENGER_E, PASSENGER_R)


def endpoint_for(location_id: str, *, has_ha_zone: bool) -> HaRelayEndpoint:
    other = is_other_family_home(
        location_id, people=PEOPLE, external_passengers=EXTERNALS
    )
    return ha_relay_endpoint(has_ha_zone=has_ha_zone, other_family_home=other)


@pytest.mark.parametrize(
    ("location_id", "has_ha_zone", "expected"),
    [
        ("loc-home", True, HaRelayEndpoint.ZONE),
        ("loc-home", False, HaRelayEndpoint.ADDRESS),
        ("loc-school", False, HaRelayEndpoint.ADDRESS),
        ("loc-s", True, HaRelayEndpoint.ZONE),
        ("loc-e", True, HaRelayEndpoint.ZONE),
        # Another family's zone-less home never leaves: fail closed (ROUTE-010).
        ("loc-s", False, HaRelayEndpoint.WITHHELD),
        ("loc-e", False, HaRelayEndpoint.WITHHELD),
        ("loc-r", False, HaRelayEndpoint.WITHHELD),
        # Nobody's home and not a passenger default: a venue, so it may be sent.
        ("loc-unlisted", False, HaRelayEndpoint.ADDRESS),
    ],
    ids=[
        "own-home+zone",
        "own-home+no-zone",
        "venue+no-zone",
        "contact-home+zone",
        "passenger-home+zone",
        "contact-home+no-zone",
        "passenger-home+no-zone",
        "passenger-dropoff-only+no-zone",
        "unlisted+no-zone",
    ],
)
def test_endpoint_policy(
    location_id: str, *, has_ha_zone: bool, expected: HaRelayEndpoint
) -> None:
    assert endpoint_for(location_id, has_ha_zone=has_ha_zone) is expected


def test_home_shared_with_a_contact_is_withheld() -> None:
    shared = Person(
        "driver-s", "Driver S", PersonRole.CONTACT, home_location_id="loc-home"
    )
    assert is_other_family_home(
        "loc-home", people=(MORGAN, shared), external_passengers=()
    )
    assert not may_send_address_to_home_assistant(
        has_ha_zone=False, other_family_home=True
    )


def test_inactive_contact_home_is_still_another_familys() -> None:
    former = Person(
        "driver-s",
        "Driver S",
        PersonRole.CONTACT,
        active=False,
        home_location_id="loc-s",
    )
    assert is_other_family_home("loc-s", people=(former,), external_passengers=())


@pytest.mark.parametrize(
    ("has_ha_zone", "other_family_home", "allowed"),
    [
        (True, False, False),
        (False, False, True),
        (True, True, False),
        (False, True, False),
    ],
)
def test_may_send_address_only_without_zone_and_not_other_family(
    *, has_ha_zone: bool, other_family_home: bool, allowed: bool
) -> None:
    assert (
        may_send_address_to_home_assistant(
            has_ha_zone=has_ha_zone, other_family_home=other_family_home
        )
        is allowed
    )
