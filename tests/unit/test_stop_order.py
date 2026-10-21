"""Constrained stop ordering and the static route provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from family_logistics.adapters.routing.base import RouteConstraints, StaticRouteProvider
from family_logistics.domain.stop_order import (
    MAX_STOPS,
    OrderStatus,
    Stop,
    choose_stop_order,
)

T0 = datetime(2026, 10, 23, 22, 30, tzinfo=UTC)
M = timedelta(minutes=1)

# A is close to the pickup, B is far; B has the tight deadline.
MATRIX = {
    ("P", "A"): 5 * M,
    ("P", "B"): 20 * M,
    ("A", "B"): 18 * M,
    ("B", "A"): 18 * M,
    ("A", "H"): 5 * M,
    ("B", "H"): 20 * M,
}
PROVIDER = StaticRouteProvider(MATRIX)


def optimize(stops, *, fixed=None, end=None):
    return PROVIDER.optimize(
        "P",
        stops,
        end,
        RouteConstraints(departure_time=T0, fixed_order=fixed, dwell=0 * M),
    )


@pytest.mark.unit
class TestStopOrder:
    def test_fastest_valid_order_without_deadlines(self):
        result = optimize([Stop("b", "B"), Stop("a", "A")])
        assert result.status is OrderStatus.FEASIBLE
        assert result.order == ("a", "b")
        assert result.total == 23 * M

    def test_deadline_beats_shorter_route(self):
        stops = [Stop("a", "A"), Stop("b", "B", deadline=T0 + 21 * M)]
        result = optimize(stops)
        assert result.status is OrderStatus.FEASIBLE
        assert result.order == ("b", "a")
        assert result.total == 38 * M

    def test_fixed_order_wins_even_when_slower(self):
        result = optimize([Stop("a", "A"), Stop("b", "B")], fixed=("b", "a"))
        assert result.order == ("b", "a")

    def test_fixed_order_missing_deadline_is_infeasible(self):
        stops = [Stop("a", "A"), Stop("b", "B", deadline=T0 + 21 * M)]
        result = optimize(stops, fixed=("a", "b"))
        assert result.status is OrderStatus.INFEASIBLE
        assert result.missed == ("b",)

    def test_no_order_meets_deadlines(self):
        stops = [
            Stop("a", "A", deadline=T0 + 1 * M),
            Stop("b", "B", deadline=T0 + 1 * M),
        ]
        assert optimize(stops).status is OrderStatus.INFEASIBLE

    def test_unknown_estimate_is_incomplete(self):
        result = optimize([Stop("a", "A"), Stop("z", "Z")])
        assert result.status is OrderStatus.INCOMPLETE

    def test_unknown_return_leg_is_incomplete(self):
        assert (
            optimize([Stop("a", "A")], end="Nowhere").status is OrderStatus.INCOMPLETE
        )

    def test_end_leg_counts_in_total(self):
        assert optimize([Stop("a", "A")], end="H").total == 10 * M

    def test_fixed_order_must_cover_every_stop(self):
        result = optimize([Stop("a", "A"), Stop("b", "B")], fixed=("a",))
        assert result.status is OrderStatus.INCOMPLETE

    def test_too_many_stops_is_incomplete(self):
        stops = [Stop(str(i), "A") for i in range(MAX_STOPS + 1)]
        assert optimize(stops).status is OrderStatus.INCOMPLETE

    def test_dwell_delays_later_arrivals(self):
        result = choose_stop_order(
            "P",
            [Stop("a", "A"), Stop("b", "B", deadline=T0 + 23 * M)],
            T0,
            lambda o, d, t: MATRIX.get((o, d)),
            fixed_order=("a", "b"),
            dwell=5 * M,
        )
        assert result.status is OrderStatus.INFEASIBLE


@pytest.mark.unit
def test_static_provider_only_models_driving():
    assert PROVIDER.estimate("P", "A", T0, "walking") is None
    estimate = PROVIDER.estimate("P", "A", T0, "driving")
    assert estimate is not None
    assert estimate.duration == 5 * M
    assert estimate.source == "static"
