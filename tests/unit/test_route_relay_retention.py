"""Retention purges for the route cache and the request queue (privacy spec 7)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fl_relay import NOW, make_engine
from sqlalchemy import func, select

from family_logistics.core.exceptions import ValidationError
from family_logistics.db import schema
from family_logistics.db.retention import (
    ROUTE_CACHE_RETENTION_DAYS,
    purge_expired_route_cache,
    purge_expired_route_requests,
)
from family_logistics.db.route_relay import (
    CACHE_VALIDITY,
    REQUEST_TTL,
    EnqueueStatus,
    Leg,
    RouteAnswer,
    RouteRelayRepository,
)

pytestmark = pytest.mark.unit

FRIDAY = 4
NAIVE = datetime.fromisoformat("2026-10-23T07:00:00")


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


@pytest.fixture
def repo(engine):
    return RouteRelayRepository(engine)


def answer(repo, leg, *, now):
    repo.enqueue(leg, now=now)
    (request,) = [r for r in repo.pending(now=now) if r.leg == leg]
    repo.record_results([RouteAnswer(request.request_id, 900)], now=now)


def count(engine, table):
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def test_the_retention_period_is_the_validity_window():
    assert timedelta(days=ROUTE_CACHE_RETENTION_DAYS) == CACHE_VALIDITY


class TestCachePurge:
    def test_entries_older_than_28_days_are_purged(self, repo, engine):
        answer(repo, Leg("home", "high-school", FRIDAY, 32), now=NOW)
        answer(repo, Leg("home", "xc", FRIDAY, 32), now=NOW + timedelta(days=10))
        later = NOW + CACHE_VALIDITY + timedelta(seconds=1)
        with engine.begin() as connection:
            assert purge_expired_route_cache(connection, now=later) == 1
        assert count(engine, schema.route_cache) == 1
        with engine.begin() as connection:
            assert purge_expired_route_cache(connection, now=later) == 0

    def test_an_entry_exactly_28_days_old_is_kept_as_it_is_still_served(
        self, repo, engine
    ):
        leg = Leg("home", "high-school", FRIDAY, 32)
        answer(repo, leg, now=NOW)
        edge = NOW + CACHE_VALIDITY
        with engine.begin() as connection:
            assert purge_expired_route_cache(connection, now=edge) == 0
        assert repo.lookup(leg, now=edge) is not None

    def test_a_naive_now_is_refused_without_echoing_it(self, engine):
        with pytest.raises(ValidationError) as caught, engine.begin() as connection:
            purge_expired_route_cache(connection, now=NAIVE)
        assert "2026" not in str(caught.value)


class TestRequestPurge:
    def test_expired_requests_are_purged_whatever_their_status(self, repo, engine):
        pending = Leg("home", "high-school", FRIDAY, 36)
        answered = Leg("home", "xc", FRIDAY, 32)
        assert repo.enqueue(pending, now=NOW) is EnqueueStatus.CREATED
        answer(repo, answered, now=NOW)
        fresh = Leg("home", "high-school", FRIDAY, 40)
        later = NOW + REQUEST_TTL + timedelta(minutes=1)
        assert repo.enqueue(fresh, now=later) is EnqueueStatus.CREATED
        with engine.begin() as connection:
            assert purge_expired_route_requests(connection, now=later) == 2
        assert count(engine, schema.route_request) == 1
        # The purge does not touch the cache.
        assert count(engine, schema.route_cache) == 1

    def test_a_request_is_kept_until_its_expiry_passes(self, repo, engine):
        repo.enqueue(Leg("home", "high-school", FRIDAY, 36), now=NOW)
        with engine.begin() as connection:
            assert purge_expired_route_requests(connection, now=NOW + REQUEST_TTL) == 0

    def test_a_naive_now_is_refused(self, engine):
        with pytest.raises(ValidationError), engine.begin() as connection:
            purge_expired_route_requests(connection, now=NAIVE)
