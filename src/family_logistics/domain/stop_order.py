"""Constrained stop ordering for small multi-passenger trips (handoff section 10).

Policy:

- A fixed (manual) stop order always wins. If it misses a deadline the result
  is ``infeasible`` (ORDER-012); it is never silently reordered.
- With ``AUTO``, every permutation of up to ``MAX_STOPS`` stops is simulated;
  orders that miss any arrival deadline are rejected, and the fastest remaining
  order is chosen. A faster order that misses a deadline is never chosen.
- Any unknown travel estimate needed for the evaluation makes the result
  ``incomplete``: route uncertainty never yields a confident pass.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

MAX_STOPS = 6
"""720 permutations at most; larger trips are reported incomplete."""

DEFAULT_DWELL = timedelta(minutes=5)
"""Load/unload buffer per stop. #ASSUME: proposed default pending approval."""


class OrderStatus(StrEnum):
    """Outcome of stop ordering."""

    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class Stop:
    """A drop-off stop.

    Attributes:
        stop_id: Label used in STOP_ORDER (passenger or destination).
        location_id: Location to drive to.
        deadline: Latest acceptable arrival, None when unconstrained.
    """

    stop_id: str
    location_id: str
    deadline: datetime | None = None


class TravelEstimator(Protocol):
    """Callable returning a travel duration, or None when unknown."""

    def __call__(
        self, origin: str, destination: str, departure: datetime
    ) -> timedelta | None:
        """Estimate travel time.

        Args:
            origin: Origin location ID.
            destination: Destination location ID.
            departure: Departure time.

        Returns:
            Travel duration, or None when unknown.
        """
        ...


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Chosen order and its evaluation.

    Attributes:
        status: Feasible, infeasible or incomplete.
        order: Chosen stop IDs (empty when incomplete).
        total: Total driving time of the chosen order.
        arrivals: Arrival time per stop ID for the chosen order.
        missed: Stop IDs whose deadline the evaluated order misses.
        reason: Short explanation for non-feasible results.
    """

    status: OrderStatus
    order: tuple[str, ...] = ()
    total: timedelta | None = None
    arrivals: tuple[tuple[str, datetime], ...] = ()
    missed: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _Plan:
    """Fixed inputs shared by every candidate order."""

    origin: str
    departure: datetime
    estimator: TravelEstimator
    dwell: timedelta
    end: str | None


@dataclass(frozen=True, slots=True)
class _Simulation:
    """Totals, arrivals and missed deadlines for one candidate order."""

    total: timedelta
    arrivals: tuple[tuple[str, datetime], ...]
    missed: tuple[str, ...]


def _simulate(plan: _Plan, order: Sequence[Stop]) -> _Simulation | None:
    """Drive one candidate order; return None if any estimate is unknown."""
    estimator = plan.estimator
    clock = plan.departure
    here = plan.origin
    total = timedelta()
    arrivals: list[tuple[str, datetime]] = []
    missed: list[str] = []
    for stop in order:
        leg = estimator(here, stop.location_id, clock)
        if leg is None:
            return None
        clock += leg
        total += leg
        arrivals.append((stop.stop_id, clock))
        if stop.deadline is not None and clock > stop.deadline:
            missed.append(stop.stop_id)
        clock += plan.dwell
        here = stop.location_id
    if plan.end is not None:
        leg = estimator(here, plan.end, clock)
        if leg is None:
            return None
        total += leg
    return _Simulation(total, tuple(arrivals), tuple(missed))


def choose_stop_order(  # noqa: PLR0913 - every argument is a distinct constraint
    origin: str,
    stops: Sequence[Stop],
    departure: datetime,
    estimator: TravelEstimator,
    *,
    fixed_order: Sequence[str] | None = None,
    end: str | None = None,
    dwell: timedelta = DEFAULT_DWELL,
) -> OrderResult:
    """Choose the fastest stop order that meets every deadline.

    Args:
        origin: Pickup location ID.
        stops: Drop-off stops.
        departure: Departure time from the origin.
        estimator: Travel-time estimator; None means unknown.
        fixed_order: Manual STOP_ORDER labels; overrides optimization.
        end: Optional final location (e.g. driver returns home).
        dwell: Load/unload buffer per stop.

    Returns:
        The evaluation result.
    """
    by_id = {s.stop_id: s for s in stops}
    if fixed_order is not None:
        if sorted(fixed_order) != sorted(by_id):
            return OrderResult(
                OrderStatus.INCOMPLETE, reason="STOP_ORDER does not cover every stop."
            )
        candidates: list[tuple[Stop, ...]] = [tuple(by_id[s] for s in fixed_order)]
    elif len(stops) > MAX_STOPS:
        return OrderResult(
            OrderStatus.INCOMPLETE, reason=f"More than {MAX_STOPS} stops to order."
        )
    else:
        candidates = list(itertools.permutations(stops))

    plan = _Plan(origin, departure, estimator, dwell, end)
    best: tuple[tuple[Stop, ...], _Simulation] | None = None
    first_rejected: tuple[tuple[Stop, ...], _Simulation] | None = None
    for candidate in candidates:
        sim = _simulate(plan, candidate)
        if sim is None:
            return OrderResult(
                OrderStatus.INCOMPLETE, reason="A travel estimate is unavailable."
            )
        if sim.missed:
            first_rejected = first_rejected or (candidate, sim)
            continue
        if best is None or sim.total < best[1].total:
            best = (candidate, sim)

    if best is not None:
        order, sim = best
        return OrderResult(
            OrderStatus.FEASIBLE,
            tuple(s.stop_id for s in order),
            sim.total,
            sim.arrivals,
        )
    if first_rejected is None:  # pragma: no cover - permutations() never yields none
        return OrderResult(OrderStatus.INCOMPLETE, reason="No candidate orders.")
    order, sim = first_rejected
    return OrderResult(
        OrderStatus.INFEASIBLE,
        tuple(s.stop_id for s in order),
        sim.total,
        sim.arrivals,
        sim.missed,
        reason="No stop order meets every arrival deadline.",
    )
