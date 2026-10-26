"""The ha_relay route provider: cache reads, queued misses and fail-closed behaviour."""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta

import pytest
from fl_engine import NOW as ENGINE_NOW
from fl_engine import activity, assessment, at, routes
from fl_factories import notes, trip
from fl_relay import HOME_ADDRESS, NOW, STUDIO_ADDRESS, ZONE, local, make_engine
from sqlalchemy import text

from family_logistics.adapters.routing import HaRelayRouteProvider
from family_logistics.adapters.routing.base import RouteConstraints
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import route_relay
from family_logistics.db.route_relay import (
    CACHE_VALIDITY,
    Leg,
    ResultStatus,
    RouteAnswer,
    RouteRelayRepository,
)
from family_logistics.domain.confirmation import confirmation_code
from family_logistics.domain.engine import assess
from family_logistics.domain.models import RunStatus
from family_logistics.domain.stop_order import OrderStatus, Stop

pytestmark = pytest.mark.unit

FRIDAY = 4
DEPARTURE = local(8, 5)  # Friday 08:05 local, bucket 32
LEG = Leg("home", "high-school", FRIDAY, 32)


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


@pytest.fixture
def repo(engine):
    return RouteRelayRepository(engine)


@pytest.fixture
def clock():
    return Clock(NOW)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def provider(repo, clock):
    return HaRelayRouteProvider(repo, ZONE, clock=clock)


def answer_all(repo, now, seconds=600):
    pending = repo.pending(now=now)
    outcome = repo.record_results(
        [RouteAnswer(r.request_id, seconds) for r in pending], now=now
    )
    assert all(i.status is ResultStatus.ACCEPTED for i in outcome.items)
    return len(pending)


class TestEstimate:
    def test_the_provider_is_named_ha_relay(self, provider):
        assert provider.name == "ha_relay"

    def test_a_miss_is_unknown_and_queues_exactly_one_request(self, provider, repo):
        assert provider.estimate("home", "high-school", DEPARTURE, "driving") is None
        assert [r.leg for r in repo.pending(now=NOW)] == [LEG]
        # A second miss, even from a different minute of the same bucket, does
        # not queue a duplicate.
        assert provider.estimate("home", "high-school", local(8, 12), "driving") is None
        assert [r.leg for r in repo.pending(now=NOW)] == [LEG]

    def test_a_hit_returns_the_cached_seconds_and_queues_nothing(self, provider, repo):
        provider.estimate("home", "high-school", DEPARTURE, "driving")
        answer_all(repo, NOW, seconds=1260)
        found = provider.estimate("home", "high-school", DEPARTURE, "driving")
        assert found is not None
        assert found.duration == timedelta(seconds=1260)
        assert found.source == "ha_relay"
        assert found.origin == "home"
        assert found.destination == "high-school"
        assert found.departure == DEPARTURE
        assert found.fetched_at == NOW
        assert repo.pending(now=NOW) == []

    def test_an_observation_older_than_28_days_is_not_served(
        self, provider, repo, clock
    ):
        provider.estimate("home", "high-school", DEPARTURE, "driving")
        answer_all(repo, NOW)
        clock.now = NOW + CACHE_VALIDITY + timedelta(seconds=1)
        assert provider.estimate("home", "high-school", DEPARTURE, "driving") is None
        # The stale leg is requested again.
        assert [r.leg for r in repo.pending(now=clock.now)] == [LEG]

    @pytest.mark.parametrize(
        ("departure", "expected_leg"),
        [
            (local(8, 20), Leg("home", "high-school", FRIDAY, 33)),
            (local(8, 5, day=24), Leg("home", "high-school", 5, 32)),
            (local(8, 5, day=30), Leg("home", "high-school", 4, 32)),
        ],
        ids=["next-bucket", "next-weekday", "same-weekday-a-week-later"],
    )
    def test_another_bucket_or_weekday_is_not_borrowed(
        self, provider, repo, departure, expected_leg
    ):
        provider.estimate("home", "high-school", DEPARTURE, "driving")
        answer_all(repo, NOW)
        found = provider.estimate("home", "high-school", departure, "driving")
        if expected_leg == LEG:
            # The same weekday and bucket a week later is the same slot: served.
            assert found is not None
        else:
            assert found is None
            assert expected_leg in {r.leg for r in repo.pending(now=NOW)}

    def test_only_driving_is_modelled(self, provider, repo):
        assert provider.estimate("home", "high-school", DEPARTURE, "walking") is None
        assert repo.pending(now=NOW) == []

    def test_a_leg_with_the_same_endpoints_is_unknown_and_not_queued(
        self, provider, repo
    ):
        assert provider.estimate("home", "home", DEPARTURE, "driving") is None
        assert repo.pending(now=NOW) == []

    def test_a_naive_departure_is_unknown_and_logged(self, provider, repo, caplog):
        naive = DEPARTURE.replace(tzinfo=None)
        with caplog.at_level(logging.WARNING):
            assert provider.estimate("home", "high-school", naive, "driving") is None
        assert repo.pending(now=NOW) == []
        assert "route_relay.estimate_unavailable" in caplog.text

    def test_the_provider_uses_the_configured_time_zone(self, repo, clock):
        from zoneinfo import ZoneInfo

        auckland = HaRelayRouteProvider(repo, ZoneInfo("Pacific/Auckland"), clock=clock)
        # Friday 08:05 in Los Angeles is Saturday 04:05 in Auckland (UTC+13).
        auckland.estimate("home", "high-school", DEPARTURE, "driving")
        assert [r.leg for r in repo.pending(now=NOW)] == [
            Leg("home", "high-school", 5, 16)
        ]


class TestFailClosed:
    def test_a_hit_for_a_location_that_was_edited_is_gone(self, provider, repo, engine):
        provider.estimate("home", "high-school", DEPARTURE, "driving")
        answer_all(repo, NOW)
        assert provider.estimate("home", "high-school", DEPARTURE, "driving")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE location SET ha_zone_entity_id = 'zone.school_b'"
                    " WHERE location_id = 'high-school'"
                )
            )
        assert provider.estimate("home", "high-school", DEPARTURE, "driving") is None

    def test_a_busy_store_never_raises_into_the_engine(
        self, engine, tmp_path, clock, caplog
    ):
        repo = RouteRelayRepository(engine, lock_timeout_ms=50)
        provider = HaRelayRouteProvider(repo, ZONE, clock=clock)
        holder = create_sqlite_engine(tmp_path / "fl.db")
        try:
            with holder.connect() as connection, caplog.at_level(logging.WARNING):
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                found = provider.estimate("home", "high-school", DEPARTURE, "driving")
                connection.rollback()
        finally:
            holder.dispose()
        assert found is None
        assert "route_relay.estimate_unavailable" in caplog.text
        assert repo.pending(now=NOW) == []

    def test_a_broken_store_never_raises_and_logs_no_addresses(
        self, engine, clock, caplog
    ):
        provider = HaRelayRouteProvider(RouteRelayRepository(engine), ZONE, clock=clock)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE route_cache"))
        with caplog.at_level(logging.WARNING):
            assert provider.estimate("home", "studio", DEPARTURE, "driving") is None
        assert "route_relay.estimate_unavailable" in caplog.text
        assert HOME_ADDRESS not in caplog.text
        assert STUDIO_ADDRESS not in caplog.text

    def test_a_failing_enqueue_never_raises(self, engine, clock, caplog):
        provider = HaRelayRouteProvider(RouteRelayRepository(engine), ZONE, clock=clock)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE route_request"))
        with caplog.at_level(logging.WARNING):
            assert (
                provider.estimate("home", "high-school", DEPARTURE, "driving") is None
            )
        assert "route_relay.estimate_unavailable" in caplog.text

    def test_an_unknown_location_is_unknown_not_an_error(self, provider, repo, caplog):
        with caplog.at_level(logging.WARNING):
            assert provider.estimate("nowhere", "home", DEPARTURE, "driving") is None
        assert repo.pending(now=NOW) == []
        assert "route_relay.estimate_unavailable" in caplog.text

    def test_a_full_queue_is_unknown_and_logged(
        self, provider, repo, monkeypatch, caplog
    ):
        monkeypatch.setattr(route_relay, "MAX_PENDING_REQUESTS", 0)
        with caplog.at_level(logging.WARNING):
            assert (
                provider.estimate("home", "high-school", DEPARTURE, "driving") is None
            )
        assert repo.pending(now=NOW) == []
        assert "route_relay.queue_full" in caplog.text


class TestOptimize:
    STOPS = (Stop("a", "high-school"), Stop("b", "xc"))

    def test_a_free_order_is_incomplete_on_a_cold_cache_and_queues_the_legs(
        self, provider, repo
    ):
        constraints = RouteConstraints(departure_time=DEPARTURE)
        result = provider.optimize("home", self.STOPS, None, constraints)
        assert result.status is OrderStatus.INCOMPLETE
        assert result.order == ()
        assert repo.pending(now=NOW)

    def test_a_warm_cache_gives_an_order(self, provider, repo):
        constraints = RouteConstraints(departure_time=DEPARTURE)
        provider.optimize("home", self.STOPS, None, constraints)
        # Keep asking until every leg the search needs has been queued, then
        # answer them all.
        for _ in range(8):
            answer_all(repo, NOW)
            result = provider.optimize("home", self.STOPS, None, constraints)
            if result.status is not OrderStatus.INCOMPLETE:
                break
        assert result.status is OrderStatus.FEASIBLE
        assert set(result.order) == {"a", "b"}

    def test_a_manual_order_is_checked_against_cached_legs(self, provider, repo):
        constraints = RouteConstraints(departure_time=DEPARTURE, fixed_order=("b", "a"))
        first = provider.optimize("home", self.STOPS, None, constraints)
        assert first.status is OrderStatus.INCOMPLETE
        for _ in range(8):
            answer_all(repo, NOW)
            result = provider.optimize("home", self.STOPS, None, constraints)
            if result.status is not OrderStatus.INCOMPLETE:
                break
        assert result.status is OrderStatus.FEASIBLE
        assert result.order == ("b", "a")


def confirmed(t):
    return dataclasses.replace(t, confirm_code=confirmation_code(t))


def ride(uid, start, end, *, pickup, destinations):
    return confirmed(
        trip(
            notes(
                pickup=pickup,
                family="Sam",
                destinations=destinations,
                driver="Riley",
                status="confirmed",
            ),
            uid=uid,
            start=start,
            end=end,
        )
    )


class TestEngine:
    def inputs(self):
        lesson = activity("lesson", ("sam",), "studio", at(16), at(17))
        to_lesson = ride(
            "to",
            at(15, 30),
            at(16),
            pickup="Home",
            destinations=(("Sam", "Music Studio"),),
        )
        from_lesson = ride(
            "from",
            at(17),
            at(17, 30),
            pickup="Music Studio",
            destinations=(("Sam", "HOME"),),
        )
        return assessment(activities=(lesson,), trips=(to_lesson, from_lesson))

    def test_a_miss_is_incomplete_with_route_010_and_a_hit_is_clear(self, repo):
        inp = self.inputs()
        provider = HaRelayRouteProvider(repo, ZONE, clock=lambda: ENGINE_NOW)
        assert assess(inp, routes(), now=ENGINE_NOW).run_status is RunStatus.CLEAR

        missed = assess(inp, provider, now=ENGINE_NOW)
        assert "ROUTE-010" in [t.finding.rule_id for t in missed.findings]
        assert missed.run_status is RunStatus.INCOMPLETE
        queued = repo.pending(now=ENGINE_NOW)
        assert queued

        # Home Assistant answers every queued leg; the same run is now clear.
        answer_all(repo, ENGINE_NOW, seconds=300)
        hit = assess(inp, provider, now=ENGINE_NOW)
        assert hit.run_status is RunStatus.CLEAR
        assert "ROUTE-010" not in [t.finding.rule_id for t in hit.findings]
        assert repo.pending(now=ENGINE_NOW) == []

    def test_a_cache_entry_that_aged_out_makes_the_run_incomplete_again(self, repo):
        inp = self.inputs()
        clock = Clock(ENGINE_NOW)
        provider = HaRelayRouteProvider(repo, ZONE, clock=clock)
        assess(inp, provider, now=ENGINE_NOW)
        answer_all(repo, ENGINE_NOW, seconds=300)
        assert assess(inp, provider, now=ENGINE_NOW).run_status is RunStatus.CLEAR
        clock.now = ENGINE_NOW + CACHE_VALIDITY + timedelta(days=1)
        stale = assess(inp, provider, now=ENGINE_NOW)
        assert stale.run_status is RunStatus.INCOMPLETE
        assert "ROUTE-010" in [t.finding.rule_id for t in stale.findings]
