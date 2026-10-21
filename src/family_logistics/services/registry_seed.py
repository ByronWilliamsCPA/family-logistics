"""Registry seed loader.

The household's reference data (people, external passengers, locations,
vehicles, calendars, school transport defaults) is maintained in one private
YAML file kept **outside** this public repository (``FL_REGISTRY_SEED``). The
loader validates it, compares it with the database and applies the
difference in one transaction, recording one audit row per changed object
(hashes only). Loading the same file twice changes nothing.

Rules:

- The seed is the source of truth for what it lists. People, external
  passengers and calendars missing from the seed are deactivated, never
  deleted; locations, vehicles and households missing from it are retained
  (other rows may still reference them).
- Addresses and coordinates are stored encrypted. Updating a registry that
  holds any protected value requires ``FL_DATA_KEY`` so an address is never
  silently dropped or compared blind.
- A seed file inside a git working tree is refused unless explicitly allowed,
  because a real seed must never be committed to this public repository.

#CRITICAL: Security: validation errors never echo input values
(``hide_input_in_errors``); messages name sections, IDs and fields only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as PydanticValidationError

from family_logistics.core.exceptions import ConfigurationError, ValidationError
from family_logistics.db.audit import record_on
from family_logistics.db.registry import Household, RegistryStore
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

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Sequence
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from family_logistics.core.crypto import AddressCipher

SEED_VERSION = 1
SLUG = r"^[a-z0-9][a-z0-9._-]{0,63}$"
CALENDAR_ID = r"^[A-Za-z0-9._:-]{1,100}$"
"""Calendar IDs stay short enough for ``calendar:<id>`` audit references."""
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
COORDINATES = re.compile(r"^\s*-?\d{1,2}(\.\d+)?\s*,\s*-?\d{1,3}(\.\d+)?\s*$")
MAX_SEED_BYTES = 1_000_000

SlugId = Annotated[str, StringConstraints(pattern=SLUG)]
CalendarId = Annotated[str, StringConstraints(pattern=CALENDAR_ID)]
ChangeAction = Literal["create", "update", "deactivate"]


class SeedError(ValidationError):
    """The seed file is unreadable, invalid or not allowed at its path."""


class _Model(BaseModel):
    """Base for seed sections: unknown keys rejected, inputs never echoed."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class SeedVehicle(_Model):
    """A vehicle row.

    Attributes:
        id: Vehicle identifier.
        name: Display label.
        seats: Seats including the driver; omit until verified.
    """

    id: SlugId
    name: str = Field(min_length=1, max_length=120)
    seats: int | None = Field(default=None, ge=1, le=20)


class SeedLocation(_Model):
    """A location row; ``address`` and ``coordinates`` are protected.

    Attributes:
        id: Location identifier.
        name: Display name (safe to show).
        aliases: Alternative names accepted in calendar notes.
        address: Street address, stored encrypted.
        coordinates: ``lat,lon``, stored encrypted.
        verified: Whether a human confirmed the address.
        ha_zone: Matching Home Assistant zone entity ID.
    """

    id: SlugId
    name: str = Field(min_length=1, max_length=200)
    aliases: tuple[str, ...] = ()
    address: SecretStr | None = None
    coordinates: SecretStr | None = None
    verified: bool = False
    ha_zone: str | None = Field(default=None, pattern=r"^zone\.[a-z0-9_]+$")

    @field_validator("coordinates")
    @classmethod
    def _coordinates_shape(cls, value: SecretStr | None) -> SecretStr | None:
        """Require ``lat,lon`` decimal degrees.

        Args:
            value: Configured coordinates.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the value is not ``lat,lon``.
        """
        if value is not None and not COORDINATES.match(value.get_secret_value()):
            msg = "coordinates must be 'lat,lon' in decimal degrees"
            raise ValueError(msg)
        return value


class SeedHousehold(_Model):
    """A household of external passengers.

    Attributes:
        id: Household identifier.
        label: Display label, e.g. ``Household E``.
    """

    id: SlugId
    label: str = Field(min_length=1, max_length=120)


class SeedMonitoring(_Model):
    """Transportation-monitoring window of a person.

    Attributes:
        enabled: Whether checks run for the person.
        start: First monitored day (inclusive).
        end: Last monitored day (inclusive).
    """

    enabled: bool = True
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        """Reject an end before the start.

        Returns:
            The unchanged model.

        Raises:
            ValueError: If ``end`` is before ``start``.
        """
        if self.start and self.end and self.end < self.start:
            msg = "monitoring end is before start"
            raise ValueError(msg)
        return self


class SeedPerson(_Model):
    """A family member or a registered contact (external driver).

    Attributes:
        id: Person identifier.
        name: Canonical display name used to match calendar notes.
        role: ``parent``, ``child`` or ``contact``.
        aliases: Alternative spellings accepted as exact matches.
        can_drive: Authorized driver flag; never inferred from calendars.
        vehicle: Default vehicle ID.
        home: Home location ID.
        active: Whether the person is in use.
        monitoring: Monitoring window (children away at university: disabled).
    """

    id: SlugId
    name: str = Field(min_length=1, max_length=120)
    role: PersonRole
    aliases: tuple[str, ...] = ()
    can_drive: bool = False
    vehicle: str | None = None
    home: str | None = None
    active: bool = True
    monitoring: SeedMonitoring = SeedMonitoring()


class SeedPassenger(_Model):
    """A child of another family.

    Attributes:
        id: Stable passenger identifier (never a full name).
        first_name: First-name label used in notes.
        household: Household ID.
        contact: Registered contact (person ID) of the household.
        pickup: Default pickup location ID.
        dropoff: Location used when a destination row says ``DEFAULT``.
        alternates: Other permitted destination location IDs.
        aliases: Alternative spellings.
        active: False once the carpool relationship ends.
    """

    id: SlugId
    first_name: str = Field(min_length=1, max_length=60)
    household: str
    contact: str | None = None
    pickup: str | None = None
    dropoff: str | None = None
    alternates: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    active: bool = True


class SeedCalendar(_Model):
    """A calendar the collector reads.

    Attributes:
        id: Calendar ID as reported by calendar discovery (an href digest).
        name: Display name.
        kind: ``transport`` or ``person`` (school calendars come from Home
            Assistant).
        person: Owner person ID for a ``person`` calendar.
        enabled: Whether the collector reads it.
    """

    id: CalendarId
    name: str = Field(min_length=1, max_length=200)
    kind: CalendarKind
    person: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _owner(self) -> Self:
        """Require an owner for person calendars and none otherwise.

        Returns:
            The unchanged model.

        Raises:
            ValueError: If the owner does not match the kind.
        """
        if (self.kind is CalendarKind.PERSON) != (self.person is not None):
            msg = "a person calendar needs 'person'; other kinds must omit it"
            raise ValueError(msg)
        return self


class SeedTransportDefault(_Model):
    """A recurring, effective-dated school transport arrangement.

    Attributes:
        person: Traveller person ID.
        weekdays: Days it applies: ``mon`` to ``sun`` or 0 (Monday) to 6.
        direction: ``to_school`` or ``from_school``.
        mode: ``bike``, ``bus``, ``walk``, ``parent`` or ``carpool``.
        origin: Start location ID.
        destination: End location ID.
        driver: Usual driver for ``parent`` and ``carpool`` modes.
        effective_from: First day it applies (inclusive).
        effective_to: Last day it applies (inclusive).
    """

    person: str
    weekdays: frozenset[int] = Field(min_length=1)
    direction: TransportDirection
    mode: TransportMode
    origin: str
    destination: str
    driver: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    @field_validator("weekdays", mode="before")
    @classmethod
    def _weekday_names(cls, value: object) -> object:
        """Accept weekday names as well as integers.

        Names are matched case-insensitively and must be the exact
        three-letter abbreviation or the full name (``mon`` or ``Monday``,
        never a prefix such as ``monkey``). Booleans are not weekdays.

        Args:
            value: Raw list from the seed file.

        Returns:
            A list of integers 0-6 for list input; other values unchanged.

        Raises:
            ValueError: If a name or number is not a weekday.
        """
        if not isinstance(value, list | tuple):
            return value
        items = cast("Sequence[object]", value)
        days: list[int] = []
        for item in items:
            name = item.strip().casefold() if isinstance(item, str) else None
            if name in WEEKDAYS:
                days.append(WEEKDAYS.index(name))
            elif name in WEEKDAY_NAMES:
                days.append(WEEKDAY_NAMES.index(name))
            elif (
                isinstance(item, int)
                and not isinstance(item, bool)
                and 0 <= item <= len(WEEKDAYS) - 1
            ):
                days.append(item)
            else:
                msg = "weekdays must be mon-sun or 0-6"
                raise ValueError(msg)
        return days

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """Check the date window and the driver requirement.

        Returns:
            The unchanged model.

        Raises:
            ValueError: If the window is inverted, a driven mode lacks a
                driver, or a self-propelled mode names one.
        """
        if (
            self.effective_from
            and self.effective_to
            and self.effective_to < self.effective_from
        ):
            msg = "effective_to is before effective_from"
            raise ValueError(msg)
        driven = self.mode in {TransportMode.PARENT, TransportMode.CARPOOL}
        if driven and self.driver is None:
            msg = f"mode {self.mode.value} needs a driver"
            raise ValueError(msg)
        if not driven and self.driver is not None:
            msg = f"mode {self.mode.value} must not name a driver"
            raise ValueError(msg)
        return self


class RegistrySeed(_Model):
    """The whole seed file.

    Attributes:
        version: Seed format version (1).
        vehicles: Vehicles.
        locations: Locations.
        households: Households of external passengers.
        people: Family members and contacts.
        external_passengers: Children of other families.
        calendars: Calendars to collect.
        transport_defaults: School transport defaults.
    """

    version: Literal[1]
    vehicles: tuple[SeedVehicle, ...] = ()
    locations: tuple[SeedLocation, ...] = ()
    households: tuple[SeedHousehold, ...] = ()
    people: tuple[SeedPerson, ...] = ()
    external_passengers: tuple[SeedPassenger, ...] = ()
    calendars: tuple[SeedCalendar, ...] = ()
    transport_defaults: tuple[SeedTransportDefault, ...] = ()

    @model_validator(mode="after")
    def _references(self) -> Self:
        """Check unique IDs and that every reference resolves within the seed.

        Returns:
            The unchanged model.

        Raises:
            ValueError: On a duplicate ID, a duplicate alias within one kind
                or an unresolved reference.
        """
        problems = [
            *_duplicate_ids(self),
            *_duplicate_aliases(self),
            *_unresolved_references(self),
        ]
        if problems:
            raise ValueError("; ".join(problems))
        return self


def _duplicate_ids(seed: RegistrySeed) -> list[str]:
    """Return one problem per ID listed twice in a section."""
    problems: list[str] = []
    for section, ids in (
        ("vehicles", [v.id for v in seed.vehicles]),
        ("locations", [x.id for x in seed.locations]),
        ("households", [h.id for h in seed.households]),
        ("people", [p.id for p in seed.people]),
        ("external_passengers", [e.id for e in seed.external_passengers]),
        ("calendars", [c.id for c in seed.calendars]),
    ):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        problems.extend(f"{section}: duplicate id {d}" for d in dupes)
    return problems


def _alias_key(alias: str) -> str:
    """Normalize an alias the way the registry's unique index compares it."""
    return " ".join(alias.split()).casefold()


def _duplicate_aliases(seed: RegistrySeed) -> list[str]:
    """Return one problem per alias claimed by two targets of one kind.

    Aliases are compared after collapsing whitespace and case-folding, the
    same normalization ``RegistryStore.set_aliases`` applies. Only the kind
    and the target IDs are named, never the alias text.
    """
    problems: list[str] = []
    for section, targets in (
        ("people", [(p.id, p.aliases) for p in seed.people]),
        ("external_passengers", [(e.id, e.aliases) for e in seed.external_passengers]),
        ("locations", [(x.id, x.aliases) for x in seed.locations]),
    ):
        owners: dict[str, str] = {}
        for target_id, aliases in targets:
            for key in {_alias_key(a) for a in aliases if a.strip()}:
                owner = owners.setdefault(key, target_id)
                if owner != target_id:
                    problems.append(
                        f"{section}: {target_id} repeats an alias of {owner}"
                    )
    return sorted(set(problems))


def _unresolved_references(seed: RegistrySeed) -> list[str]:
    """Return one problem per reference that does not resolve in the seed."""
    known = {
        "vehicle": {v.id for v in seed.vehicles},
        "location": {x.id for x in seed.locations},
        "household": {h.id for h in seed.households},
        "person": {p.id for p in seed.people},
    }
    refs: list[tuple[str, str, str | None]] = []
    for p in seed.people:
        refs += [
            (f"people.{p.id}", "vehicle", p.vehicle),
            (f"people.{p.id}", "location", p.home),
        ]
    for e in seed.external_passengers:
        where = f"external_passengers.{e.id}"
        refs += [(where, "household", e.household), (where, "person", e.contact)]
        refs += [
            (where, "location", ref) for ref in (e.pickup, e.dropoff, *e.alternates)
        ]
    refs += [(f"calendars.{c.id}", "person", c.person) for c in seed.calendars]
    for i, t in enumerate(seed.transport_defaults):
        where = f"transport_defaults[{i}]"
        refs += [(where, "person", t.person), (where, "person", t.driver)]
        refs += [(where, "location", t.origin), (where, "location", t.destination)]
    problems = [
        f"{where}: unknown {kind} {ref}"
        for where, kind, ref in refs
        if ref is not None and ref not in known[kind]
    ]
    drivers = {p.id for p in seed.people if p.can_drive}
    problems.extend(
        f"transport_defaults[{i}]: driver {t.driver} is not authorized to drive"
        for i, t in enumerate(seed.transport_defaults)
        if t.driver in known["person"] and t.driver not in drivers
    )
    return problems


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def inside_git_worktree(path: Path) -> bool:
    """Report whether a path is inside a git working tree.

    Args:
        path: File path.

    Returns:
        True when the file or any parent directory contains ``.git``.
    """
    resolved = path.resolve()
    return any((parent / ".git").exists() for parent in resolved.parents)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects a mapping key listed twice.

    Plain ``yaml.safe_load`` keeps the last duplicate silently, so a repeated
    ``can_drive`` or a second ``people:`` section would quietly override the
    first. The error carries only the position, never the key or value.
    """

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Hashable, object]:
        """Build a mapping, refusing duplicate keys.

        Args:
            node: The YAML mapping node.
            deep: Construct nested values immediately.

        Returns:
            The constructed mapping.

        Raises:
            yaml.constructor.ConstructorError: If a key appears twice.
        """
        seen: set[object] = set()
        pairs = cast("list[tuple[yaml.Node, yaml.Node]]", node.value)
        for key_node, _ in pairs:
            # Third-party stub gap: types-PyYAML leaves construct_object's
            # ``node`` parameter unannotated.
            key = cast("object", self.construct_object(key_node, deep=deep))  # pyright: ignore[reportUnknownMemberType]
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    problem="duplicate mapping key",
                    problem_mark=cast("yaml.Mark", key_node.start_mark),
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_seed(text: str) -> RegistrySeed:
    """Validate seed YAML text.

    Args:
        text: YAML document.

    Returns:
        The validated seed.

    Raises:
        SeedError: If the YAML is malformed or fails validation. The message
            names locations and reasons, never values.
    """
    # The SafeLoader subclass is driven directly (as ``yaml.load`` would) so
    # only safe constructors run, plus duplicate-key rejection.
    try:
        loader = _UniqueKeyLoader(text)
        try:
            data = cast("object", loader.get_single_data())
        finally:
            loader.dispose()
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        where = f" near line {line + 1}" if isinstance(line, int) else ""
        msg = f"registry seed is not valid YAML{where}"
        raise SeedError(msg) from None
    try:
        return RegistrySeed.model_validate(data)
    except PydanticValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in err['loc']) or 'seed'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        ]
        msg = "registry seed is invalid: " + "; ".join(problems)
        raise SeedError(msg, details={"problems": problems}) from None


def load_seed_file(path: Path, *, allow_repo_path: bool = False) -> RegistrySeed:
    """Read and validate a seed file.

    Args:
        path: Seed file path.
        allow_repo_path: Permit a file inside a git working tree (fictional
            examples and tests only).

    Returns:
        The validated seed.

    Raises:
        SeedError: If the path is inside a working tree without permission,
            the file is missing or too large, or validation fails.
    """
    # #CRITICAL: Security: a real seed holds names and addresses; refusing a
    # path inside a git working tree keeps it out of this public repository.
    if not allow_repo_path and inside_git_worktree(path):
        msg = (
            "refusing a registry seed inside a git working tree; keep the real "
            "seed outside the repository (or pass --allow-repo-path for examples)"
        )
        raise SeedError(msg)
    if not path.is_file():
        msg = f"registry seed not found: {path.name}"
        raise SeedError(msg)
    if path.stat().st_size > MAX_SEED_BYTES:
        msg = "registry seed is larger than 1 MB"
        raise SeedError(msg)
    return parse_seed(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Domain conversion
# ---------------------------------------------------------------------------


def _secret(value: SecretStr | None) -> SensitiveStr | None:
    """Wrap a seed secret as a SensitiveStr."""
    return None if value is None else SensitiveStr(value.get_secret_value())


def seed_vehicles(seed: RegistrySeed) -> list[Vehicle]:
    """Convert seed vehicles.

    Args:
        seed: The seed.

    Returns:
        Domain vehicles.
    """
    return [Vehicle(v.id, v.name, v.seats) for v in seed.vehicles]


def seed_locations(seed: RegistrySeed) -> list[Location]:
    """Convert seed locations.

    Args:
        seed: The seed.

    Returns:
        Domain locations with protected fields wrapped.
    """
    return [
        Location(
            location_id=x.id,
            name=x.name,
            aliases=x.aliases,
            verified=x.verified,
            ha_zone_entity_id=x.ha_zone,
            address=_secret(x.address),
            coordinates=_secret(x.coordinates),
        )
        for x in seed.locations
    ]


def seed_people(seed: RegistrySeed) -> list[Person]:
    """Convert seed people.

    Args:
        seed: The seed.

    Returns:
        Domain people.
    """
    return [
        Person(
            person_id=p.id,
            display_name=p.name,
            role=p.role,
            aliases=p.aliases,
            active=p.active,
            can_drive=p.can_drive,
            default_vehicle_id=p.vehicle,
            monitoring_enabled=p.monitoring.enabled,
            monitoring_start=p.monitoring.start,
            monitoring_end=p.monitoring.end,
            home_location_id=p.home,
        )
        for p in seed.people
    ]


def seed_passengers(seed: RegistrySeed) -> list[ExternalPassenger]:
    """Convert seed external passengers.

    Args:
        seed: The seed.

    Returns:
        Domain external passengers.
    """
    return [
        ExternalPassenger(
            passenger_id=e.id,
            display_first_name=e.first_name,
            household_id=e.household,
            contact_id=e.contact,
            default_pickup_location_id=e.pickup,
            default_dropoff_location_id=e.dropoff,
            alternate_destination_ids=e.alternates,
            aliases=e.aliases,
            active=e.active,
        )
        for e in seed.external_passengers
    ]


def seed_calendars(seed: RegistrySeed) -> list[CalendarSource]:
    """Convert seed calendars.

    Args:
        seed: The seed.

    Returns:
        Calendar sources (collection status empty).
    """
    return [
        CalendarSource(
            calendar_id=c.id,
            display_name=c.name,
            kind=c.kind,
            person_id=c.person,
            enabled=c.enabled,
        )
        for c in seed.calendars
    ]


def seed_transport_defaults(seed: RegistrySeed) -> dict[str, list[TransportDefault]]:
    """Convert seed transport defaults, grouped by traveller.

    Args:
        seed: The seed.

    Returns:
        Person ID to that person's defaults in file order.
    """
    grouped: dict[str, list[TransportDefault]] = {}
    for t in seed.transport_defaults:
        grouped.setdefault(t.person, []).append(
            TransportDefault(
                person_id=t.person,
                weekdays=t.weekdays,
                direction=t.direction,
                mode=t.mode,
                origin_location_id=t.origin,
                destination_location_id=t.destination,
                driver_id=t.driver,
                effective_from=t.effective_from,
                effective_to=t.effective_to,
            )
        )
    return grouped


# ---------------------------------------------------------------------------
# Comparable state (what the audit log hashes; never protected plaintext)
# ---------------------------------------------------------------------------

State = dict[str, object]


def _aliases_state(aliases: tuple[str, ...]) -> list[str]:
    """Return aliases in a canonical, case-insensitive order."""
    return sorted({" ".join(a.split()).casefold() for a in aliases if a.strip()})


def _vehicle_state(v: Vehicle) -> State:
    """Return a vehicle's comparable state."""
    return {"name": v.display_name, "seats": v.seat_capacity}


def _location_state(x: Location, store: RegistryStore) -> State:
    """Return a location's comparable state with protected-field digests."""
    return {
        "name": x.name,
        "aliases": _aliases_state(x.aliases),
        "verified": x.verified,
        "ha_zone": x.ha_zone_entity_id,
        "address": store.fingerprint(x.address),
        "coordinates": store.fingerprint(x.coordinates),
    }


def _household_state(h: Household) -> State:
    """Return a household's comparable state."""
    return {"label": h.label, "privacy_scope": h.privacy_scope}


def _person_state(p: Person) -> State:
    """Return a person's comparable state."""
    return {
        "name": p.display_name,
        "role": p.role.value,
        "aliases": _aliases_state(p.aliases),
        "active": p.active,
        "can_drive": p.can_drive,
        "vehicle": p.default_vehicle_id,
        "home": p.home_location_id,
        "monitoring": [p.monitoring_enabled, p.monitoring_start, p.monitoring_end],
    }


def _passenger_state(e: ExternalPassenger) -> State:
    """Return an external passenger's comparable state."""
    return {
        "first_name": e.display_first_name,
        "household": e.household_id,
        "contact": e.contact_id,
        "pickup": e.default_pickup_location_id,
        "dropoff": e.default_dropoff_location_id,
        "alternates": list(e.alternate_destination_ids),
        "aliases": _aliases_state(e.aliases),
        "active": e.active,
    }


def _calendar_state(c: CalendarSource) -> State:
    """Return a calendar's comparable configuration state."""
    return {
        "name": c.display_name,
        "kind": c.kind.value,
        "person": c.person_id,
        "enabled": c.enabled,
    }


def _defaults_state(items: list[TransportDefault]) -> list[State]:
    """Return a person's transport defaults in canonical order."""
    states: list[State] = [
        {
            "weekdays": sorted(t.weekdays),
            "direction": t.direction.value,
            "mode": t.mode.value,
            "origin": t.origin_location_id,
            "destination": t.destination_location_id,
            "driver": t.driver_id,
            "from": t.effective_from,
            "to": t.effective_to,
        }
        for t in items
    ]
    return sorted(states, key=repr)


# ---------------------------------------------------------------------------
# Plan and apply
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Change:
    """One object the seed creates, updates or deactivates.

    Attributes:
        object_ref: Internal reference, e.g. ``person:alex`` (no names).
        action: ``create``, ``update`` or ``deactivate``.
        prior: Comparable state before (hashed into the audit log only).
        new: Comparable state after.
    """

    object_ref: str
    action: ChangeAction
    prior: object | None
    new: object | None


@dataclass(frozen=True, slots=True)
class SeedPlan:
    """The difference between a seed and the database.

    Attributes:
        changes: Objects that change, in application order.
        unchanged: Number of seed objects already matching the database.
        retained: Locations, vehicles and households in the database that the
            seed no longer lists (kept, never deleted).
    """

    changes: tuple[Change, ...]
    unchanged: int
    retained: tuple[str, ...] = field(default_factory=tuple)

    def counts(self) -> dict[str, int]:
        """Summarize the plan.

        Returns:
            Counts of created, updated, deactivated, unchanged and retained.
        """
        result = {"create": 0, "update": 0, "deactivate": 0}
        for change in self.changes:
            result[change.action] += 1
        result["unchanged"] = self.unchanged
        result["retained"] = len(self.retained)
        return result


@dataclass
class _Planner:
    """Accumulates changes while comparing the seed with the database."""

    changes: list[Change] = field(default_factory=list[Change])
    unchanged: int = 0

    def compare(self, ref: str, prior: object | None, new: object) -> bool:
        """Record a create or update when states differ.

        Args:
            ref: Object reference.
            prior: Comparable state in the database, or None when absent.
            new: Comparable state the seed asks for.

        Returns:
            True when the object changes.
        """
        if prior == new:
            self.unchanged += 1
            return False
        self.changes.append(
            Change(ref, "create" if prior is None else "update", prior, new)
        )
        return True


def _require_cipher_for(
    store: RegistryStore, seed: RegistrySeed, cipher: AddressCipher | None
) -> None:
    """Refuse to touch protected data without a cipher."""
    seed_protected = any(x.address or x.coordinates for x in seed.locations)
    if cipher is None and (seed_protected or store.has_protected_data()):
        msg = (
            "FL_DATA_KEY is required: the seed or the registry holds protected "
            "addresses or coordinates"
        )
        raise ConfigurationError(msg)


def build_plan(store: RegistryStore, seed: RegistrySeed) -> SeedPlan:  # noqa: C901, PLR0912 - one pass per section
    """Compare a seed with the database.

    Args:
        store: Registry store on an open connection (with the cipher when the
            registry holds protected data).
        seed: The validated seed.

    Returns:
        The plan; nothing is written.
    """
    planner = _Planner()
    retained: list[str] = []

    existing_vehicles = {v.vehicle_id: v for v in store.vehicles()}
    for v in seed_vehicles(seed):
        prior = existing_vehicles.get(v.vehicle_id)
        planner.compare(
            f"vehicle:{v.vehicle_id}",
            None if prior is None else _vehicle_state(prior),
            _vehicle_state(v),
        )
    retained.extend(
        f"vehicle:{i}"
        for i in existing_vehicles
        if i not in {v.id for v in seed.vehicles}
    )

    existing_locations = {x.location_id: x for x in store.locations()}
    for x in seed_locations(seed):
        prior_loc = existing_locations.get(x.location_id)
        planner.compare(
            f"location:{x.location_id}",
            None if prior_loc is None else _location_state(prior_loc, store),
            _location_state(x, store),
        )
    retained.extend(
        f"location:{i}"
        for i in existing_locations
        if i not in {x.id for x in seed.locations}
    )

    existing_households = {h.household_id: h for h in store.households()}
    for h in seed.households:
        new_household = Household(h.id, h.label)
        prior_household = existing_households.get(h.id)
        planner.compare(
            f"household:{h.id}",
            None if prior_household is None else _household_state(prior_household),
            _household_state(new_household),
        )
    retained.extend(
        f"household:{i}"
        for i in existing_households
        if i not in {h.id for h in seed.households}
    )

    existing_people = {p.person_id: p for p in store.people()}
    for p in seed_people(seed):
        prior_person = existing_people.get(p.person_id)
        planner.compare(
            f"person:{p.person_id}",
            None if prior_person is None else _person_state(prior_person),
            _person_state(p),
        )
    seed_person_ids = {p.id for p in seed.people}
    for pid, p in existing_people.items():
        if pid not in seed_person_ids and p.active:
            planner.changes.append(
                Change(
                    f"person:{pid}", "deactivate", _person_state(p), {"active": False}
                )
            )

    existing_passengers = {e.passenger_id: e for e in store.external_passengers()}
    for e in seed_passengers(seed):
        prior_passenger = existing_passengers.get(e.passenger_id)
        planner.compare(
            f"passenger:{e.passenger_id}",
            None if prior_passenger is None else _passenger_state(prior_passenger),
            _passenger_state(e),
        )
    seed_passenger_ids = {e.id for e in seed.external_passengers}
    for eid, e in existing_passengers.items():
        if eid not in seed_passenger_ids and e.active:
            planner.changes.append(
                Change(
                    f"passenger:{eid}",
                    "deactivate",
                    _passenger_state(e),
                    {"active": False},
                )
            )

    existing_calendars = {c.calendar_id: c for c in store.calendar_sources()}
    for c in seed_calendars(seed):
        prior_calendar = existing_calendars.get(c.calendar_id)
        planner.compare(
            f"calendar:{c.calendar_id}",
            None if prior_calendar is None else _calendar_state(prior_calendar),
            _calendar_state(c),
        )
    seed_calendar_ids = {c.id for c in seed.calendars}
    for cid, c in existing_calendars.items():
        if cid not in seed_calendar_ids and c.enabled:
            planner.changes.append(
                Change(
                    f"calendar:{cid}",
                    "deactivate",
                    _calendar_state(c),
                    {"enabled": False},
                )
            )

    existing_defaults: dict[str, list[TransportDefault]] = {}
    for t in store.transport_defaults():
        existing_defaults.setdefault(t.person_id, []).append(t)
    wanted_defaults = seed_transport_defaults(seed)
    for pid in sorted(set(existing_defaults) | set(wanted_defaults)):
        prior_defaults = existing_defaults.get(pid)
        planner.compare(
            f"transport_defaults:{pid}",
            None if prior_defaults is None else _defaults_state(prior_defaults),
            _defaults_state(wanted_defaults.get(pid, [])),
        )

    return SeedPlan(tuple(planner.changes), planner.unchanged, tuple(sorted(retained)))


def _apply(
    store: RegistryStore, seed: RegistrySeed, plan: SeedPlan, today: date
) -> None:
    """Write a plan's changes, in foreign-key order, on the store's connection."""
    refs = {c.object_ref for c in plan.changes if c.action != "deactivate"}
    # Clear the aliases of every changing target first, so an alias moving
    # from one target to another never collides with its old owner on the
    # unique (kind, lower(alias)) index; each upsert then rewrites its own.
    for ref in sorted(refs):
        kind, ident = ref.split(":", 1)
        if kind in {"person", "passenger", "location"}:
            store.set_aliases(kind, ident, ())
    inactive_since = store.passenger_inactive_since()
    wanted = seed_transport_defaults(seed)
    # Order matters for foreign keys: places and vehicles, then households,
    # contacts before the passengers that reference them, then the rest.
    people = sorted(seed_people(seed), key=lambda p: p.role is not PersonRole.CONTACT)
    steps: list[tuple[str, Callable[[], None]]] = [
        *(
            (f"vehicle:{v.vehicle_id}", partial(store.upsert_vehicle, v))
            for v in seed_vehicles(seed)
        ),
        *(
            (f"location:{x.location_id}", partial(store.upsert_location, x))
            for x in seed_locations(seed)
        ),
        *(
            (
                f"household:{h.id}",
                partial(store.upsert_household, Household(h.id, h.label)),
            )
            for h in seed.households
        ),
        *((f"person:{p.person_id}", partial(store.upsert_person, p)) for p in people),
        *(
            (
                f"passenger:{e.passenger_id}",
                partial(
                    store.upsert_external_passenger,
                    e,
                    inactive_since=inactive_since.get(e.passenger_id, today),
                ),
            )
            for e in seed_passengers(seed)
        ),
        *(
            (f"calendar:{c.calendar_id}", partial(store.upsert_calendar_source, c))
            for c in seed_calendars(seed)
        ),
    ]
    for ref, step in steps:
        if ref in refs:
            step()
    for ref in sorted(r for r in refs if r.startswith("transport_defaults:")):
        pid = ref.split(":", 1)[1]
        store.replace_transport_defaults(pid, wanted.get(pid, []))
    _deactivate(store, plan, today)


def _deactivate(store: RegistryStore, plan: SeedPlan, today: date) -> None:
    """Apply the plan's deactivations (rows are never deleted)."""
    for change in plan.changes:
        if change.action != "deactivate":
            continue
        kind, ident = change.object_ref.split(":", 1)
        if kind == "person":
            store.set_person_active(ident, active=False)
        elif kind == "passenger":
            store.deactivate_external_passenger(ident, since=today)
        else:
            store.set_calendar_enabled(ident, enabled=False)


def apply_seed(  # noqa: PLR0913 - keyword-only options
    engine: Engine,
    seed: RegistrySeed,
    *,
    cipher: AddressCipher | None,
    audit_key: SecretStr | None,
    audit_key_version: int = 1,
    actor: str = "registry-seed",
    dry_run: bool = False,
    today: date | None = None,
    correlation_id: str | None = None,
) -> SeedPlan:
    """Apply a seed atomically and audit every changed object.

    Args:
        engine: Application engine (schema at head).
        seed: The validated seed.
        cipher: Address cipher from ``FL_DATA_KEY``.
        audit_key: ``Settings.audit_key``. A real (not dry) run that changes
            anything needs it; without it the first audit row raises
            ``ConfigurationError`` and the whole seed rolls back.
        audit_key_version: ``Settings.audit_key_version``, stored per row.
        actor: Audit actor.
        dry_run: Compute the plan without writing anything.
        today: Local day used for passenger ``inactive_since`` (defaults to
            today in UTC).
        correlation_id: Optional correlation ID for the audit rows.

    Protected data without a cipher raises ``ConfigurationError``; a
    conflicting write (for example a duplicate alias) raises ``RegistryError``
    and rolls the whole seed back.

    Returns:
        The plan that was (or would be) applied.
    """
    day = today or datetime.now(UTC).date()
    if dry_run:
        with engine.connect() as connection:
            store = RegistryStore(connection, cipher)
            _require_cipher_for(store, seed, cipher)
            return build_plan(store, seed)
    # #CRITICAL: Data integrity: the audit rows are written on the same
    # connection, so an audit failure rolls the registry changes back too.
    # #VERIFY: test_audit_failure_rolls_back_registry_changes.
    with engine.begin() as connection:
        store = RegistryStore(connection, cipher)
        _require_cipher_for(store, seed, cipher)
        plan = build_plan(store, seed)
        _apply(store, seed, plan, day)
        for change in plan.changes:
            record_on(
                connection,
                key=audit_key,
                key_version=audit_key_version,
                actor=actor,
                object_ref=change.object_ref,
                action=f"seed-{change.action}",
                prior_state=change.prior,
                new_state=change.new,
                correlation_id=correlation_id,
            )
    return plan
