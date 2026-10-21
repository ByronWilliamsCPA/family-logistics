"""Location resolver: names, homes, destinations; no addresses in keys."""

from __future__ import annotations

import pytest
from fl_engine import LOCATIONS, PEOPLE

from family_logistics.domain.locations import (
    UNREGISTERED_LABEL,
    LocationResolver,
    text_key,
)
from family_logistics.domain.models import (
    Destination,
    DestinationKind,
    ExternalPassenger,
    Location,
    PassengerKind,
)

pytestmark = pytest.mark.unit

EXTERNALS = (
    ExternalPassenger(
        "ext-e", "Passenger E", "h-e", default_dropoff_location_id="loc-e-home"
    ),
    ExternalPassenger("ext-r", "Passenger R", "h-r"),
)


@pytest.fixture
def resolver() -> LocationResolver:
    return LocationResolver(LOCATIONS, PEOPLE, EXTERNALS)


class TestResolveText:
    def test_name_alias_and_id(self, resolver):
        assert resolver.resolve_text("music  STUDIO") == "studio"
        assert resolver.resolve_text("hs") == "high-school"
        assert resolver.resolve_text("xc") == "xc"

    def test_name_before_at_sign_only(self, resolver):
        assert resolver.resolve_text("Sibling Game @ 2 Sample Road") == "game"

    def test_unknown_or_empty(self, resolver):
        assert resolver.resolve_text("Somewhere Else") is None
        assert resolver.resolve_text(None) is None

    def test_ambiguous_name_never_resolves(self):
        twins = (Location("a", "Gym"), Location("b", "Gym"))
        ambiguous = LocationResolver(twins, (), ())
        assert ambiguous.resolve_text("Gym") is None


class TestKeys:
    def test_home_location_maps_to_home_key(self, resolver):
        assert resolver.key_for("home", "sam") == "home:sam"
        assert resolver.key_for("studio", "sam") == "studio"
        assert resolver.key_for("home", None) == "home"

    def test_labels_never_reveal_addresses(self, resolver):
        assert resolver.label("home:sam") == "home"
        assert resolver.label("studio") == "Music Studio"
        assert resolver.label("text:abc") == UNREGISTERED_LABEL

    def test_text_key_hides_text(self):
        key = text_key("1 Example Way")
        assert key.startswith("text:")
        assert "Example" not in key
        assert text_key(" 1  example way ") == key


class TestDestination:
    def test_family_home_is_home_key(self, resolver):
        place = resolver.destination(
            "sam", PassengerKind.FAMILY, Destination("HOME", DestinationKind.HOME)
        )
        assert (place.key, place.location_id, place.verified) == (
            "home:sam",
            "home",
            True,
        )

    def test_external_default_uses_saved_dropoff(self, resolver):
        place = resolver.destination(
            "ext-e",
            PassengerKind.EXTERNAL,
            Destination("DEFAULT", DestinationKind.DEFAULT),
        )
        assert place.location_id == "loc-e-home"

    def test_external_without_default_is_unresolved(self, resolver):
        place = resolver.destination(
            "ext-r",
            PassengerKind.EXTERNAL,
            Destination("DEFAULT", DestinationKind.DEFAULT),
        )
        assert place.key is None
        assert "default" in place.reason

    def test_named_unverified_and_unknown(self, resolver):
        unverified = resolver.destination(
            "sam", PassengerKind.FAMILY, Destination("New Place", DestinationKind.NAMED)
        )
        assert unverified.key == "new-place"
        assert not unverified.verified
        unknown = resolver.destination(
            "sam", PassengerKind.FAMILY, Destination("Nowhere", DestinationKind.NAMED)
        )
        assert unknown.key is None

    def test_unregistered_home_keeps_home_key(self):
        plain = LocationResolver(LOCATIONS, (), ())
        place = plain.destination(
            "sam", PassengerKind.FAMILY, Destination("HOME", DestinationKind.HOME)
        )
        assert place.key == "home:sam"
        assert place.reason == "home location not registered"


class TestPickup:
    def test_home_word_is_passengers_home(self, resolver):
        assert resolver.pickup("Home", "sam").key == "home:sam"

    def test_registered_and_unknown(self, resolver):
        assert resolver.pickup("XC Practice", "sam").key == "xc"
        assert resolver.pickup("Mystery Lot", "sam").key is None
