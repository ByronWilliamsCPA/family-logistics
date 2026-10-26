"""Notification subjects: when a finding matters and to whom (ADR 0008 D7).

A ``Finding`` carries free text (message, suggestion, raw names in some entity
keys) that must never reach a push. This module derives the structured part a
push needs, the time, the people and the driver, from the finding's entity
keys, a ``RideIndex`` the run outcome carries and the day timelines. It reads
no message, no suggestion and no raw name, performs no I/O and runs outside
``assess``, so it can never fail an assessment run.

Entity-key formats are a contract with the rule modules (they also define
``Finding.issue_id``); ``tests/scenarios/test_notification_subjects_matrix.py``
resolves every finding of the acceptance scenarios so a grammar change fails CI
before it fails silently.

#CRITICAL: Security: children's data. A ``Subject`` holds registry IDs and
kinds only. ``unregistered:`` IDs (a name written in calendar notes that is not
in the registry) are dropped here, so a raw name can never travel from a
calendar note into a push.
#VERIFY: tests/unit/test_notification_subjects.py (unregistered IDs dropped,
kinds kept; the parser's raw-name keys are never read).
#ASSUME: Data integrity: entity-key formats stay as tabulated in ADR 0008 D7.
#VERIFY: tests/scenarios/test_notification_subjects_matrix.py (zero
unresolved findings over the acceptance scenarios). An unresolved finding is
treated as imminent by the policy and reported by rule ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from family_logistics.domain.models import PassengerKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime, timedelta

    from family_logistics.domain.models import Finding, SourceEventKey, Trip
    from family_logistics.domain.timeline import DayTimeline, Transition

UNREGISTERED_PREFIX = "unregistered:"
"""Prefix the parser gives a passenger name that is not in the registry."""

_PASSENGER_KEY = "passenger:"
_DRIVER_KEY = "driver:"
_PERSON_KEY = "person:"
_DAY_KEY = "day:"


@dataclass(frozen=True, slots=True)
class RideWindow:
    """One ride's window and people, as the notification policy needs them.

    Attributes:
        start: Window start (aware UTC).
        end: Window end (aware UTC).
        driver_id: Registered driver, or None (unparsed events, unknown driver).
        passengers: ``(id, kind)`` per registered passenger; ``unregistered:``
            IDs are dropped.
    """

    start: datetime
    end: datetime
    driver_id: str | None
    passengers: tuple[tuple[str, PassengerKind], ...]


def _registered(identifier: str | None) -> str | None:
    """Return the ID, or None when it is empty or an unregistered name."""
    if not identifier or identifier.startswith(UNREGISTERED_PREFIX):
        return None
    return identifier


def _window_of_trip(trip: Trip) -> RideWindow:
    return RideWindow(
        start=trip.start,
        end=trip.end,
        driver_id=_registered(trip.driver_id),
        passengers=tuple(
            (p.passenger_id, p.kind)
            for p in trip.passengers
            if _registered(p.passenger_id) is not None
        ),
    )


def _frozen(items: Mapping[str, RideWindow]) -> Mapping[str, RideWindow]:
    return MappingProxyType(dict(items))


@dataclass(frozen=True, slots=True)
class RideIndex:
    """Ride windows by ``SourceEventKey.as_str()``.

    Attributes:
        current: This run's parsed trips.
        unparsed: Transport events that did not parse (start and end only).
        previous: ``history.trips``, the last completed run's rides.
    """

    current: Mapping[str, RideWindow] = field(default_factory=lambda: _frozen({}))
    unparsed: Mapping[str, RideWindow] = field(default_factory=lambda: _frozen({}))
    previous: Mapping[str, RideWindow] = field(default_factory=lambda: _frozen({}))

    @classmethod
    def build(
        cls,
        *,
        current: Sequence[Trip],
        previous: Sequence[Trip],
        unparsed: Sequence[tuple[SourceEventKey, datetime, datetime]],
    ) -> RideIndex:
        """Index the rides of one run.

        Args:
            current: This run's parsed trips.
            previous: The last completed run's trips (``history.trips``).
            unparsed: Key, start and end of each transport event that did not
                parse.

        Returns:
            The index.
        """
        return cls(
            current=_frozen({t.key.as_str(): _window_of_trip(t) for t in current}),
            unparsed=_frozen(
                {
                    k.as_str(): RideWindow(start, end, None, ())
                    for k, start, end in unparsed
                }
            ),
            previous=_frozen({t.key.as_str(): _window_of_trip(t) for t in previous}),
        )

    def windows(self) -> tuple[RideWindow, ...]:
        """Return the windows run-wide imminence is judged over.

        Returns:
            Current and unparsed windows; the previous run's windows only when
            both are empty (a failed run that read its history).
        """
        known = (*self.current.values(), *self.unparsed.values())
        return known or tuple(self.previous.values())


@dataclass(frozen=True, slots=True)
class Subject:
    """When and for whom a finding matters.

    Attributes:
        due_at: Start of the ride or move; None for run-wide or day-level.
        due_end: End of the ride or move.
        day: Local day for a day-level finding (SCHOOL-009).
        passengers: ``(id, kind)`` of the people the finding concerns.
        driver_id: Registered driver the finding concerns, if any.
    """

    due_at: datetime | None = None
    due_end: datetime | None = None
    day: date | None = None
    passengers: tuple[tuple[str, PassengerKind], ...] = ()
    driver_id: str | None = None


class Resolution(StrEnum):
    """How far a finding's subject could be derived."""

    RESOLVED = "resolved"
    RUN_WIDE = "run_wide"
    DAY = "day"
    UNRESOLVED = "unresolved"


def transition_window(
    tr: Transition, pickup_tolerance: timedelta
) -> tuple[datetime, datetime]:
    """Return the window of a required move.

    Args:
        tr: The transition.
        pickup_tolerance: The engine's pickup tolerance (30 minutes by default).

    Returns:
        ``(due_at, due_end)``: ``due_at`` is the move's reference time and
        ``due_end`` the arrival deadline, else the departure plus the
        tolerance. ``due_end`` is never before ``due_at`` (a scheduling
        conflict has an arrival before the departure).
    """
    due_at = tr.reference_time
    # With no arrival time the move has a departure time (``reference_time``
    # refused a move with neither), so ``due_at`` is the departure.
    due_end = tr.arrive_by if tr.arrive_by is not None else due_at + pickup_tolerance
    return due_at, max(due_at, due_end)


def is_imminent(
    due_at: datetime, due_end: datetime, now: datetime, window: timedelta
) -> bool:
    """Report whether a ride or move is imminent or in progress.

    Args:
        due_at: Start of the ride or move.
        due_end: End of the ride or move.
        now: The dispatch clock (aware).
        window: ``FL_IMMINENT_WINDOW_MINUTES``.

    Returns:
        ``due_at - window <= now <= due_end``. A ride that has started stays
        imminent until it ends; a zero window means "only while in progress".
    """
    return due_at - window <= now <= due_end


def run_imminent(  # noqa: PLR0913 - the clock and thresholds are keyword-only
    rides: RideIndex | None,
    timelines: Sequence[DayTimeline],
    *,
    now: datetime,
    window: timedelta,
    pickup_tolerance: timedelta,
) -> bool:
    """Report whether anything the run covers is imminent.

    Args:
        rides: The run's ride index; None means the ride set is unknown.
        timelines: Day timelines whose moves also count (a stale school
            snapshot affects bus and school moves, not only rides).
        now: The dispatch clock (aware).
        window: ``FL_IMMINENT_WINDOW_MINUTES``.
        pickup_tolerance: The engine's pickup tolerance.

    Returns:
        True when the ride set is unknown or any ride or move is imminent.
    """
    if rides is None:
        return True
    if any(is_imminent(w.start, w.end, now, window) for w in rides.windows()):
        return True
    return any(
        is_imminent(*transition_window(tr, pickup_tolerance), now, window)
        for timeline in timelines
        for tr in timeline.transitions
    )


def _transition_keys(tr: Transition) -> tuple[str, ...]:
    """Return the four entity keys the coverage rules emit for a move."""
    return (
        f"{_PERSON_KEY}{tr.person_id}",
        f"{_DAY_KEY}{tr.day.isoformat()}",
        f"from:{tr.from_key}",
        f"to:{tr.to_key}",
    )


def _tagged(keys: Iterable[str], prefix: str) -> list[str]:
    """Return the values of keys that start with a prefix, in order."""
    return [k[len(prefix) :] for k in keys if k.startswith(prefix)]


def _first_tagged(keys: Iterable[str], prefix: str) -> str | None:
    found = _tagged(keys, prefix)
    return _registered(found[0]) if found else None


def _named_passengers(
    keys: Sequence[str], window: RideWindow
) -> tuple[tuple[str, PassengerKind], ...]:
    """Return the ``passenger:`` keys that are on the ride, with their kinds."""
    kinds = dict(window.passengers)
    seen: dict[str, PassengerKind] = {}
    for passenger_id in _tagged(keys, _PASSENGER_KEY):
        kind = kinds.get(passenger_id)
        if kind is not None:
            seen[passenger_id] = kind
    return tuple(seen.items())


def _ride_subject(rule_id: str, keys: Sequence[str], window: RideWindow) -> Subject:
    """Return the subject of a ride-level finding (who depends on the rule)."""
    extra = keys[1:]
    passengers: tuple[tuple[str, PassengerKind], ...] = ()
    driver: str | None = None
    if rule_id in {"PASS-003", "DEST-004"}:
        passengers = _named_passengers(extra, window)
        driver = window.driver_id if rule_id == "PASS-003" else None
    elif rule_id == "DRIVER-006":
        passengers = _named_passengers(extra, window)
        driver = _first_tagged(extra, _DRIVER_KEY)
    elif rule_id == "CAP-014":
        driver = window.driver_id
    elif rule_id == "ROUTE-010" and "reachability" in extra:
        driver = _first_tagged(extra, _DRIVER_KEY)
    elif rule_id in {"TIME-007", "ORDER-012", "ROUTE-010"}:
        passengers = window.passengers
        driver = window.driver_id
    return Subject(
        due_at=window.start,
        due_end=window.end,
        passengers=passengers,
        driver_id=driver,
    )


_LOOKUP_ORDER: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "PASS-003": ("previous", "current", "unparsed"),
        "CONF-008": ("current", "previous", "unparsed"),
    }
)
_DEFAULT_ORDER = ("current", "unparsed", "previous")


def _find_window(
    rule_id: str, trip_key: str, rides: RideIndex | None
) -> RideWindow | None:
    """Match a key exactly against the ride index, in the rule's order."""
    if rides is None:
        return None
    sources = {
        "current": rides.current,
        "unparsed": rides.unparsed,
        "previous": rides.previous,
    }
    for name in _LOOKUP_ORDER.get(rule_id, _DEFAULT_ORDER):
        window = sources[name].get(trip_key)
        if window is not None:
            return window
    return None


def _transition_subject(
    keys: Sequence[str], timelines: Sequence[DayTimeline], pickup_tolerance: timedelta
) -> Subject | None:
    """Find a move by its four keys and build its subject."""
    head = tuple(keys[:4])
    for timeline in timelines:
        for tr in timeline.transitions:
            if _transition_keys(tr) != head:
                continue
            due_at, due_end = transition_window(tr, pickup_tolerance)
            person = _registered(tr.person_id)
            return Subject(
                due_at=due_at,
                due_end=due_end,
                passengers=((person, PassengerKind.FAMILY),) if person else (),
                driver_id=_first_tagged(keys[4:], _DRIVER_KEY),
            )
    return None


def _school_subject(keys: Sequence[str]) -> Subject | None:
    """Build the day-level subject of SCHOOL-009 from its keys."""
    persons = _tagged(keys, _PERSON_KEY)
    days = _tagged(keys, _DAY_KEY)
    person = _registered(persons[0]) if persons else None
    if person is None or not days:
        return None
    try:
        day = date.fromisoformat(days[0])
    except ValueError:
        return None
    return Subject(day=day, passengers=((person, PassengerKind.FAMILY),))


def resolve_subject(
    finding: Finding,
    *,
    rides: RideIndex | None,
    timelines: Sequence[DayTimeline],
    pickup_tolerance: timedelta,
) -> tuple[Resolution, Subject]:
    """Derive a finding's subject without reading any free text.

    Args:
        finding: The finding; only ``rule_id`` and ``entity_keys`` are read.
        rides: The run's ride index, or None when unknown.
        timelines: Day timelines of the run (transition lookups).
        pickup_tolerance: The engine's pickup tolerance.

    Returns:
        ``RUN_WIDE`` for DATA-011, ``DAY`` for SCHOOL-009, ``RESOLVED`` with
        the window and people for everything else it can place, otherwise
        ``UNRESOLVED`` with an empty subject.
    """
    rule_id, keys = finding.rule_id, finding.entity_keys
    if rule_id == "DATA-011":
        return Resolution.RUN_WIDE, Subject()
    if not keys:
        return Resolution.UNRESOLVED, Subject()
    # An exact match against the ride index wins over any prefix reading, so a
    # calendar ID that starts with ``person:`` is still a trip key.
    window = _find_window(rule_id, keys[0], rides)
    if window is not None:
        return Resolution.RESOLVED, _ride_subject(rule_id, keys, window)
    if rule_id == "SCHOOL-009":
        school = _school_subject(keys)
        if school is not None:
            return Resolution.DAY, school
    elif keys[0].startswith(_PERSON_KEY):
        moved = _transition_subject(keys, timelines, pickup_tolerance)
        if moved is not None:
            return Resolution.RESOLVED, moved
    return Resolution.UNRESOLVED, Subject()


__all__ = [
    "UNREGISTERED_PREFIX",
    "Resolution",
    "RideIndex",
    "RideWindow",
    "Subject",
    "is_imminent",
    "resolve_subject",
    "run_imminent",
    "transition_window",
]
