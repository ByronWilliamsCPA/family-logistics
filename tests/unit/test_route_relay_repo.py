"""Route relay repository: cache validity, the queue, due requests and results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fl_relay import NOW, ZONE, local, make_engine
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from family_logistics.core.database import create_sqlite_engine
from family_logistics.core.exceptions import ValidationError
from family_logistics.db import route_relay, schema
from family_logistics.db.route_relay import (
    CACHE_VALIDITY,
    MAX_ROUTE_DURATION_SECONDS,
    REQUEST_TTL,
    BatchOutcome,
    EnqueueStatus,
    Leg,
    RejectReason,
    ResultStatus,
    RouteAnswer,
    RouteRelayBusyError,
    RouteRelayRepository,
    bucket_start,
    due_weekday,
    slot_of,
)

pytestmark = pytest.mark.unit

FRIDAY = 4
NAIVE = datetime.fromisoformat("2026-10-23T07:00:00")
SATURDAY = 5
FRIDAY_0800 = Leg("home", "high-school", FRIDAY, 32)


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


@pytest.fixture
def repo(engine):
    return RouteRelayRepository(engine)


def answer_leg(repo, leg, seconds, *, now=NOW):
    """Enqueue a leg and answer its request, returning the stored request id."""
    assert repo.enqueue(leg, now=now) is EnqueueStatus.CREATED
    (request,) = [r for r in repo.pending(now=now) if r.leg == leg]
    outcome = repo.record_results(
        [RouteAnswer(request.request_id, seconds)], now=now, zone=ZONE
    )
    assert outcome.items[0].status is ResultStatus.ACCEPTED
    return request.request_id


class TestSlots:
    def test_a_departure_falls_in_its_local_weekday_and_bucket(self):
        assert slot_of(local(8, 7), ZONE) == (FRIDAY, 32)
        assert slot_of(local(8, 14, second=59), ZONE) == (FRIDAY, 32)
        assert slot_of(local(8, 15), ZONE) == (FRIDAY, 33)
        assert slot_of(local(0, 0), ZONE) == (FRIDAY, 0)
        assert slot_of(local(23, 59), ZONE) == (FRIDAY, 95)

    def test_the_slot_uses_the_local_zone_not_utc(self):
        # 2026-10-24 02:00 UTC is still Friday evening locally.
        late = datetime(2026, 10, 24, 2, 0, tzinfo=UTC)
        assert slot_of(late, ZONE) == (FRIDAY, 76)

    def test_a_naive_departure_is_refused(self):
        with pytest.raises(ValidationError):
            slot_of(NAIVE, ZONE)

    def test_a_bucket_starts_on_a_quarter_hour(self):
        assert bucket_start(0).isoformat() == "00:00:00"
        assert bucket_start(32).isoformat() == "08:00:00"
        assert bucket_start(95).isoformat() == "23:45:00"

    def test_leg_at_builds_from_a_departure(self):
        assert Leg.at("home", "high-school", local(8, 5), ZONE) == FRIDAY_0800


class TestLookup:
    def test_a_miss_is_none(self, repo):
        assert repo.lookup(FRIDAY_0800, now=NOW) is None

    def test_a_hit_returns_the_observed_seconds(self, repo):
        answer_leg(repo, FRIDAY_0800, 1260)
        found = repo.lookup(FRIDAY_0800, now=NOW + timedelta(hours=1))
        assert found is not None
        assert found.duration == timedelta(seconds=1260)
        assert found.observed_at == NOW

    def test_the_observation_is_stamped_with_the_server_clock(self, repo, engine):
        answer_leg(repo, FRIDAY_0800, 900)
        with engine.connect() as connection:
            observed = connection.execute(
                select(schema.route_cache.c.observed_at)
            ).scalar_one()
        assert observed == NOW

    def test_an_observation_exactly_28_days_old_is_served(self, repo):
        answer_leg(repo, FRIDAY_0800, 900)
        assert repo.lookup(FRIDAY_0800, now=NOW + CACHE_VALIDITY) is not None

    def test_an_observation_older_than_28_days_is_not_served(self, repo):
        answer_leg(repo, FRIDAY_0800, 900)
        stale = NOW + CACHE_VALIDITY + timedelta(seconds=1)
        assert repo.lookup(FRIDAY_0800, now=stale) is None

    def test_an_observation_dated_in_the_future_is_not_served(self, repo):
        evening = Leg("home", "high-school", FRIDAY, 80)  # Friday 20:00
        answer_leg(repo, evening, 900, now=NOW + timedelta(hours=2))
        assert repo.lookup(evening, now=NOW) is None

    @pytest.mark.parametrize(
        "other",
        [
            Leg("home", "high-school", SATURDAY, 32),
            Leg("home", "high-school", FRIDAY, 33),
            Leg("home", "high-school", FRIDAY, 31),
            Leg("high-school", "home", FRIDAY, 32),
            Leg("home", "xc", FRIDAY, 32),
        ],
        ids=["weekday", "next-bucket", "previous-bucket", "reversed", "destination"],
    )
    def test_another_weekday_bucket_or_leg_is_not_served(self, repo, other):
        answer_leg(repo, FRIDAY_0800, 900)
        assert repo.lookup(other, now=NOW) is None

    def test_a_naive_now_is_refused(self, repo):
        with pytest.raises(ValidationError):
            repo.lookup(FRIDAY_0800, now=NAIVE)


class TestEnqueue:
    def test_a_new_leg_is_queued_once(self, repo):
        assert repo.enqueue(FRIDAY_0800, now=NOW) is EnqueueStatus.CREATED
        assert repo.enqueue(FRIDAY_0800, now=NOW) is EnqueueStatus.EXISTS
        (request,) = repo.pending(now=NOW)
        assert request.leg == FRIDAY_0800
        assert request.created_at == NOW
        assert request.expires_at == NOW + REQUEST_TTL
        assert len(request.request_id) == 32

    def test_other_weekdays_and_buckets_are_separate_requests(self, repo):
        assert repo.enqueue(FRIDAY_0800, now=NOW) is EnqueueStatus.CREATED
        other_day = Leg("home", "high-school", SATURDAY, 32)
        other_bucket = Leg("home", "high-school", FRIDAY, 33)
        assert repo.enqueue(other_day, now=NOW) is EnqueueStatus.CREATED
        assert repo.enqueue(other_bucket, now=NOW) is EnqueueStatus.CREATED
        assert len(repo.pending(now=NOW)) == 3

    def test_an_expired_pending_request_is_replaced(self, repo):
        assert repo.enqueue(FRIDAY_0800, now=NOW) is EnqueueStatus.CREATED
        (old,) = repo.pending(now=NOW)
        later = NOW + REQUEST_TTL + timedelta(minutes=1)
        assert repo.enqueue(FRIDAY_0800, now=later) is EnqueueStatus.CREATED
        (new,) = repo.pending(now=later)
        assert new.request_id != old.request_id

    def test_an_answered_leg_can_be_requested_again(self, repo):
        answer_leg(repo, FRIDAY_0800, 900)
        stale = NOW + CACHE_VALIDITY + timedelta(days=1)
        assert repo.enqueue(FRIDAY_0800, now=stale) is EnqueueStatus.CREATED

    def test_the_queue_is_capped(self, repo, monkeypatch):
        monkeypatch.setattr(route_relay, "MAX_PENDING_REQUESTS", 2)
        assert repo.enqueue(Leg("home", "high-school", 0, 1), now=NOW) is (
            EnqueueStatus.CREATED
        )
        assert repo.enqueue(Leg("home", "high-school", 0, 2), now=NOW) is (
            EnqueueStatus.CREATED
        )
        assert repo.enqueue(Leg("home", "high-school", 0, 3), now=NOW) is (
            EnqueueStatus.FULL
        )
        assert len(repo.pending(now=NOW)) == 2
        # An existing leg is still reported as existing, and expiry frees room.
        assert repo.enqueue(Leg("home", "high-school", 0, 1), now=NOW) is (
            EnqueueStatus.EXISTS
        )
        later = NOW + REQUEST_TTL + timedelta(minutes=1)
        assert repo.enqueue(Leg("home", "high-school", 0, 3), now=later) is (
            EnqueueStatus.CREATED
        )

    def test_an_unknown_location_is_refused_and_nothing_is_written(self, repo):
        with pytest.raises(IntegrityError):
            repo.enqueue(Leg("nowhere", "high-school", FRIDAY, 32), now=NOW)
        assert repo.pending(now=NOW) == []


class TestDueRequests:
    def due_ids(self, repo, now, legs):
        created = now - timedelta(minutes=1)
        for leg in legs:
            repo.enqueue(leg, now=created)
        by_id = {r.request_id: r.leg for r in repo.pending(now=created)}
        return {by_id[r.request_id] for r in repo.due_requests(now=now, zone=ZONE)}

    def test_today_is_due_while_its_time_is_still_ahead(self, repo):
        leg = Leg("home", "high-school", FRIDAY, 32)  # Friday 08:00
        assert self.due_ids(repo, local(7, 0), [leg]) == {leg}

    def test_today_is_not_due_once_its_time_has_passed(self, repo):
        # At 09:00 on Friday the next 08:00 is Saturday's, so a Friday request
        # waits until next Friday (it is priced then, not at some other time).
        leg = Leg("home", "high-school", FRIDAY, 32)
        assert self.due_ids(repo, local(9, 0), [leg]) == set()

    def test_tomorrow_is_due_after_the_time_has_passed_today(self, repo):
        leg = Leg("home", "high-school", SATURDAY, 32)
        assert self.due_ids(repo, local(9, 0), [leg]) == {leg}

    def test_a_later_weekday_is_not_due(self, repo):
        tuesday = Leg("home", "high-school", 1, 32)
        sunday = Leg("home", "high-school", 6, 32)
        assert self.due_ids(repo, local(9, 0), [tuesday, sunday]) == set()

    def test_the_moment_the_time_arrives_it_has_passed(self, repo):
        leg = Leg("home", "high-school", FRIDAY, 32)
        assert self.due_ids(repo, local(8, 0), [leg]) == set()
        assert self.due_ids(repo, local(7, 59, second=59), [leg]) == set()

    def test_a_request_inside_the_lead_time_is_not_served(self, repo):
        # Home Assistant prices some seconds after the poll; a request whose
        # time is closer than the lead could be priced for the next day.
        friday = Leg("home", "high-school", FRIDAY, 32)
        saturday = Leg("home", "high-school", SATURDAY, 32)
        assert self.due_ids(repo, local(7, 50), [friday, saturday]) == set()
        assert self.due_ids(repo, local(7, 59, second=59), [friday, saturday]) == set()

    def test_a_request_at_least_the_lead_time_away_is_served(self, repo):
        friday = Leg("home", "high-school", FRIDAY, 32)
        saturday = Leg("home", "high-school", SATURDAY, 32)
        assert self.due_ids(repo, local(7, 29), [friday, saturday]) == {friday}
        assert self.due_ids(repo, local(7, 30), [friday, saturday]) == {friday}

    def test_the_lead_time_is_thirty_minutes(self):
        assert timedelta(minutes=30) == route_relay.SERVE_LEAD_TIME

    def test_around_midnight_the_new_day_decides(self, repo):
        friday_late = Leg("home", "high-school", FRIDAY, 95)  # 23:45
        saturday_early = Leg("home", "high-school", SATURDAY, 1)  # 00:15
        saturday_morning = Leg("home", "high-school", SATURDAY, 32)
        legs = [friday_late, saturday_early, saturday_morning]
        # Friday 23:10: 23:45 is 35 minutes away (Friday's), 00:15 and 08:00
        # next occur on Saturday.
        assert self.due_ids(repo, local(23, 10), legs) == {
            friday_late,
            saturday_early,
            saturday_morning,
        }
        # Friday 23:50: 23:45 has passed (the next one is Saturday's, and a
        # Friday request waits a week); 00:15 is inside the lead time.
        assert self.due_ids(repo, local(23, 50), legs) == {saturday_morning}
        # Saturday 00:05: 00:15 is inside the lead time; 23:45 is Saturday's.
        assert self.due_ids(repo, local(0, 5, day=24), legs) == {saturday_morning}
        # Saturday 00:20: 00:15 has passed, so it waits for next Saturday.
        assert self.due_ids(repo, local(0, 20, day=24), legs) == {saturday_morning}

    def test_a_time_inside_the_spring_forward_gap_fails_closed(self, repo):
        # 2026-03-08 is a Sunday; 02:30 local does not exist that day. A
        # nonexistent wall time on either of the next two days is not served,
        # and neither is the adjacent day's request that shares the bucket.
        gap = Leg("home", "high-school", 6, 10)  # Sunday 02:30
        monday = Leg("home", "high-school", 0, 10)  # Monday 02:30
        legs = [gap, monday]
        assert self.due_ids(repo, local(12, 0, month=3, day=7), legs) == set()
        assert self.due_ids(repo, local(0, 30, month=3, day=8), legs) == set()
        # From Monday 00:10 both of the next two days (Monday, Tuesday) exist.
        assert self.due_ids(repo, local(0, 10, month=3, day=9), legs) == {monday}

    def test_a_time_after_the_gap_keeps_its_weekday(self, repo):
        sunday_0900 = Leg("home", "high-school", 6, 36)
        saturday_noon = local(12, 0, month=3, day=7)
        assert self.due_ids(repo, saturday_noon, [sunday_0900]) == {sunday_0900}

    def test_an_ambiguous_fall_back_time_fails_closed(self, repo):
        # 2026-11-01 is a Sunday; 01:30 happens twice. Which pass Home
        # Assistant prices is unknowable, so the request is not served.
        sunday = Leg("home", "high-school", 6, 6)  # Sunday 01:30
        monday = Leg("home", "high-school", 0, 6)  # Monday 01:30
        legs = [sunday, monday]
        assert self.due_ids(repo, local(12, 0, month=10, day=31), legs) == set()
        # The reviewer's case: Sunday 01:10 on the second pass (PST). The true
        # next 01:30 is Sunday's, not Monday's.
        second_pass = datetime(2026, 11, 1, 1, 10, tzinfo=ZONE, fold=1)
        assert self.due_ids(repo, second_pass, legs) == set()
        # Once the ambiguous day is behind the next two days, Monday is served.
        assert self.due_ids(repo, local(0, 10, month=11, day=2), legs) == {monday}

    def test_due_weekday_helper(self):
        assert due_weekday(32, local(7, 0), ZONE) == FRIDAY
        assert due_weekday(32, local(9, 0), ZONE) == SATURDAY
        assert due_weekday(32, local(7, 59, second=59), ZONE) == FRIDAY
        assert due_weekday(32, local(8, 0), ZONE) == SATURDAY

    def test_due_weekday_is_none_for_a_nonexistent_or_ambiguous_time(self):
        assert due_weekday(10, local(12, 0, month=3, day=7), ZONE) is None
        assert due_weekday(6, local(12, 0, month=10, day=31), ZONE) is None

    def test_an_expired_request_is_not_served(self, repo):
        leg = Leg("home", "high-school", FRIDAY, 32)
        repo.enqueue(leg, now=NOW)
        later = NOW + REQUEST_TTL + timedelta(minutes=1)
        assert repo.due_requests(now=later, zone=ZONE) == []

    def test_an_answered_request_is_not_served(self, repo):
        leg = Leg("home", "high-school", FRIDAY, 32)
        answer_leg(repo, leg, 900)
        assert repo.due_requests(now=NOW, zone=ZONE) == []

    def test_the_poll_is_limited_and_oldest_first(self, repo):
        legs = [Leg("home", "high-school", FRIDAY, bucket) for bucket in (40, 41, 42)]
        for index, leg in enumerate(legs):
            repo.enqueue(leg, now=NOW + timedelta(seconds=index))
        found = repo.due_requests(now=local(7, 30), zone=ZONE, limit=2)
        assert [r.leg for r in found] == legs[:2]


class TestLocationSources:
    def test_zone_and_protected_address_are_returned_undecrypted(self, repo):
        sources = repo.location_sources({"home", "middle-school", "missing"})
        assert set(sources) == {"home", "middle-school"}
        assert sources["home"].zone_entity_id == "zone.home_base"
        assert sources["home"].address_token is not None
        assert sources["home"].address_token.startswith("gAAAAA")
        assert sources["middle-school"].zone_entity_id is None
        assert sources["middle-school"].address_token is None

    def test_no_ids_means_no_query(self, repo):
        assert repo.location_sources(set()) == {}


class TestRecordResults:
    def request_for(self, repo, leg=FRIDAY_0800, *, now=NOW):
        repo.enqueue(leg, now=now)
        (request,) = [r for r in repo.pending(now=now) if r.leg == leg]
        return request

    def test_an_answer_fills_the_cache_for_the_requests_own_leg(self, repo, engine):
        request = self.request_for(repo, Leg("high-school", "home", FRIDAY, 70))
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, 840)], now=NOW, zone=ZONE
        )
        assert [i.status for i in outcome.items] == [ResultStatus.ACCEPTED]
        assert (
            outcome.count(ResultStatus.ACCEPTED),
            outcome.count(ResultStatus.REJECTED),
        ) == (1, 0)
        with engine.connect() as connection:
            rows = connection.execute(select(schema.route_cache)).mappings().all()
        assert [
            (
                r["origin_location_id"],
                r["destination_location_id"],
                r["weekday"],
                r["bucket"],
                r["duration_seconds"],
            )
            for r in rows
        ] == [("high-school", "home", FRIDAY, 70, 840)]
        assert repo.pending(now=NOW) == []

    def test_an_unknown_request_id_is_rejected(self, repo):
        outcome = repo.record_results([RouteAnswer("f" * 32, 900)], now=NOW, zone=ZONE)
        assert outcome.items == (
            route_relay.ItemOutcome(
                "f" * 32, ResultStatus.REJECTED, RejectReason.UNKNOWN_REQUEST
            ),
        )

    def test_an_expired_request_is_rejected_and_stores_nothing(self, repo, engine):
        request = self.request_for(repo)
        later = NOW + REQUEST_TTL
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, 900)], now=later, zone=ZONE
        )
        assert outcome.items[0].reason is RejectReason.EXPIRED
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(func.count()).select_from(schema.route_cache)
                ).scalar_one()
                == 0
            )

    def test_the_same_value_again_is_an_idempotent_no_op(self, repo, engine):
        request = self.request_for(repo)
        repo.record_results([RouteAnswer(request.request_id, 900)], now=NOW, zone=ZONE)
        later = NOW + timedelta(hours=3)
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, 900)], now=later, zone=ZONE
        )
        assert outcome.items[0].status is ResultStatus.UNCHANGED
        assert outcome.items[0].reason is None
        # The observation time was not refreshed by the replay.
        found = repo.lookup(FRIDAY_0800, now=later)
        assert found is not None
        assert found.observed_at == NOW

    def test_a_different_value_for_an_answered_request_is_a_conflict(self, repo):
        request = self.request_for(repo)
        repo.record_results([RouteAnswer(request.request_id, 900)], now=NOW, zone=ZONE)
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, 1800)], now=NOW, zone=ZONE
        )
        assert outcome.items[0].status is ResultStatus.REJECTED
        assert outcome.items[0].reason is RejectReason.CONFLICT
        found = repo.lookup(FRIDAY_0800, now=NOW)
        assert found is not None
        assert found.duration == timedelta(seconds=900)

    def test_a_replay_after_the_request_expired_is_still_a_no_op(self, repo):
        request = self.request_for(repo)
        repo.record_results([RouteAnswer(request.request_id, 900)], now=NOW, zone=ZONE)
        later = NOW + REQUEST_TTL + timedelta(days=1)
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, 900)], now=later, zone=ZONE
        )
        assert outcome.items[0].status is ResultStatus.UNCHANGED

    def test_a_newer_answer_replaces_the_cache_entry(self, repo):
        first = self.request_for(repo)
        repo.record_results([RouteAnswer(first.request_id, 900)], now=NOW, zone=ZONE)
        later = NOW + CACHE_VALIDITY + timedelta(days=7)  # a Friday again
        second = self.request_for(repo, now=later)
        repo.record_results(
            [RouteAnswer(second.request_id, 1000)], now=later, zone=ZONE
        )
        found = repo.lookup(FRIDAY_0800, now=later)
        assert found is not None
        assert found.duration == timedelta(seconds=1000)
        assert found.observed_at == later

    def test_items_are_judged_independently_and_in_order(self, repo):
        good = self.request_for(repo)
        outcome = repo.record_results(
            [
                RouteAnswer("e" * 32, 900),
                RouteAnswer(good.request_id, 900),
                RouteAnswer(good.request_id, 900),
                RouteAnswer(good.request_id, 901),
            ],
            now=NOW,
            zone=ZONE,
        )
        assert [(i.status, i.reason) for i in outcome.items] == [
            (ResultStatus.REJECTED, RejectReason.UNKNOWN_REQUEST),
            (ResultStatus.ACCEPTED, None),
            (ResultStatus.UNCHANGED, None),
            (ResultStatus.REJECTED, RejectReason.CONFLICT),
        ]

    def test_a_result_received_after_its_time_arrived_is_rejected_not_due(
        self, repo, engine
    ):
        # Polled at Friday 07:59:59 and priced at 08:00:01, Home Assistant would
        # have priced Saturday 08:00; filing that as Friday would be wrong.
        request = self.request_for(repo)
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, 900)],
            now=local(8, 0, second=2),
            zone=ZONE,
        )
        assert outcome.items == (
            route_relay.ItemOutcome(
                request.request_id, ResultStatus.REJECTED, RejectReason.NOT_DUE
            ),
        )
        assert RejectReason.NOT_DUE.value == "not_due"
        assert repo.lookup(FRIDAY_0800, now=local(8, 0, second=2)) is None
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(func.count()).select_from(schema.route_cache)
                ).scalar_one()
                == 0
            )
        # Still pending, so it is served again in its next due window and a
        # result received then is accepted.
        assert [r.request_id for r in repo.pending(now=NOW)] == [request.request_id]
        next_friday = local(7, 0, day=30)
        assert [
            r.request_id for r in repo.due_requests(now=next_friday, zone=ZONE)
        ] == [request.request_id]
        retry = repo.record_results(
            [RouteAnswer(request.request_id, 900)], now=next_friday, zone=ZONE
        )
        assert retry.items[0].status is ResultStatus.ACCEPTED

    def test_a_result_well_before_its_time_is_accepted(self, repo):
        request = self.request_for(repo, now=local(0, 0))
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, 900)], now=local(0, 5), zone=ZONE
        )
        assert outcome.items[0].status is ResultStatus.ACCEPTED

    def test_a_result_for_the_wrong_weekday_is_not_due(self, repo):
        # A Saturday 08:00 request answered on Friday morning: the next 08:00
        # is Friday's, so the answer would measure the wrong day.
        saturday = Leg("home", "high-school", SATURDAY, 32)
        request = self.request_for(repo, saturday)
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, 900)], now=NOW, zone=ZONE
        )
        assert outcome.items[0].reason is RejectReason.NOT_DUE
        assert repo.lookup(saturday, now=NOW) is None

    def test_a_result_for_an_ambiguous_time_is_not_due(self, repo):
        monday = Leg("home", "high-school", 0, 6)  # Monday 01:30
        created = local(12, 0, month=10, day=31)
        request = self.request_for(repo, monday, now=created)
        second_pass = datetime(2026, 11, 1, 1, 10, tzinfo=ZONE, fold=1)
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, 900)], now=second_pass, zone=ZONE
        )
        assert outcome.items[0].reason is RejectReason.NOT_DUE
        assert repo.lookup(monday, now=second_pass) is None

    def test_an_expired_or_replayed_request_is_judged_before_due(self, repo):
        request = self.request_for(repo)
        repo.record_results([RouteAnswer(request.request_id, 900)], now=NOW, zone=ZONE)
        # Replays are idempotent even when the time of day has since passed.
        replay = repo.record_results(
            [RouteAnswer(request.request_id, 900)], now=local(9, 0), zone=ZONE
        )
        assert replay.items[0].status is ResultStatus.UNCHANGED

    @pytest.mark.parametrize("seconds", [0, -1, MAX_ROUTE_DURATION_SECONDS + 1])
    def test_a_duration_outside_the_range_is_refused_before_any_write(
        self, repo, seconds
    ):
        request = self.request_for(repo)
        with pytest.raises(ValidationError):
            repo.record_results(
                [RouteAnswer(request.request_id, 900), RouteAnswer("x", seconds)],
                now=NOW,
                zone=ZONE,
            )
        assert repo.lookup(FRIDAY_0800, now=NOW) is None
        assert len(repo.pending(now=NOW)) == 1

    def test_the_upper_bound_itself_is_accepted(self, repo):
        request = self.request_for(repo)
        outcome = repo.record_results(
            [RouteAnswer(request.request_id, MAX_ROUTE_DURATION_SECONDS)],
            now=NOW,
            zone=ZONE,
        )
        assert outcome.items[0].status is ResultStatus.ACCEPTED

    def test_a_failing_hook_rolls_the_whole_batch_back(self, repo):
        request = self.request_for(repo)

        def fail(_connection, _outcome):
            msg = "audit failed"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="audit failed"):
            repo.record_results(
                [RouteAnswer(request.request_id, 900)],
                now=NOW,
                before_commit=fail,
                zone=ZONE,
            )
        assert repo.lookup(FRIDAY_0800, now=NOW) is None
        assert [r.request_id for r in repo.pending(now=NOW)] == [request.request_id]

    def test_the_hook_sees_the_outcome(self, repo):
        request = self.request_for(repo)
        seen: list[BatchOutcome] = []
        repo.record_results(
            [RouteAnswer(request.request_id, 900)],
            now=NOW,
            before_commit=lambda _c, outcome: seen.append(outcome),
            zone=ZONE,
        )
        assert [i.status for i in seen[0].items] == [ResultStatus.ACCEPTED]

    def test_a_naive_now_is_refused(self, repo):
        with pytest.raises(ValidationError):
            repo.record_results([], now=NAIVE, zone=ZONE)


class TestWriteLock:
    def test_a_held_write_lock_raises_busy_and_writes_nothing(self, engine, tmp_path):
        repo = RouteRelayRepository(engine, lock_timeout_ms=50)
        holder = create_sqlite_engine(tmp_path / "fl.db")
        try:
            with holder.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                with pytest.raises(RouteRelayBusyError) as caught:
                    repo.enqueue(FRIDAY_0800, now=NOW)
                assert caught.value.retry_after == route_relay.LOCK_RETRY_AFTER_SECONDS
                with pytest.raises(RouteRelayBusyError):
                    repo.record_results(
                        [RouteAnswer("a" * 32, 900)], now=NOW, zone=ZONE
                    )
                connection.rollback()
        finally:
            holder.dispose()
        assert repo.pending(now=NOW) == []
        # The busy timeout is restored for later connections' work.
        assert repo.enqueue(FRIDAY_0800, now=NOW) is EnqueueStatus.CREATED
