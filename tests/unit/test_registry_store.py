"""Registry repositories: round trips, protected fields, conflicts."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from family_logistics.core.crypto import AddressCipher, ProtectedDataError, generate_key
from family_logistics.core.database import create_sqlite_engine
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.db import migrations_api
from family_logistics.db.registry import (
    Household,
    RegistryError,
    RegistryStore,
    load_snapshot,
)
from family_logistics.db.schema import location as location_table
from family_logistics.domain.events import (
    CalendarKind,
    CalendarSource,
    TransportDefault,
    TransportDirection,
    TransportMode,
)
from family_logistics.domain.models import (
    ExternalPassenger,
    Location,
    Person,
    PersonRole,
    SensitiveStr,
    Vehicle,
)

ADDRESS = "100 Example Lane"
OTHER_ADDRESS = "200 Sample Road"


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def cipher():
    return AddressCipher(SecretStr(generate_key()))


def base_rows(store: RegistryStore) -> None:
    store.upsert_vehicle(Vehicle("family-suv", "family SUV"))
    store.upsert_location(Location("home", "Home"))
    store.upsert_location(Location("school", "School"))
    store.upsert_household(Household("household-e", "Household E"))
    store.upsert_person(Person("riley", "Riley", PersonRole.PARENT, can_drive=True))
    store.upsert_person(Person("alex", "Alex", PersonRole.CHILD))


@pytest.mark.unit
class TestVehiclesAndHouseholds:
    def test_upsert_and_update(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            store.upsert_vehicle(Vehicle("family-suv", "family SUV"))
            store.upsert_vehicle(Vehicle("family-suv", "family SUV", seat_capacity=7))
            store.upsert_household(Household("household-e", "Household E"))
            store.upsert_household(Household("household-e", "Family E"))
        with engine.connect() as connection:
            store = RegistryStore(connection)
            assert store.vehicles() == [Vehicle("family-suv", "family SUV", 7)]
            assert store.households() == [Household("household-e", "Family E")]


@pytest.mark.unit
class TestLocations:
    def test_address_is_stored_encrypted(self, engine, cipher):
        with engine.begin() as connection:
            RegistryStore(connection, cipher).upsert_location(
                Location("home", "Home", address=SensitiveStr(ADDRESS))
            )
        with engine.connect() as connection:
            raw = connection.execute(
                select(location_table.c.address_ciphertext)
            ).scalar_one()
            dump = "".join(
                str(r) for r in connection.execute(text("SELECT * FROM location"))
            )
            stored_class = connection.execute(
                text("SELECT typeof(address_ciphertext) FROM location")
            ).scalar_one()
        # FernetToken hands the token back as text; SQLite keeps it as a blob.
        assert isinstance(raw, str)
        assert raw.startswith("gAAAAA")
        assert stored_class == "blob"
        assert ADDRESS not in raw
        assert ADDRESS not in dump

    def test_read_back_with_and_without_cipher(self, engine, cipher):
        with engine.begin() as connection:
            RegistryStore(connection, cipher).upsert_location(
                Location(
                    "home",
                    "Home",
                    aliases=("House",),
                    verified=True,
                    address=SensitiveStr(ADDRESS),
                    coordinates=SensitiveStr("1.0,2.0"),
                )
            )
        with engine.connect() as connection:
            [with_key] = RegistryStore(connection, cipher).locations()
            [without_key] = RegistryStore(connection).locations()
            presence = RegistryStore(connection).protected_presence()
        assert with_key.address == SensitiveStr(ADDRESS)
        assert with_key.coordinates == SensitiveStr("1.0,2.0")
        assert (with_key.aliases, with_key.verified) == (("House",), True)
        assert without_key.address is None
        assert ADDRESS not in repr(with_key)
        assert presence == {"home": (True, True)}

    def test_address_without_cipher_is_refused(self, engine):
        store_location = Location("home", "Home", address=SensitiveStr(ADDRESS))
        with engine.begin() as connection:
            store = RegistryStore(connection)
            with pytest.raises(ConfigurationError, match="FL_DATA_KEY"):
                store.upsert_location(store_location)

    def test_wrong_key_fails_closed(self, engine, cipher):
        with engine.begin() as connection:
            RegistryStore(connection, cipher).upsert_location(
                Location("home", "Home", address=SensitiveStr(ADDRESS))
            )
        other = AddressCipher(SecretStr(generate_key()))
        with engine.connect() as connection:
            store = RegistryStore(connection, other)
            with pytest.raises(ProtectedDataError):
                store.locations()

    def test_fingerprints_detect_change_without_plaintext(self, engine, cipher):
        with engine.begin() as connection:
            store = RegistryStore(connection, cipher)
            store.upsert_location(
                Location("home", "Home", address=SensitiveStr(ADDRESS))
            )
            first = store.location_fingerprints()["home"]
            store.upsert_location(
                Location("home", "Home", address=SensitiveStr(ADDRESS))
            )
            same = store.location_fingerprints()["home"]
            store.upsert_location(
                Location("home", "Home", address=SensitiveStr(OTHER_ADDRESS))
            )
            changed = store.location_fingerprints()["home"]
        assert first == same
        assert first.address != changed.address
        assert first.address is not None
        assert ADDRESS not in first.address
        assert first.coordinates is None

    def test_fingerprint_needs_a_cipher(self, engine):
        with engine.connect() as connection:
            assert RegistryStore(connection).fingerprint(SensitiveStr(ADDRESS)) is None

    def test_has_protected_data(self, engine, cipher):
        with engine.begin() as connection:
            store = RegistryStore(connection, cipher)
            store.upsert_location(Location("school", "School"))
            assert store.has_protected_data() is False
            store.upsert_location(
                Location("home", "Home", address=SensitiveStr(ADDRESS))
            )
            assert store.has_protected_data() is True


@pytest.mark.unit
class TestPeopleAndAliases:
    def test_round_trip_with_aliases(self, engine):
        riley = Person(
            "riley",
            "Riley",
            PersonRole.PARENT,
            aliases=("R", "r ", "Ri"),
            can_drive=True,
            default_vehicle_id="family-suv",
            home_location_id="home",
        )
        casey = Person(
            "casey",
            "Casey",
            PersonRole.CHILD,
            monitoring_enabled=False,
            monitoring_start=date(2026, 12, 15),
            monitoring_end=date(2027, 1, 5),
        )
        with engine.begin() as connection:
            store = RegistryStore(connection)
            store.upsert_vehicle(Vehicle("family-suv", "family SUV"))
            store.upsert_location(Location("home", "Home"))
            store.upsert_person(riley)
            store.upsert_person(casey)
        with engine.connect() as connection:
            people = {p.person_id: p for p in RegistryStore(connection).people()}
        assert people["riley"].aliases == ("R", "Ri")
        assert people["riley"].default_vehicle_id == "family-suv"
        assert people["casey"] == casey

    def test_alias_taken_by_another_person(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            store.upsert_person(Person("sam", "Sam", PersonRole.CHILD, aliases=("S",)))
            jordan = Person("jordan", "Jordan", PersonRole.CHILD, aliases=("s",))
            with pytest.raises(RegistryError, match="already belongs"):
                store.upsert_person(jordan)

    def test_same_alias_allowed_across_kinds(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            store.upsert_person(
                Person("sam", "Sam", PersonRole.CHILD, aliases=("Studio",))
            )
            store.upsert_location(
                Location("studio", "Music Studio", aliases=("Studio",))
            )
            assert store.aliases_by_target("location") == {"studio": ("Studio",)}

    def test_unknown_alias_kind(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            with pytest.raises(RegistryError, match="unknown alias kind"):
                store.set_aliases("vehicle", "family-suv", ["Van"])

    def test_unknown_vehicle_reference(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            person = Person(
                "riley", "Riley", PersonRole.PARENT, default_vehicle_id="nope"
            )
            with pytest.raises(RegistryError, match="unknown vehicle"):
                store.upsert_person(person)

    def test_deactivate_person(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            store.upsert_person(Person("alex", "Alex", PersonRole.CHILD))
            store.set_person_active("alex", active=False)
            [alex] = store.people()
        assert alex.active is False


@pytest.mark.unit
class TestExternalPassengers:
    def test_round_trip_and_deactivation_keeps_first_day(self, engine):
        passenger = ExternalPassenger(
            "ext-e",
            "Passenger E",
            "household-e",
            contact_id="riley",
            default_dropoff_location_id="home",
            alternate_destination_ids=("school",),
            aliases=("E",),
        )
        with engine.begin() as connection:
            store = RegistryStore(connection)
            base_rows(store)
            store.upsert_external_passenger(passenger)
            store.deactivate_external_passenger("ext-e", since=date(2026, 11, 1))
            store.deactivate_external_passenger("ext-e", since=date(2026, 12, 1))
            [stored] = store.external_passengers()
            since = store.passenger_inactive_since()
        assert stored.alternate_destination_ids == ("school",)
        assert stored.aliases == ("E",)
        assert stored.active is False
        assert since == {"ext-e": date(2026, 11, 1)}

    def test_active_passenger_has_no_inactive_day(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            base_rows(store)
            store.upsert_external_passenger(
                ExternalPassenger("ext-e", "Passenger E", "household-e"),
                inactive_since=date(2026, 11, 1),
            )
            assert store.passenger_inactive_since() == {}

    def test_unknown_household(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            passenger = ExternalPassenger("ext-e", "Passenger E", "nope")
            with pytest.raises(RegistryError, match="unknown household"):
                store.upsert_external_passenger(passenger)


@pytest.mark.unit
class TestTransportDefaults:
    def default(self, **overrides) -> TransportDefault:
        values = {
            "person_id": "alex",
            "weekdays": frozenset({0, 2, 4}),
            "direction": TransportDirection.TO_SCHOOL,
            "mode": TransportMode.BIKE,
            "origin_location_id": "home",
            "destination_location_id": "school",
        }
        values.update(overrides)
        return TransportDefault(**values)  # type: ignore[arg-type]

    def test_replace_and_read(self, engine):
        parent = self.default(
            mode=TransportMode.PARENT,
            driver_id="riley",
            effective_from=date(2026, 9, 1),
            effective_to=date(2027, 6, 15),
        )
        with engine.begin() as connection:
            store = RegistryStore(connection)
            base_rows(store)
            store.replace_transport_defaults("alex", [self.default()])
            store.replace_transport_defaults("alex", [self.default(), parent])
            stored = store.transport_defaults("alex")
            everyone = store.transport_defaults()
        assert stored == [self.default(), parent]
        assert everyone == stored

    def test_default_for_another_person(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            base_rows(store)
            defaults = [self.default(person_id="riley")]
            with pytest.raises(RegistryError, match="given for alex"):
                store.replace_transport_defaults("alex", defaults)

    def test_unknown_location(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            base_rows(store)
            defaults = [self.default(destination_location_id="nope")]
            with pytest.raises(RegistryError, match="unknown ID"):
                store.replace_transport_defaults("alex", defaults)


@pytest.mark.unit
class TestCalendarSources:
    def test_upsert_keeps_collection_status(self, engine):
        source = CalendarSource(
            "cal-a", "Family Transportation", CalendarKind.TRANSPORT
        )
        seen = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
        with engine.begin() as connection:
            store = RegistryStore(connection)
            store.upsert_calendar_source(source)
            store.record_calendar_success("cal-a", seen)
            store.record_calendar_failure("cal-a", "timeout " * 100)
            store.upsert_calendar_source(
                CalendarSource("cal-a", "Transport", CalendarKind.TRANSPORT)
            )
            [stored] = store.calendar_sources()
        assert stored.display_name == "Transport"
        assert stored.last_success == seen
        assert stored.last_error is not None
        assert len(stored.last_error) == 500

    def test_success_clears_error_and_enabled_filter(self, engine):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            base_rows(store)
            store.upsert_calendar_source(
                CalendarSource("cal-a", "Transport", CalendarKind.TRANSPORT)
            )
            store.upsert_calendar_source(
                CalendarSource("cal-b", "Alex", CalendarKind.PERSON, person_id="alex")
            )
            store.record_calendar_failure("cal-a", "timeout")
            store.record_calendar_success("cal-a", datetime(2026, 10, 21, tzinfo=UTC))
            store.set_calendar_enabled("cal-b", enabled=False)
            enabled = store.calendar_sources(enabled_only=True)
        assert [c.calendar_id for c in enabled] == ["cal-a"]
        assert enabled[0].last_error is None


@pytest.mark.unit
class TestSnapshot:
    def test_snapshot_feeds_domain_lookups(self, engine, cipher):
        with engine.begin() as connection:
            store = RegistryStore(connection, cipher)
            base_rows(store)
            store.upsert_location(
                Location("loc-e", "E home", address=SensitiveStr(ADDRESS))
            )
            store.upsert_person(
                Person("sam", "Sam", PersonRole.CHILD, aliases=("Sammy",))
            )
            store.upsert_external_passenger(
                ExternalPassenger("ext-e", "Passenger E", "household-e")
            )
        snapshot = load_snapshot(engine, cipher)
        sam = snapshot.registry.resolve_family_child("sammy")
        assert sam is not None
        assert sam.person_id == "sam"
        assert [
            e.passenger_id for e in snapshot.registry.resolve_external("Passenger E")
        ] == ["ext-e"]
        e_home = snapshot.location("loc-e")
        assert e_home is not None
        assert e_home.address == SensitiveStr(ADDRESS)
        assert snapshot.location("nope") is None
        assert [h.household_id for h in snapshot.households] == ["household-e"]

    def test_snapshot_without_cipher_hides_addresses(self, engine, cipher):
        with engine.begin() as connection:
            RegistryStore(connection, cipher).upsert_location(
                Location("home", "Home", address=SensitiveStr(ADDRESS))
            )
        home = load_snapshot(engine).location("home")
        assert home is not None
        assert home.address is None
