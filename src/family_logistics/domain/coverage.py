"""Transport coverage of required moves (RIDE-005, MODE-015, SCHOOL-009).

Every transition in a child's timeline needs transport:

1. A ride leg carrying that child covers it when the ride's window reaches the
   move (no later than the pickup tolerance after the stay ends, and before
   the next stay starts) and the ride's pickup and destination match the two
   places. Calendar rides take precedence over defaults (an exception wins).
2. Otherwise an effective-dated ``TransportDefault`` for that day and that
   origin/destination covers it, subject to its mode:
   - bike and walk: covered;
   - bus: covered only while the move happens at the bus time (a stay
     extended past the bell by a sport is not covered); on early-release or
     late-start days, or with no school schedule, MODE-015 asks to confirm;
   - parent or carpool: needs a named driver (else MODE-015) who is free at
     that time (else DRIVER-006).
3. Otherwise RIDE-005 (critical): the child has no way to make the move.

A move whose place is unregistered can only be matched by time; it is also
reported as ROUTE-010 (incomplete) so an unverifiable plan never looks green.

School baseline (SCHOOL-009): a day with an applicable school default but no
school schedule, or a schedule whose kind is unknown, is flagged as uncertain.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from family_logistics.domain.driver import commitments
from family_logistics.domain.events import SchoolDayKind, TransportMode
from family_logistics.domain.rules import make_finding
from family_logistics.domain.timeline import monitored_children

if TYPE_CHECKING:
    from datetime import date

    from family_logistics.domain.context import AssessmentContext
    from family_logistics.domain.events import SchoolDay, TransportDefault
    from family_logistics.domain.models import Finding, TripPassengerLeg
    from family_logistics.domain.timeline import DayTimeline, Transition

_INDEPENDENT = frozenset({TransportMode.BIKE, TransportMode.WALK})
_DRIVEN = frozenset({TransportMode.PARENT, TransportMode.CARPOOL})
_ADJUSTED_DAYS = frozenset({SchoolDayKind.EARLY_RELEASE, SchoolDayKind.LATE_START})


def _keys(tr: Transition) -> tuple[str, ...]:
    """Return the stable entity keys of a transition."""
    return (
        f"person:{tr.person_id}",
        f"day:{tr.day.isoformat()}",
        f"from:{tr.from_key}",
        f"to:{tr.to_key}",
    )


def _route(ctx: AssessmentContext, tr: Transition) -> str:
    """Describe a transition for messages without addresses."""
    return (
        f"from {ctx.resolver.label(tr.from_key)} to {ctx.resolver.label(tr.to_key)} "
        f"({ctx.when(tr.reference_time)})"
    )


def _time_matches(
    ctx: AssessmentContext, leg: TripPassengerLeg, tr: Transition
) -> bool:
    """Report whether a leg's window reaches a move at the right time."""
    if tr.depart_after is not None and not (
        tr.depart_after <= leg.end
        and leg.start <= tr.depart_after + ctx.settings.pickup_tolerance
    ):
        return False
    return tr.arrive_by is None or (
        leg.start <= tr.arrive_by
        and leg.end >= tr.arrive_by - ctx.settings.early_arrival_window
    )


def leg_covers(ctx: AssessmentContext, leg: TripPassengerLeg, tr: Transition) -> bool:
    """Report whether a ride leg covers a transition.

    Args:
        ctx: Run context.
        leg: A leg carrying the transition's person.
        tr: The required move.

    Returns:
        True when the leg's time window and places match the move.
    """
    if not _time_matches(ctx, leg, tr):
        return False
    dest = ctx.resolver.destination(
        leg.passenger_id, leg.passenger_kind, leg.destination
    )
    if dest.key is not None and tr.to_resolved and dest.key != tr.to_key:
        return False
    if tr.depart_after is None or not tr.from_resolved:
        return True
    pickup = ctx.resolver.pickup(leg.pickup, leg.passenger_id)
    return pickup.key is None or pickup.key == tr.from_key


def _default_matches(
    ctx: AssessmentContext, default: TransportDefault, tr: Transition
) -> bool:
    """Report whether a transport default describes this move."""
    origin = ctx.resolver.key_for(default.origin_location_id, tr.person_id)
    destination = ctx.resolver.key_for(default.destination_location_id, tr.person_id)
    return tr.from_key == origin and tr.to_key == destination


def _driver_busy(ctx: AssessmentContext, driver_id: str, tr: Transition) -> bool:
    """Report whether a default's driver is committed elsewhere at the move."""
    start = tr.reference_time
    end = start + ctx.settings.pickup_tolerance
    keys = {tr.from_key, tr.to_key}
    ids = {ctx.resolver.location_id_of(k) for k in keys} - {None}
    return any(
        c.start < end
        and start < c.end
        and c.start_key not in keys
        and c.start_location_id not in ids
        for c in commitments(ctx, driver_id)
    )


def _bus_findings(
    ctx: AssessmentContext,
    tr: Transition,
    school: SchoolDay | None,
) -> list[Finding]:
    """Judge a school bus default for one move."""
    name = ctx.display_name(tr.person_id)
    if school is None:
        return [
            make_finding(
                "MODE-015",
                f"{name}: no school schedule to check the bus {_route(ctx, tr)}.",
                (*_keys(tr), "bus"),
            )
        ]
    late = tr.depart_after is not None and tr.depart_after > (
        school.end + ctx.settings.pickup_tolerance
    )
    if late:
        return [
            make_finding(
                "RIDE-005",
                f"{name} stays past the bus {_route(ctx, tr)}; a pickup is needed.",
                _keys(tr),
                suggestion="Add a pickup ride for when the activity ends.",
            )
        ]
    if school.kind in _ADJUSTED_DAYS:
        return [
            make_finding(
                "MODE-015",
                f"{name}: confirm the bus runs at the adjusted time {_route(ctx, tr)}.",
                (*_keys(tr), "bus"),
            )
        ]
    return []


def _default_findings(
    ctx: AssessmentContext,
    default: TransportDefault,
    tr: Transition,
) -> list[Finding]:
    """Judge a matching transport default for one move."""
    if default.mode in _INDEPENDENT:
        return []
    if default.mode is TransportMode.BUS:
        return _bus_findings(ctx, tr, ctx.school_day(tr.person_id, tr.day))
    name = ctx.display_name(tr.person_id)
    if default.driver_id is None:
        return [
            make_finding(
                "MODE-015",
                f"{name}: the usual ride {_route(ctx, tr)} has no named driver.",
                (*_keys(tr), "default-driver"),
            )
        ]
    if _driver_busy(ctx, default.driver_id, tr):
        driver = ctx.display_name(default.driver_id)
        return [
            make_finding(
                "DRIVER-006",
                f"{name}: usual driver {driver} is busy {_route(ctx, tr)}.",
                (*_keys(tr), f"driver:{default.driver_id}"),
                suggestion="Add a ride with another driver for this day.",
            )
        ]
    return []


def transition_findings(ctx: AssessmentContext, tr: Transition) -> list[Finding]:
    """Evaluate one required move.

    Args:
        ctx: Run context.
        tr: The move.

    Returns:
        Findings for the move (empty when covered without caveats).
    """
    name = ctx.display_name(tr.person_id)
    if tr.is_conflict:
        return [
            make_finding(
                "RIDE-005",
                f"{name} is scheduled at two places at once {_route(ctx, tr)}.",
                _keys(tr),
                suggestion="Correct the overlapping calendar events.",
            )
        ]
    found: list[Finding] = []
    if not (tr.from_resolved and tr.to_resolved):
        found.append(
            make_finding(
                "ROUTE-010",
                f"{name}: unregistered place {_route(ctx, tr)}; matched by time only.",
                (*_keys(tr), "unresolved"),
                suggestion="Register the place in the registry.",
            )
        )
    if any(leg_covers(ctx, leg, tr) for leg in ctx.legs_for(tr.person_id)):
        return found
    school = ctx.school_day(tr.person_id, tr.day)
    if school is None or school.kind is not SchoolDayKind.NO_SCHOOL:
        for default in ctx.defaults_for(tr.person_id, tr.day):
            if _default_matches(ctx, default, tr):
                return [*found, *_default_findings(ctx, default, tr)]
    found.append(
        make_finding(
            "RIDE-005",
            f"{name} has no ride {_route(ctx, tr)}.",
            _keys(tr),
            suggestion=(
                "Add a ride to the transportation calendar or record the usual "
                "transport mode."
            ),
        )
    )
    return found


def _window_days(ctx: AssessmentContext) -> list[date]:
    """Return the local days from today to the end of the window."""
    first = max(ctx.local_day(ctx.inp.window_start), ctx.local_day(ctx.now))
    last = ctx.local_day(ctx.inp.window_end)
    days: list[date] = []
    day = first
    while day <= last:
        days.append(day)
        day += timedelta(days=1)
    return days


def school_baseline_findings(ctx: AssessmentContext) -> list[Finding]:
    """Flag days whose school baseline is missing or uncertain (SCHOOL-009).

    Args:
        ctx: Run context.

    Returns:
        SCHOOL-009 warnings.
    """
    found: list[Finding] = []
    for person in monitored_children(ctx.inp):
        pid = person.person_id
        for day in _window_days(ctx):
            if not person.is_monitored_on(day):
                continue
            school = ctx.school_day(pid, day)
            reason = ""
            if school is None and ctx.defaults_for(pid, day):
                reason = "no school schedule was supplied"
            elif school is not None and school.kind is SchoolDayKind.UNKNOWN:
                reason = "the school schedule is uncertain"
            if reason:
                found.append(
                    make_finding(
                        "SCHOOL-009",
                        f"{person.display_name} on {day.isoformat()}: {reason}.",
                        (f"person:{pid}", f"day:{day.isoformat()}", "school"),
                        suggestion="Check the school calendars in Home Assistant.",
                    )
                )
    return found


def coverage_findings(
    ctx: AssessmentContext, timelines: tuple[DayTimeline, ...]
) -> list[Finding]:
    """Evaluate every upcoming move of every monitored child.

    Args:
        ctx: Run context.
        timelines: Day timelines from ``build_timelines``.

    Returns:
        Coverage and school-baseline findings.
    """
    found: list[Finding] = []
    for timeline in timelines:
        for tr in timeline.transitions:
            if tr.reference_time >= ctx.now:
                found.extend(transition_findings(ctx, tr))
    found.extend(school_baseline_findings(ctx))
    return found
