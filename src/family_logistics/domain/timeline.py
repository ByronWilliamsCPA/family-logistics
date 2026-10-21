"""Per-person location timeline (handoff 3.2; requirements 4.2).

For each monitored child the timeline merges school bell blocks, sports,
lessons and other activities into time-ordered *stays*: intervals spent at
one place. Rules:

- Adjacent activities at the same place, separated by at most the merge gap,
  form one stay; no ride is needed between them.
- A school day contributes its bell block; ``NO_SCHOOL`` contributes nothing,
  so the school baseline is suppressed while other activities stay. A school
  sport at the school extends the stay past the bell, so pickup is due at the
  end of the last activity there, never at the bell.
- A *transition* is a required move: home to the first stay of a day, between
  stays at different places, and from the last stay of a day back home. A
  child is never inferred home because an activity ended.
- A stay that continues at the same place the next day (an overnight) needs no
  return home and no morning trip there.

Times are UTC; "day" means the local calendar day in the assessment timezone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_logistics.domain.events import SchoolDayKind
from family_logistics.domain.locations import text_key
from family_logistics.domain.models import PersonRole

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date, datetime, timedelta, tzinfo

    from family_logistics.domain.events import Activity, AssessmentInput, SchoolDay
    from family_logistics.domain.locations import LocationResolver
    from family_logistics.domain.models import Person

SCHOOL_PREFIX = "school:"
"""Stay source IDs for bell blocks are ``school:<person>:<date>``."""


@dataclass(frozen=True, slots=True)
class Stay:
    """An interval one person spends at one place.

    Attributes:
        person_id: Who.
        key: Location key (see ``domain.locations``).
        location_id: Registry location, None when unregistered.
        label: Display label, never an address.
        start: Arrival (UTC).
        end: Departure (UTC); the end of the last merged activity.
        source_ids: Activity IDs and ``school:`` IDs merged into the stay.
        resolved: Whether the place is a registry location or a home.
    """

    person_id: str
    key: str
    location_id: str | None
    label: str
    start: datetime
    end: datetime
    source_ids: tuple[str, ...]
    resolved: bool


@dataclass(frozen=True, slots=True)
class Transition:
    """A move a person must make, which some transport has to cover.

    Attributes:
        person_id: Who moves.
        day: Local calendar day of the move.
        from_key: Location key of the origin.
        to_key: Location key of the destination.
        depart_after: When the origin stay ends; None when leaving home.
        arrive_by: When the next stay starts; None when returning home.
        from_resolved: Whether the origin is a known place.
        to_resolved: Whether the destination is a known place.
    """

    person_id: str
    day: date
    from_key: str
    to_key: str
    depart_after: datetime | None
    arrive_by: datetime | None
    from_resolved: bool = True
    to_resolved: bool = True

    @property
    def reference_time(self) -> datetime:
        """Return the moment the move happens.

        Returns:
            The departure time, or the arrival time when leaving home.

        Raises:
            ValueError: If the transition has neither time (never built so).
        """
        if self.depart_after is not None:
            return self.depart_after
        if self.arrive_by is None:  # pragma: no cover - a transition has an end
            msg = "Transition without a departure or arrival time"
            raise ValueError(msg)
        return self.arrive_by

    @property
    def is_conflict(self) -> bool:
        """Report whether the person is scheduled at two places at once.

        Returns:
            True when the next stay starts before the current one ends.
        """
        return (
            self.depart_after is not None
            and self.arrive_by is not None
            and self.arrive_by < self.depart_after
        )


@dataclass(frozen=True, slots=True)
class DayTimeline:
    """One person's stays and required moves for one local day.

    Attributes:
        person_id: Who.
        day: Local calendar day.
        stays: Stays starting that day, in time order.
        transitions: Required moves that day, in time order.
    """

    person_id: str
    day: date
    stays: tuple[Stay, ...]
    transitions: tuple[Transition, ...]


@dataclass(frozen=True, slots=True)
class _Block:
    """One activity or bell block before merging."""

    key: str
    location_id: str | None
    label: str
    start: datetime
    end: datetime
    source_id: str
    resolved: bool


def _activity_block(
    activity: Activity, person_id: str, resolver: LocationResolver
) -> _Block:
    """Turn one activity into a block from one participant's perspective."""
    location_id = activity.location_id or resolver.resolve_text(activity.location_text)
    if location_id is not None and resolver.location(location_id) is not None:
        key = resolver.key_for(location_id, person_id)
        return _Block(
            key,
            location_id,
            resolver.label(key),
            activity.start,
            activity.end,
            activity.activity_id,
            resolved=True,
        )
    return _Block(
        text_key(activity.location_text or activity.activity_id),
        None,
        resolver.label("unregistered"),
        activity.start,
        activity.end,
        activity.activity_id,
        resolved=False,
    )


def _school_block(day: SchoolDay, resolver: LocationResolver) -> _Block | None:
    """Turn a school day into a bell block, or None for no school."""
    if day.kind is SchoolDayKind.NO_SCHOOL or day.end <= day.start:
        return None
    key = resolver.key_for(day.location_id, day.person_id)
    known = resolver.location(day.location_id) is not None
    return _Block(
        key if known else text_key(day.location_id),
        day.location_id if known else None,
        resolver.label(key),
        day.start,
        day.end,
        f"{SCHOOL_PREFIX}{day.person_id}:{day.date.isoformat()}",
        resolved=known,
    )


def merge_blocks(
    person_id: str, blocks: Iterable[_Block], gap: timedelta
) -> tuple[Stay, ...]:
    """Merge time-ordered blocks at the same place into stays.

    Blocks at the same resolved place merge when the next one starts no later
    than ``gap`` after the current stay ends. Unregistered places merge only
    with an identical unregistered key.

    Args:
        person_id: Whose blocks.
        blocks: Activity and bell blocks.
        gap: Largest idle gap that still counts as one stay.

    Returns:
        Stays in start order.
    """
    stays: list[Stay] = []
    for block in sorted(blocks, key=lambda b: (b.start, b.end, b.source_id)):
        last = stays[-1] if stays else None
        if last is not None and last.key == block.key and block.start <= last.end + gap:
            stays[-1] = Stay(
                person_id,
                last.key,
                last.location_id,
                last.label,
                last.start,
                max(last.end, block.end),
                (*last.source_ids, block.source_id),
                last.resolved,
            )
            continue
        stays.append(
            Stay(
                person_id,
                block.key,
                block.location_id,
                block.label,
                block.start,
                block.end,
                (block.source_id,),
                block.resolved,
            )
        )
    return tuple(stays)


def _local_day(value: datetime, zone: tzinfo) -> date:
    """Return the local calendar day of a UTC time."""
    return value.astimezone(zone).date()


def transitions_for(
    stays: Sequence[Stay], home_key: str, zone: tzinfo
) -> tuple[Transition, ...]:
    """Derive the moves a person must make between stays and home.

    Args:
        stays: The person's stays in time order.
        home_key: Location key of the person's home.
        zone: Local timezone for day boundaries.

    Returns:
        Required moves in time order.
    """
    moves: list[Transition] = []
    for index, stay in enumerate(stays):
        day = _local_day(stay.start, zone)
        prev = stays[index - 1] if index else None
        nxt = stays[index + 1] if index + 1 < len(stays) else None
        first_today = prev is None or _local_day(prev.start, zone) != day
        if first_today:
            overnight = prev is not None and prev.key == stay.key
            if not overnight and stay.key != home_key:
                moves.append(
                    Transition(
                        stay.person_id,
                        day,
                        home_key,
                        stay.key,
                        None,
                        stay.start,
                        to_resolved=stay.resolved,
                    )
                )
        elif prev is not None and prev.key != stay.key:
            moves.append(
                Transition(
                    stay.person_id,
                    day,
                    prev.key,
                    stay.key,
                    prev.end,
                    stay.start,
                    from_resolved=prev.resolved,
                    to_resolved=stay.resolved,
                )
            )
        last_today = nxt is None or _local_day(nxt.start, zone) != day
        continues = nxt is not None and nxt.key == stay.key
        if last_today and not continues and stay.key != home_key:
            moves.append(
                Transition(
                    stay.person_id,
                    day,
                    stay.key,
                    home_key,
                    stay.end,
                    None,
                    from_resolved=stay.resolved,
                )
            )
    return tuple(sorted(moves, key=lambda t: t.reference_time))


def monitored_children(inp: AssessmentInput) -> tuple[Person, ...]:
    """Return the children whose transport is checked at all.

    Args:
        inp: Assessment input.

    Returns:
        Active children with monitoring enabled (dates are checked per day).
    """
    return tuple(
        p
        for p in inp.people
        if p.role is PersonRole.CHILD and p.active and p.monitoring_enabled
    )


def build_timelines(
    inp: AssessmentInput,
    resolver: LocationResolver,
    gap: timedelta,
    zone: tzinfo,
) -> tuple[DayTimeline, ...]:
    """Build day timelines for every monitored child.

    Days outside a person's monitoring window are dropped, so a child with
    monitoring disabled (for example away at university) has no timeline.

    Args:
        inp: Assessment input.
        resolver: Location resolver.
        gap: Same-location merge gap.
        zone: Local timezone.

    Returns:
        Timelines ordered by person then day.
    """
    timelines: list[DayTimeline] = []
    for person in monitored_children(inp):
        pid = person.person_id
        blocks = [
            _activity_block(a, pid, resolver)
            for a in inp.activities
            if pid in a.participant_ids and a.end > a.start
        ]
        blocks.extend(
            block
            for sd in inp.school_days
            if sd.person_id == pid
            and (block := _school_block(sd, resolver)) is not None
        )
        stays = merge_blocks(pid, blocks, gap)
        moves = transitions_for(stays, resolver.home_key(pid), zone)
        days = sorted({_local_day(s.start, zone) for s in stays})
        timelines.extend(
            DayTimeline(
                pid,
                day,
                tuple(s for s in stays if _local_day(s.start, zone) == day),
                tuple(t for t in moves if t.day == day),
            )
            for day in days
            if person.is_monitored_on(day)
        )
    return tuple(timelines)
