"""Shared builders for the ha_relay route provider tests.

Locations are fictional. Addresses are obviously fake placeholders that no
other test data uses, so a test can assert they never appear in a response.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from sqlalchemy import event

from family_logistics.core.crypto import AddressCipher, generate_key
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.registry import Household, RegistryStore
from family_logistics.domain.models import (
    ExternalPassenger,
    Location,
    Person,
    PersonRole,
    SensitiveStr,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

ZONE = ZoneInfo("America/Los_Angeles")
DATA_KEY = SecretStr(generate_key())
CIPHER = AddressCipher(DATA_KEY)

HOME_ADDRESS = "1 Placeholder Row, Exampleton"
STUDIO_ADDRESS = "2 Placeholder Row, Exampleton"
DRIVER_S_ADDRESS = "3 Placeholder Row, Exampleton"
PASSENGER_E_ADDRESS = "4 Placeholder Row, Exampleton"

LOCATIONS = (
    Location("home", "Family Home", verified=True, ha_zone_entity_id="zone.home_base"),
    Location(
        "high-school", "High School", verified=True, ha_zone_entity_id="zone.school_a"
    ),
    Location("middle-school", "Middle School", verified=True),
    Location("studio", "Music Studio", verified=True),
    Location("xc", "XC Practice", verified=True, ha_zone_entity_id="zone.field_a"),
)
"""``home``, ``high-school`` and ``xc`` have zones; ``studio`` and ``middle-school``
have none. ``make_engine`` then stores an address for ``home`` (alongside its
zone) and for ``studio`` (its only endpoint), so ``middle-school`` is the one
location with neither a zone nor an address."""

OTHER_FAMILY_HOMES = (
    ("driver-s-home", "Driver S Home", DRIVER_S_ADDRESS),
    ("passenger-e-home", "Passenger E Home", PASSENGER_E_ADDRESS),
)
"""Zone-less homes with an address. Nobody is registered at them until a test
calls ``register_person``, so the policy sees them as venues at first."""


def local(
    hour: int, minute: int = 0, *, month: int = 10, day: int = 23, second: int = 0
) -> datetime:
    """Return a UTC instant from a local wall-clock time in 2026."""
    return datetime(2026, month, day, hour, minute, second, tzinfo=ZONE).astimezone(UTC)


NOW = local(7, 0)
"""Friday 2026-10-23 07:00 local."""


def make_engine(tmp_path: Path) -> Engine:
    """Create a migrated database holding the fictional locations."""
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    with engine.begin() as connection:
        store = RegistryStore(connection, CIPHER)
        for item in LOCATIONS:
            store.upsert_location(item)
        store.upsert_location(
            Location(
                "studio",
                "Music Studio",
                verified=True,
                address=SensitiveStr(STUDIO_ADDRESS),
            )
        )
        store.upsert_location(
            Location(
                "home",
                "Family Home",
                verified=True,
                ha_zone_entity_id="zone.home_base",
                address=SensitiveStr(HOME_ADDRESS),
            )
        )
        for location_id, name, address in OTHER_FAMILY_HOMES:
            store.upsert_location(
                Location(
                    location_id, name, verified=True, address=SensitiveStr(address)
                )
            )
    return engine


def register_person(
    engine: Engine, person_id: str, role: PersonRole, home_location_id: str | None
) -> None:
    """Register a person (a household member or a contact) with a home."""
    with engine.begin() as connection:
        RegistryStore(connection, CIPHER).upsert_person(
            Person(
                person_id,
                person_id.replace("-", " ").title(),
                role,
                home_location_id=home_location_id,
            )
        )


def register_passenger_default(
    engine: Engine, passenger_id: str, pickup_location_id: str
) -> None:
    """Register an external passenger whose default pickup is a location.

    No contact is linked, so the policy treats the pickup as a venue.
    """
    with engine.begin() as connection:
        store = RegistryStore(connection, CIPHER)
        store.upsert_household(Household(f"household-{passenger_id}", "Household"))
        store.upsert_external_passenger(
            ExternalPassenger(
                passenger_id,
                "E",
                f"household-{passenger_id}",
                default_pickup_location_id=pickup_location_id,
            )
        )


def clear_zone(engine: Engine, location_id: str) -> None:
    """Remove a location's Home Assistant zone (the address stays)."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE location SET ha_zone_entity_id = NULL WHERE location_id = ?",
            (location_id,),
        )


def make_writes_fail(engine: Engine) -> None:
    """Make every new connection refuse writes, which is not lock contention.

    ``PRAGMA query_only`` makes ``BEGIN IMMEDIATE`` fail with SQLITE_READONLY, an
    ``OperationalError`` that is neither SQLITE_BUSY nor SQLITE_LOCKED.
    """

    @event.listens_for(engine, "connect")
    def _read_only(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("PRAGMA query_only=ON")

    engine.dispose()


def days(count: float) -> timedelta:
    """Return a timedelta of ``count`` days."""
    return timedelta(days=count)


class Clock:
    """A settable clock returning an aware instant."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def audit_key_text() -> str:
    """Return a valid ``FL_AUDIT_KEY`` value built at run time (no literal)."""
    return base64.urlsafe_b64encode(bytes(range(32))).decode()
