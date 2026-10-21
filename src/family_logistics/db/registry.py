"""Registry repositories (SQLAlchemy Core) and the domain snapshot adapter.

The registry holds the household's reference data: people, external
passengers, households, locations, vehicles, aliases, school transport defaults
and calendar sources. ``RegistryStore`` reads and writes these rows on one
connection, so a caller controls the transaction (the seed loader applies a
whole seed atomically). ``load_snapshot`` turns the rows into the immutable
domain objects the parser and engine use.

#CRITICAL: Security: addresses and coordinates are written only as Fernet
ciphertext (``core.crypto``) and come back wrapped in ``SensitiveStr``, which
never renders in ``repr()``, ``str()`` or logs. Storing an address without
``FL_DATA_KEY`` is refused (``require_cipher`` semantics). Error messages name
identifiers and fields only, never names, aliases or addresses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from family_logistics.core.crypto import AddressCipher, ProtectedDataError
from family_logistics.core.exceptions import ConfigurationError, ValidationError
from family_logistics.db.schema import (
    alias,
    calendar_source,
    external_passenger,
    household,
    location,
    person,
    transport_default,
    vehicle,
)
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
from family_logistics.domain.registry import Registry

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.sql.base import Executable

    Row = dict[str, object]

ALIAS_KINDS = ("person", "passenger", "location")
"""Alias namespaces; uniqueness is case-insensitive within a namespace."""

VERIFIED = "verified"
UNVERIFIED = "unverified"


class RegistryError(ValidationError):
    """A registry write was refused (conflict, missing reference, bad value).

    Messages carry identifiers and field names only, never household data.
    """


@dataclass(frozen=True, slots=True)
class Household:
    """A family of external passengers (a label only; contacts are people).

    Attributes:
        household_id: Stable identifier.
        label: Display label, e.g. ``Household E``.
        privacy_scope: ``external`` for other families.
    """

    household_id: str
    label: str
    privacy_scope: str = "external"


@dataclass(frozen=True, slots=True)
class ProtectedFingerprints:
    """Keyed digests of a location's protected fields (change detection only).

    Attributes:
        address: Digest of the address, or None when unset.
        coordinates: Digest of the coordinates, or None when unset.
    """

    address: str | None
    coordinates: str | None


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """Immutable view of the whole registry for the parser and engine.

    Attributes:
        registry: People, external passengers and vehicles for name resolution.
        locations: All locations (protected fields decrypted only when a
            cipher was supplied).
        households: All households.
        transport_defaults: All school transport defaults.
        calendar_sources: All configured calendars.
    """

    registry: Registry
    locations: tuple[Location, ...]
    households: tuple[Household, ...]
    transport_defaults: tuple[TransportDefault, ...]
    calendar_sources: tuple[CalendarSource, ...]

    def location(self, location_id: str) -> Location | None:
        """Look up a location by ID.

        Args:
            location_id: Location identifier.

        Returns:
            The location, or None.
        """
        return next((x for x in self.locations if x.location_id == location_id), None)


# ---------------------------------------------------------------------------
# Row value helpers (rows arrive as dict[str, object]; nothing is trusted)
# ---------------------------------------------------------------------------


def _text(row: Row, key: str) -> str:
    """Return a required text column."""
    value = row[key]
    if not isinstance(value, str):
        msg = f"registry column {key} is not text"
        raise TypeError(msg)
    return value


def _opt_text(row: Row, key: str) -> str | None:
    """Return an optional text column."""
    value = row[key]
    return None if value is None else str(value)


def _flag(row: Row, key: str) -> bool:
    """Return a boolean column (SQLite stores 0/1)."""
    return bool(row[key])


def _opt_int(row: Row, key: str) -> int | None:
    """Return an optional integer column."""
    value = row[key]
    if value is None:
        return None
    if not isinstance(value, int):
        msg = f"registry column {key} is not an integer"
        raise TypeError(msg)
    return value


def _opt_date(row: Row, key: str) -> date | None:
    """Return an optional date column."""
    value = row[key]
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, date):
        msg = f"registry column {key} is not a date"
        raise TypeError(msg)
    return value


def _opt_datetime(row: Row, key: str) -> datetime | None:
    """Return an optional timestamp column."""
    value = row[key]
    if value is None:
        return None
    if not isinstance(value, datetime):
        msg = f"registry column {key} is not a timestamp"
        raise TypeError(msg)
    # #ASSUME: Data integrity: every stored timestamp is UTC; SQLite drops the
    # offset on the way back, so it is restored here.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _opt_bytes(row: Row, key: str) -> bytes | None:
    """Return an optional binary column."""
    value = row[key]
    if value is None:
        return None
    if isinstance(value, memoryview):
        return value.tobytes()
    if not isinstance(value, bytes):
        msg = f"registry column {key} is not binary"
        raise TypeError(msg)
    return value


def _str_list(row: Row, key: str) -> tuple[str, ...]:
    """Return a JSON list-of-strings column."""
    value = row[key]
    if value is None:
        return ()
    if not isinstance(value, list):
        msg = f"registry column {key} is not a list"
        raise TypeError(msg)
    items = cast("list[object]", value)
    return tuple(str(item) for item in items)


def _int_set(row: Row, key: str) -> frozenset[int]:
    """Return a JSON list-of-integers column as a set."""
    value = row[key]
    if not isinstance(value, list):
        msg = f"registry column {key} is not a list"
        raise TypeError(msg)
    items = cast("list[object]", value)
    result: set[int] = set()
    for item in items:
        if not isinstance(item, int):
            msg = f"registry column {key} holds a non-integer"
            raise TypeError(msg)
        result.add(item)
    return frozenset(result)


def _rows(connection: Connection, query: Executable) -> list[Row]:
    """Execute a select and return plain dict rows."""
    return [dict(mapping) for mapping in connection.execute(query).mappings()]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class RegistryStore:
    """Reads and writes registry rows on one connection.

    The caller owns the transaction: open it with ``engine.begin()`` to write
    atomically, or ``engine.connect()`` to read.

    Args:
        connection: An open SQLAlchemy connection (schema at head).
        cipher: Address cipher from ``FL_DATA_KEY``. Required to write a
            protected address and to read one back; without it protected
            fields read as None.
    """

    def __init__(
        self, connection: Connection, cipher: AddressCipher | None = None
    ) -> None:
        self._conn = connection
        self._cipher = cipher

    # -- vehicles ------------------------------------------------------------

    def upsert_vehicle(self, item: Vehicle) -> None:
        """Insert or update a vehicle.

        Args:
            item: The vehicle.
        """
        values = {
            "vehicle_id": item.vehicle_id,
            "display_name": item.display_name,
            "seat_capacity": item.seat_capacity,
        }
        stmt = sqlite_insert(vehicle).values(values)
        self._conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[vehicle.c.vehicle_id],
                set_={k: v for k, v in values.items() if k != "vehicle_id"},
            )
        )

    def vehicles(self) -> list[Vehicle]:
        """Return all vehicles ordered by ID.

        Returns:
            The vehicles.
        """
        rows = _rows(self._conn, select(vehicle).order_by(vehicle.c.vehicle_id))
        return [
            Vehicle(
                vehicle_id=_text(r, "vehicle_id"),
                display_name=_text(r, "display_name"),
                seat_capacity=_opt_int(r, "seat_capacity"),
            )
            for r in rows
        ]

    # -- households ----------------------------------------------------------

    def upsert_household(self, item: Household) -> None:
        """Insert or update a household.

        Args:
            item: The household.
        """
        values = {
            "household_id": item.household_id,
            "label": item.label,
            "privacy_scope": item.privacy_scope,
        }
        stmt = sqlite_insert(household).values(values)
        self._conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[household.c.household_id],
                set_={k: v for k, v in values.items() if k != "household_id"},
            )
        )

    def households(self) -> list[Household]:
        """Return all households ordered by ID.

        Returns:
            The households.
        """
        rows = _rows(self._conn, select(household).order_by(household.c.household_id))
        return [
            Household(
                household_id=_text(r, "household_id"),
                label=_text(r, "label"),
                privacy_scope=_text(r, "privacy_scope"),
            )
            for r in rows
        ]

    # -- locations -----------------------------------------------------------

    def _encrypt(self, value: SensitiveStr | None, field_name: str) -> bytes | None:
        """Encrypt a protected value, refusing without a cipher."""
        if value is None:
            return None
        if self._cipher is None:
            # #CRITICAL: Security: never store a protected address in clear.
            msg = f"FL_DATA_KEY is required before storing a location {field_name}"
            raise ConfigurationError(msg)
        return self._cipher.encrypt(value).encode("ascii")

    def _decrypt(self, token: bytes | None) -> SensitiveStr | None:
        """Decrypt a protected value; None without a cipher or a value."""
        if token is None or self._cipher is None:
            return None
        return self._cipher.decrypt(token.decode("ascii"))

    def upsert_location(self, item: Location) -> None:
        """Insert or update a location and its aliases.

        Storing an address or coordinates without a cipher raises
        ``ConfigurationError`` (from ``_encrypt``).

        Args:
            item: The location; protected fields are encrypted before storage.
        """
        values = {
            "location_id": item.location_id,
            "name": item.name,
            "ha_zone_entity_id": item.ha_zone_entity_id,
            "address_ciphertext": self._encrypt(item.address, "address"),
            "latlon_ciphertext": self._encrypt(item.coordinates, "coordinates"),
            "verification_state": VERIFIED if item.verified else UNVERIFIED,
        }
        stmt = sqlite_insert(location).values(values)
        self._conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[location.c.location_id],
                set_={k: v for k, v in values.items() if k != "location_id"},
            )
        )
        self.set_aliases("location", item.location_id, item.aliases)

    def locations(self) -> list[Location]:
        """Return all locations ordered by ID.

        A stored value that the configured key cannot decrypt raises
        ``ProtectedDataError`` (fail closed).

        Returns:
            The locations; protected fields are decrypted only with a cipher.
        """
        aliases = self.aliases_by_target("location")
        rows = _rows(self._conn, select(location).order_by(location.c.location_id))
        return [
            Location(
                location_id=_text(r, "location_id"),
                name=_text(r, "name"),
                aliases=aliases.get(_text(r, "location_id"), ()),
                verified=_text(r, "verification_state") == VERIFIED,
                ha_zone_entity_id=_opt_text(r, "ha_zone_entity_id"),
                address=self._decrypt(_opt_bytes(r, "address_ciphertext")),
                coordinates=self._decrypt(_opt_bytes(r, "latlon_ciphertext")),
            )
            for r in rows
        ]

    def location_fingerprints(self) -> dict[str, ProtectedFingerprints]:
        """Return keyed digests of every location's protected fields.

        Raises ``ProtectedDataError`` (via ``locations``) when a stored value
        cannot be decrypted.

        Returns:
            Location ID to fingerprints; empty digests without a cipher.
        """
        result: dict[str, ProtectedFingerprints] = {}
        for item in self.locations():
            result[item.location_id] = ProtectedFingerprints(
                address=self.fingerprint(item.address),
                coordinates=self.fingerprint(item.coordinates),
            )
        return result

    def fingerprint(self, value: SensitiveStr | None) -> str | None:
        """Return the keyed digest of a protected value.

        Args:
            value: The protected value, or None.

        Returns:
            The digest, or None when there is no value or no cipher.
        """
        if value is None or self._cipher is None:
            return None
        return self._cipher.fingerprint(value)

    def protected_presence(self) -> dict[str, tuple[bool, bool]]:
        """Report which locations store an address and coordinates.

        Works without a cipher; nothing is decrypted.

        Returns:
            Location ID to (address stored, coordinates stored).
        """
        rows = _rows(
            self._conn,
            select(
                location.c.location_id,
                location.c.address_ciphertext.is_not(None).label("has_address"),
                location.c.latlon_ciphertext.is_not(None).label("has_coordinates"),
            ),
        )
        return {
            _text(r, "location_id"): (
                _flag(r, "has_address"),
                _flag(r, "has_coordinates"),
            )
            for r in rows
        }

    def has_protected_data(self) -> bool:
        """Report whether any location stores an address or coordinates.

        Returns:
            True when at least one protected field is stored.
        """
        query = select(func.count()).where(
            location.c.address_ciphertext.is_not(None)
            | location.c.latlon_ciphertext.is_not(None)
        )
        count = self._conn.execute(query).scalar_one()
        return bool(count)

    # -- people --------------------------------------------------------------

    def upsert_person(self, item: Person) -> None:
        """Insert or update a person and their aliases.

        Args:
            item: The person.
        """
        values = {
            "person_id": item.person_id,
            "display_name": item.display_name,
            "role": item.role.value,
            "active": item.active,
            "can_drive": item.can_drive,
            "default_vehicle_id": item.default_vehicle_id,
            "monitoring_enabled": item.monitoring_enabled,
            "monitoring_start": item.monitoring_start,
            "monitoring_end": item.monitoring_end,
            "home_location_id": item.home_location_id,
        }
        stmt = sqlite_insert(person).values(values)
        self._execute_checked(
            stmt.on_conflict_do_update(
                index_elements=[person.c.person_id],
                set_={k: v for k, v in values.items() if k != "person_id"},
            ),
            f"person {item.person_id} references an unknown vehicle or location",
        )
        self.set_aliases("person", item.person_id, item.aliases)

    def people(self) -> list[Person]:
        """Return all people ordered by ID.

        Returns:
            The people with their aliases.
        """
        aliases = self.aliases_by_target("person")
        rows = _rows(self._conn, select(person).order_by(person.c.person_id))
        return [
            Person(
                person_id=_text(r, "person_id"),
                display_name=_text(r, "display_name"),
                role=PersonRole(_text(r, "role")),
                aliases=aliases.get(_text(r, "person_id"), ()),
                active=_flag(r, "active"),
                can_drive=_flag(r, "can_drive"),
                default_vehicle_id=_opt_text(r, "default_vehicle_id"),
                monitoring_enabled=_flag(r, "monitoring_enabled"),
                monitoring_start=_opt_date(r, "monitoring_start"),
                monitoring_end=_opt_date(r, "monitoring_end"),
                home_location_id=_opt_text(r, "home_location_id"),
            )
            for r in rows
        ]

    def set_person_active(self, person_id: str, *, active: bool) -> None:
        """Activate or deactivate a person (rows are never deleted).

        Args:
            person_id: Person identifier.
            active: New state.
        """
        self._conn.execute(
            update(person).where(person.c.person_id == person_id).values(active=active)
        )

    # -- external passengers -------------------------------------------------

    def upsert_external_passenger(
        self, item: ExternalPassenger, *, inactive_since: date | None = None
    ) -> None:
        """Insert or update an external passenger and their aliases.

        Args:
            item: The passenger.
            inactive_since: Day the passenger became inactive (drives the
                90-day removal rule); ignored while active.
        """
        values = {
            "passenger_id": item.passenger_id,
            "display_first_name": item.display_first_name,
            "household_id": item.household_id,
            "contact_id": item.contact_id,
            "default_pickup_location_id": item.default_pickup_location_id,
            "default_dropoff_location_id": item.default_dropoff_location_id,
            "alternate_destination_ids": list(item.alternate_destination_ids),
            "active": item.active,
            "inactive_since": None if item.active else inactive_since,
        }
        stmt = sqlite_insert(external_passenger).values(values)
        self._execute_checked(
            stmt.on_conflict_do_update(
                index_elements=[external_passenger.c.passenger_id],
                set_={k: v for k, v in values.items() if k != "passenger_id"},
            ),
            f"passenger {item.passenger_id} references an unknown household or place",
        )
        self.set_aliases("passenger", item.passenger_id, item.aliases)

    def external_passengers(self) -> list[ExternalPassenger]:
        """Return all external passengers ordered by ID.

        Returns:
            The passengers with their aliases.
        """
        aliases = self.aliases_by_target("passenger")
        rows = _rows(
            self._conn,
            select(external_passenger).order_by(external_passenger.c.passenger_id),
        )
        return [
            ExternalPassenger(
                passenger_id=_text(r, "passenger_id"),
                display_first_name=_text(r, "display_first_name"),
                household_id=_text(r, "household_id"),
                contact_id=_opt_text(r, "contact_id"),
                default_pickup_location_id=_opt_text(r, "default_pickup_location_id"),
                default_dropoff_location_id=_opt_text(r, "default_dropoff_location_id"),
                alternate_destination_ids=_str_list(r, "alternate_destination_ids"),
                aliases=aliases.get(_text(r, "passenger_id"), ()),
                active=_flag(r, "active"),
            )
            for r in rows
        ]

    def passenger_inactive_since(self) -> dict[str, date]:
        """Return the inactive-since day of every inactive passenger.

        Returns:
            Passenger ID to the day it became inactive.
        """
        rows = _rows(
            self._conn,
            select(
                external_passenger.c.passenger_id, external_passenger.c.inactive_since
            ).where(external_passenger.c.inactive_since.is_not(None)),
        )
        result: dict[str, date] = {}
        for r in rows:
            since = _opt_date(r, "inactive_since")
            if since is not None:
                result[_text(r, "passenger_id")] = since
        return result

    def deactivate_external_passenger(self, passenger_id: str, *, since: date) -> None:
        """Mark a passenger inactive, keeping the first inactive day.

        Args:
            passenger_id: Passenger identifier.
            since: Day the carpool relationship ended.
        """
        self._conn.execute(
            update(external_passenger)
            .where(
                (external_passenger.c.passenger_id == passenger_id)
                & external_passenger.c.inactive_since.is_(None)
            )
            .values(active=False, inactive_since=since)
        )

    # -- aliases -------------------------------------------------------------

    def set_aliases(self, kind: str, target_id: str, aliases: Iterable[str]) -> None:
        """Replace the aliases of one target.

        Args:
            kind: ``person``, ``passenger`` or ``location``.
            target_id: The target's identifier.
            aliases: The complete new alias list (case-insensitive duplicates
                are collapsed).

        Raises:
            RegistryError: If the kind is unknown or an alias already belongs
                to another target of the same kind.
        """
        if kind not in ALIAS_KINDS:
            msg = f"unknown alias kind {kind!r}"
            raise RegistryError(msg, field="kind")
        self._conn.execute(
            delete(alias).where(
                (alias.c.kind == kind) & (alias.c.target_id == target_id)
            )
        )
        seen: set[str] = set()
        for name in aliases:
            cleaned = " ".join(name.split())
            if not cleaned or cleaned.casefold() in seen:
                continue
            seen.add(cleaned.casefold())
            self._execute_checked(
                alias.insert().values(kind=kind, alias=cleaned, target_id=target_id),
                f"an alias of {kind} {target_id} already belongs to another {kind}",
            )

    def aliases_by_target(self, kind: str) -> dict[str, tuple[str, ...]]:
        """Return the aliases of every target of one kind.

        Args:
            kind: ``person``, ``passenger`` or ``location``.

        Returns:
            Target ID to its aliases in insertion order.
        """
        rows = _rows(
            self._conn,
            select(alias.c.target_id, alias.c.alias)
            .where(alias.c.kind == kind)
            .order_by(alias.c.id),
        )
        grouped: dict[str, list[str]] = {}
        for r in rows:
            grouped.setdefault(_text(r, "target_id"), []).append(_text(r, "alias"))
        return {k: tuple(v) for k, v in grouped.items()}

    # -- transport defaults --------------------------------------------------

    def replace_transport_defaults(
        self, person_id: str, defaults: Sequence[TransportDefault]
    ) -> None:
        """Replace every school transport default of one person.

        Args:
            person_id: Traveller.
            defaults: The complete new set; each must belong to ``person_id``.

        Raises:
            RegistryError: If a default belongs to another person or references
                an unknown person or location.
        """
        self._conn.execute(
            delete(transport_default).where(transport_default.c.person_id == person_id)
        )
        for item in defaults:
            if item.person_id != person_id:
                msg = f"transport default for {item.person_id} given for {person_id}"
                raise RegistryError(msg, field="person_id")
            self._execute_checked(
                transport_default.insert().values(
                    person_id=item.person_id,
                    weekdays=sorted(item.weekdays),
                    direction=item.direction.value,
                    mode=item.mode.value,
                    driver_id=item.driver_id,
                    effective_from=item.effective_from,
                    effective_to=item.effective_to,
                    origin_location_id=item.origin_location_id,
                    destination_location_id=item.destination_location_id,
                ),
                f"transport default of {person_id} references an unknown ID",
            )

    def transport_defaults(
        self, person_id: str | None = None
    ) -> list[TransportDefault]:
        """Return school transport defaults.

        Args:
            person_id: Limit to one traveller; None returns all.

        Returns:
            The defaults ordered by person and row order.
        """
        query = select(transport_default).order_by(
            transport_default.c.person_id, transport_default.c.id
        )
        if person_id is not None:
            query = query.where(transport_default.c.person_id == person_id)
        return [
            TransportDefault(
                person_id=_text(r, "person_id"),
                weekdays=_int_set(r, "weekdays"),
                direction=TransportDirection(_text(r, "direction")),
                mode=TransportMode(_text(r, "mode")),
                origin_location_id=_text(r, "origin_location_id"),
                destination_location_id=_text(r, "destination_location_id"),
                driver_id=_opt_text(r, "driver_id"),
                effective_from=_opt_date(r, "effective_from"),
                effective_to=_opt_date(r, "effective_to"),
            )
            for r in _rows(self._conn, query)
        ]

    # -- calendar sources ----------------------------------------------------

    def upsert_calendar_source(self, item: CalendarSource) -> None:
        """Insert or update a calendar's configuration.

        Collection status (``last_success``, ``last_error``) is kept on update:
        it belongs to the collector, not to the registry seed.

        Args:
            item: The calendar source.
        """
        config = {
            "display_name": item.display_name,
            "kind": item.kind.value,
            "person_id": item.person_id,
            "enabled": item.enabled,
        }
        stmt = sqlite_insert(calendar_source).values(
            calendar_id=item.calendar_id,
            last_success=item.last_success,
            last_error=item.last_error,
            **config,
        )
        self._conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[calendar_source.c.calendar_id], set_=config
            )
        )

    def calendar_sources(self, *, enabled_only: bool = False) -> list[CalendarSource]:
        """Return configured calendars ordered by ID.

        Args:
            enabled_only: Return only calendars the collector should read.

        Returns:
            The calendar sources.
        """
        query = select(calendar_source).order_by(calendar_source.c.calendar_id)
        if enabled_only:
            query = query.where(calendar_source.c.enabled.is_(True))
        return [
            CalendarSource(
                calendar_id=_text(r, "calendar_id"),
                display_name=_text(r, "display_name"),
                kind=CalendarKind(_text(r, "kind")),
                person_id=_opt_text(r, "person_id"),
                enabled=_flag(r, "enabled"),
                last_success=_opt_datetime(r, "last_success"),
                last_error=_opt_text(r, "last_error"),
            )
            for r in _rows(self._conn, query)
        ]

    def set_calendar_enabled(self, calendar_id: str, *, enabled: bool) -> None:
        """Enable or disable a calendar without losing its status.

        Args:
            calendar_id: Calendar identifier.
            enabled: New state.
        """
        self._conn.execute(
            update(calendar_source)
            .where(calendar_source.c.calendar_id == calendar_id)
            .values(enabled=enabled)
        )

    def record_calendar_success(self, calendar_id: str, at: datetime) -> None:
        """Record a complete collection of one calendar.

        Args:
            calendar_id: Calendar identifier.
            at: UTC time of the successful collection.
        """
        self._conn.execute(
            update(calendar_source)
            .where(calendar_source.c.calendar_id == calendar_id)
            .values(last_success=at, last_error=None)
        )

    def record_calendar_failure(self, calendar_id: str, error: str) -> None:
        """Record a failed collection, keeping the last success time.

        Args:
            calendar_id: Calendar identifier.
            error: Short description; must not contain credentials or data.
        """
        self._conn.execute(
            update(calendar_source)
            .where(calendar_source.c.calendar_id == calendar_id)
            .values(last_error=error[:500])
        )

    # -- internals -----------------------------------------------------------

    def _execute_checked(self, statement: Executable, conflict_message: str) -> None:
        """Execute a write, turning integrity errors into RegistryError."""
        try:
            self._conn.execute(statement)
        except IntegrityError as exc:
            raise RegistryError(conflict_message) from exc


def load_snapshot(
    engine: Engine, cipher: AddressCipher | None = None
) -> RegistrySnapshot:
    """Read the whole registry into immutable domain objects.

    Args:
        engine: Application engine (schema at head).
        cipher: Address cipher; without it protected fields read as None.

    Raises ``ProtectedDataError`` (via ``RegistryStore.locations``) when a
    protected value cannot be decrypted with the configured key.

    Returns:
        The registry snapshot.
    """
    with engine.connect() as connection:
        store = RegistryStore(connection, cipher)
        people = store.people()
        passengers = store.external_passengers()
        vehicles = store.vehicles()
        return RegistrySnapshot(
            registry=Registry(
                people=tuple(people),
                external_passengers=tuple(passengers),
                vehicles=tuple(vehicles),
            ),
            locations=tuple(store.locations()),
            households=tuple(store.households()),
            transport_defaults=tuple(store.transport_defaults()),
            calendar_sources=tuple(store.calendar_sources()),
        )


__all__ = [
    "ALIAS_KINDS",
    "Household",
    "ProtectedDataError",
    "ProtectedFingerprints",
    "RegistryError",
    "RegistrySnapshot",
    "RegistryStore",
    "load_snapshot",
]
