"""In-memory registry lookups used by the parser.

Matching rules (handoff section 4.2):

- Family names match only exactly (case-insensitive, after Unicode
  normalization) against a display name or a registered alias. Fuzzy matching
  is used only to *suggest* a correction, never to substitute one.
- External passenger names may match several registered children (two
  children with the same first name). An explicit ``Name [passenger_id]`` form
  in the notes selects one; otherwise the lookup is ambiguous and the parser
  rejects it.
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass

from family_logistics.domain.models import (
    ExternalPassenger,
    Person,
    PersonRole,
    Vehicle,
)

SUGGESTION_CUTOFF = 0.75
"""Minimum difflib similarity for a spelling suggestion."""


def normalize_name(value: str) -> str:
    """Normalize a name for exact comparison.

    Args:
        value: Raw name text.

    Returns:
        NFC-normalized, whitespace-collapsed, case-folded name.
    """
    text = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    return " ".join(text.split()).casefold()


@dataclass(frozen=True, slots=True)
class Registry:
    """Registered people and external passengers.

    Attributes:
        people: Family members and external driver contacts.
        external_passengers: Children from other families.
        vehicles: Registered vehicles with seat capacity.
    """

    people: tuple[Person, ...]
    external_passengers: tuple[ExternalPassenger, ...] = ()
    vehicles: tuple[Vehicle, ...] = ()

    def _person_names(self, person: Person) -> set[str]:
        """Return a person's normalized display name and aliases."""
        return {normalize_name(person.display_name)} | {
            normalize_name(a) for a in person.aliases
        }

    def family_children(self) -> tuple[Person, ...]:
        """Return active family children.

        Returns:
            Children with role CHILD that are active.
        """
        return tuple(p for p in self.people if p.role is PersonRole.CHILD and p.active)

    def resolve_family_child(self, name: str) -> Person | None:
        """Resolve a family child by exact (normalized) name or alias.

        Args:
            name: Name as written in the notes.

        Returns:
            The child, or None when there is no exact match.
        """
        wanted = normalize_name(name)
        for person in self.family_children():
            if wanted in self._person_names(person):
                return person
        return None

    def resolve_driver(self, name: str) -> Person | None:
        """Resolve a name in a DRIVER field to a registered person.

        The caller must still check ``Person.can_drive``: resolving a name is
        not authorization to drive.

        Args:
            name: Name as written in the notes.

        Returns:
            The person, or None when there is no exact match.
        """
        wanted = normalize_name(name)
        for person in self.people:
            if person.active and wanted in self._person_names(person):
                return person
        return None

    def suggest_family_child(self, name: str) -> str | None:
        """Suggest the closest family child name for a misspelling.

        Args:
            name: Name as written in the notes.

        Returns:
            Canonical display name of the closest child, or None.
        """
        return _suggest(name, [p.display_name for p in self.family_children()])

    def suggest_driver(self, name: str) -> str | None:
        """Suggest the closest registered driver name.

        Args:
            name: Name as written in the notes.

        Returns:
            Canonical display name of the closest person, or None.
        """
        return _suggest(
            name, [p.display_name for p in self.people if p.active and p.can_drive]
        )

    def resolve_external(self, name: str) -> list[ExternalPassenger]:
        """Return every active external passenger matching a first name or alias.

        Args:
            name: Name as written in the notes.

        Returns:
            All matches; more than one means the name is ambiguous.
        """
        wanted = normalize_name(name)
        return [
            e
            for e in self.external_passengers
            if e.active
            and wanted
            in {normalize_name(e.display_first_name)}
            | {normalize_name(a) for a in e.aliases}
        ]

    def external_by_id(self, passenger_id: str) -> ExternalPassenger | None:
        """Look up an external passenger by stable ID.

        Args:
            passenger_id: Registry identifier.

        Returns:
            The passenger, or None.
        """
        for e in self.external_passengers:
            if e.passenger_id == passenger_id and e.active:
                return e
        return None


def vehicle_by_id(registry: Registry, vehicle_id: str | None) -> Vehicle | None:
    """Look up a vehicle.

    Args:
        registry: The registry.
        vehicle_id: Vehicle identifier, or None.

    Returns:
        The vehicle, or None.
    """
    return next((v for v in registry.vehicles if v.vehicle_id == vehicle_id), None)


def _suggest(name: str, candidates: list[str]) -> str | None:
    """Return the closest candidate above the suggestion cutoff, or None."""
    by_norm = {normalize_name(c): c for c in candidates}
    matches = difflib.get_close_matches(
        normalize_name(name), list(by_norm), n=1, cutoff=SUGGESTION_CUTOFF
    )
    return by_norm[matches[0]] if matches else None
