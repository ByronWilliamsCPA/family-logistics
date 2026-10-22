"""Location resolution for the engine (pure, no I/O).

The engine compares places by *location key*, a string that is safe to store
in finding entity keys and never contains an address:

- a registry ``location_id`` for a resolved place;
- ``home:<person_id>`` for a person's home, so a ride to ``HOME`` and a stay at
  the registered home location compare equal even when the home is not yet
  registered;
- ``text:<hash>`` for a place written in a calendar but not in the registry.
  The hash keeps free text (which may be an address) out of stored keys.

Names resolve exactly (after Unicode and whitespace normalization) against a
location's name, aliases or ID. A name shared by two locations is ambiguous
and never resolves, so the engine reports it instead of guessing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_logistics.domain.models import DestinationKind, PassengerKind
from family_logistics.domain.registry import normalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_logistics.domain.models import (
        Destination,
        ExternalPassenger,
        Location,
        Person,
    )

HOME_PREFIX = "home:"
TEXT_PREFIX = "text:"
UNREGISTERED_LABEL = "an unregistered location"
HOME_WORDS = frozenset({"home", "house"})
"""Free-text pickup values that mean the passenger's own home."""


def text_key(text: str | None) -> str:
    """Return a stable key for unregistered free text without storing it.

    Args:
        text: Location text as written, possibly an address.

    Returns:
        ``text:`` plus 12 hex characters of SHA-256 over the normalized text.
    """
    material = normalize_name(text or "")
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"{TEXT_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class ResolvedPlace:
    """Outcome of resolving one destination or pickup.

    Attributes:
        key: Location key, or None when the place cannot be determined.
        location_id: Registry location, None for unregistered or unknown.
        verified: Whether a human verified the registry location.
        reason: Why the place is unresolved or unverified; empty when fine.
    """

    key: str | None
    location_id: str | None
    verified: bool
    reason: str = ""


def _unresolved(reason: str, key: str | None = None) -> ResolvedPlace:
    """Return a place that is not resolved to a verified registry location."""
    return ResolvedPlace(key, None, verified=False, reason=reason)


class LocationResolver:
    """Resolves names, homes and trip destinations to location keys.

    Args:
        locations: Registered locations.
        people: Registered people (for home locations).
        external_passengers: Registered external passengers (for defaults).
    """

    def __init__(
        self,
        locations: Iterable[Location],
        people: Iterable[Person],
        external_passengers: Iterable[ExternalPassenger],
    ) -> None:
        self._locations = {loc.location_id: loc for loc in locations}
        self._people = {p.person_id: p for p in people}
        self._externals = {e.passenger_id: e for e in external_passengers}
        index: dict[str, set[str]] = {}
        for loc in self._locations.values():
            for name in (loc.location_id, loc.name, *loc.aliases):
                index.setdefault(normalize_name(name), set()).add(loc.location_id)
        self._index = {
            name: next(iter(ids)) for name, ids in index.items() if len(ids) == 1
        }
        self._ambiguous = frozenset(n for n, ids in index.items() if len(ids) > 1)

    def location(self, location_id: str | None) -> Location | None:
        """Return a registered location by ID.

        Args:
            location_id: Location ID, or None.

        Returns:
            The location, or None when unknown.
        """
        return self._locations.get(location_id) if location_id else None

    def resolve_text(self, text: str | None) -> str | None:
        """Resolve a written place name to a registry location ID.

        ``Name @ explicit address`` resolves by the name part only; the
        address part is never matched or stored.

        Args:
            text: Place as written.

        Returns:
            The location ID, or None when unknown or ambiguous.
        """
        if not text:
            return None
        name = normalize_name(text.split("@", 1)[0])
        if name in self._ambiguous:
            return None
        return self._index.get(name)

    @staticmethod
    def home_key(person_id: str) -> str:
        """Return the home location key of a person.

        Args:
            person_id: Person or passenger identifier.

        Returns:
            ``home:<person_id>``.
        """
        return f"{HOME_PREFIX}{person_id}"

    def key_for(self, location_id: str, person_id: str | None) -> str:
        """Return the comparison key of a registry location for a person.

        Args:
            location_id: Registry location ID.
            person_id: Whose perspective; their home maps to the home key.

        Returns:
            The home key when the location is the person's home, else the ID.
        """
        person = self._people.get(person_id) if person_id else None
        if person is not None and person.home_location_id == location_id:
            return self.home_key(person.person_id)
        return location_id

    def location_id_of(self, key: str) -> str | None:
        """Return the registry location a location key refers to.

        Keys from different perspectives (``home:<person>`` for one person,
        the bare location ID for another) name the same place when they map
        to the same registry location.

        Args:
            key: Location key.

        Returns:
            The person's registered home for a home key, the location ID for a
            registry key, or None for an unregistered place or home.
        """
        if key.startswith(HOME_PREFIX):
            person = self._people.get(key.removeprefix(HOME_PREFIX))
            return person.home_location_id if person is not None else None
        return key if key in self._locations else None

    def label(self, key: str) -> str:
        """Return a display label for a location key (never an address).

        Args:
            key: Location key.

        Returns:
            The registry name, ``home`` or a generic unregistered label.
        """
        if key.startswith(HOME_PREFIX):
            return "home"
        loc = self._locations.get(key)
        return loc.name if loc is not None else UNREGISTERED_LABEL

    def pickup(self, text: str, person_id: str | None) -> ResolvedPlace:
        """Resolve a trip's pickup text from one passenger's perspective.

        Args:
            text: PICKUP value as written.
            person_id: Passenger whose ``home`` a home pickup refers to.

        Returns:
            The resolved place.
        """
        if person_id and normalize_name(text) in HOME_WORDS:
            return self._home_place(person_id)
        location_id = self.resolve_text(text)
        if location_id is None:
            return _unresolved("pickup is not a registered place")
        return self._registry_place(location_id, person_id)

    def destination(
        self, passenger_id: str, kind: PassengerKind, destination: Destination
    ) -> ResolvedPlace:
        """Resolve one passenger's destination.

        ``HOME`` means the passenger's home. ``DEFAULT`` means the saved
        default drop-off of an external passenger (a family child's default
        is home). A named place must match the registry.

        Args:
            passenger_id: Passenger identifier.
            kind: Family or external passenger.
            destination: The passenger's destination row.

        Returns:
            The resolved place; ``key`` None when it cannot be determined.
        """
        pid = passenger_id
        named = destination.kind is DestinationKind.NAMED
        if kind is PassengerKind.EXTERNAL and not named:
            external = self._externals.get(pid)
            target = external.default_dropoff_location_id if external else None
            if target is None:
                return _unresolved("no saved default destination")
            return self._registry_place(target, None)
        if not named:
            return self._home_place(pid)
        location_id = self.resolve_text(destination.raw)
        if location_id is None:
            return _unresolved("destination is not a registered place")
        return self._registry_place(location_id, pid)

    def _home_place(self, person_id: str) -> ResolvedPlace:
        """Resolve a person's home, keyed even when not yet registered."""
        person = self._people.get(person_id)
        home_id = person.home_location_id if person else None
        if home_id is None:
            return _unresolved("home location not registered", self.home_key(person_id))
        loc = self._locations.get(home_id)
        verified = loc is not None and loc.verified
        reason = "" if verified else "home location not verified"
        return ResolvedPlace(self.home_key(person_id), home_id, verified, reason)

    def _registry_place(self, location_id: str, person_id: str | None) -> ResolvedPlace:
        """Wrap a registry location with its verification state."""
        loc = self._locations.get(location_id)
        if loc is None:
            return _unresolved("location is not registered")
        reason = "" if loc.verified else "location not verified"
        return ResolvedPlace(
            self.key_for(location_id, person_id), location_id, loc.verified, reason
        )
