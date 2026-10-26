"""GET /api/v1/route-requests and POST /api/v1/route-results (ADR 0006)."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fl_relay import (
    DATA_KEY,
    DRIVER_S_ADDRESS,
    HOME_ADDRESS,
    NOW,
    PASSENGER_E_ADDRESS,
    STUDIO_ADDRESS,
    ZONE,
    Clock,
    audit_key_text,
    clear_zone,
    local,
    make_engine,
    make_writes_fail,
    register_passenger_default,
    register_person,
)
from pydantic import SecretStr
from sqlalchemy import text

from family_logistics.adapters.routing import HaRelayRouteProvider
from family_logistics.api import route_relay as route_relay_api
from family_logistics.api.app import create_app
from family_logistics.api.ha import MAX_FAILURES, build_ha_router
from family_logistics.api.route_relay import (
    MAX_BODY_BYTES,
    MAX_RESULTS_PER_POST,
    register_route_relay_routes,
)
from family_logistics.core.config import Settings
from family_logistics.core.crypto import AddressCipher
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db.route_relay import (
    MAX_REQUESTS_PER_POLL,
    MAX_ROUTE_DURATION_SECONDS,
    Leg,
    RouteAnswer,
    RouteRelayRepository,
)
from family_logistics.domain.models import PersonRole

pytestmark = pytest.mark.unit

TOKEN = "t" * 40
GOOD = {"Authorization": f"Bearer {TOKEN}"}
FRIDAY = 4
HOME_TO_SCHOOL = Leg("home", "high-school", FRIDAY, 32)
HOME_TO_STUDIO = Leg("home", "studio", FRIDAY, 32)
GET = "/api/v1/route-requests"
POST = "/api/v1/route-results"


def make_settings(tmp_path, **overrides):
    values = {
        "ha_token": TOKEN,
        "data_key": DATA_KEY,
        "audit_key": SecretStr(audit_key_text()),
        "database_path": tmp_path / "fl.db",
        "timezone": str(ZONE),
    }
    values.update(overrides)
    return Settings.model_validate(values)


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


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def build_client(settings, engine, clock, *, lock_timeout_ms=None):
    router = build_ha_router(settings)
    repository = (
        RouteRelayRepository(engine)
        if lock_timeout_ms is None
        else RouteRelayRepository(engine, lock_timeout_ms=lock_timeout_ms)
    )
    register_route_relay_routes(router, settings, repository, clock=clock)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def client(settings, engine, clock):
    return build_client(settings, engine, clock)


def queue(repo, *legs, now=NOW):
    for leg in legs:
        repo.enqueue(leg, now=now)
    return {r.leg: r.request_id for r in repo.pending(now=now)}


def audit_rows(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT actor, object_ref, action, new_hash FROM audit_log")
        ).all()


class TestAuth:
    def test_both_routes_refuse_a_missing_token(self, client):
        assert client.get(GET).status_code == 401
        assert client.post(POST, json={"results": []}).status_code == 401

    def test_both_routes_refuse_a_wrong_token(self, client):
        bad = {"Authorization": "Bearer " + "x" * 40}
        assert client.get(GET, headers=bad).status_code == 401
        body = {"results": [{"request_id": "a", "duration_seconds": 60}]}
        assert client.post(POST, headers=bad, json=body).status_code == 401

    def test_authentication_comes_before_body_validation(self, client):
        assert client.post(POST, json={"nonsense": True}).status_code == 401

    @pytest.mark.parametrize(
        "content",
        [b"not json", b"\xff\xfe", b'{"results": [', b"", b"[" * 100_000],
        ids=["text", "bad-utf8", "truncated", "empty", "deeply-nested"],
    )
    def test_authentication_comes_before_a_malformed_body(self, client, content):
        assert client.post(POST, content=content).status_code == 401
        bad = {"Authorization": "Bearer " + "x" * 40}
        assert client.post(POST, headers=bad, content=content).status_code == 401

    def test_an_oversized_body_without_a_token_is_a_401_not_a_413(self, client):
        content = b"x" * (MAX_BODY_BYTES + 1)
        assert client.post(POST, content=content).status_code == 401

    def test_malformed_unauthenticated_posts_are_throttled(self, client):
        # The body is never parsed before the token check, so every attempt
        # counts toward the failure throttle.
        for _ in range(MAX_FAILURES):
            assert client.post(POST, content=b"not json").status_code == 401
        assert client.post(POST, content=b"not json").status_code == 429

    def test_an_unconfigured_token_denies_everything(self, tmp_path, engine, clock):
        unset = make_settings(tmp_path, ha_token=None)
        client = build_client(unset, engine, clock)
        assert client.get(GET, headers=GOOD).status_code == 401

    def test_the_routes_are_registered_by_create_app(self, settings, engine):
        client = TestClient(create_app(settings))
        assert client.get(GET).status_code == 401
        assert client.get(GET, headers=GOOD).status_code == 200


class TestGetRouteRequests:
    def test_an_empty_queue_is_an_empty_list(self, client):
        response = client.get(GET, headers=GOOD)
        assert response.status_code == 200
        assert response.json() == {"requests": []}

    def test_a_zoned_location_is_sent_as_its_zone_id_and_never_as_an_address(
        self, client, repo
    ):
        # "home" has both a zone and an address; the address must not be used.
        ids = queue(repo, HOME_TO_SCHOOL)
        response = client.get(GET, headers=GOOD)
        assert response.status_code == 200
        assert response.json() == {
            "requests": [
                {
                    "request_id": ids[HOME_TO_SCHOOL],
                    "origin": {"kind": "zone", "value": "zone.home_base"},
                    "destination": {"kind": "zone", "value": "zone.school_a"},
                    "weekday": "fri",
                    "departure_time": "08:00:00",
                }
            ]
        }
        assert HOME_ADDRESS not in response.text
        assert STUDIO_ADDRESS not in response.text

    def test_a_zone_less_location_is_sent_as_its_decrypted_address(self, client, repo):
        queue(repo, HOME_TO_STUDIO)
        (item,) = client.get(GET, headers=GOOD).json()["requests"]
        assert item["origin"] == {"kind": "zone", "value": "zone.home_base"}
        assert item["destination"] == {"kind": "address", "value": STUDIO_ADDRESS}
        assert HOME_ADDRESS not in client.get(GET, headers=GOOD).text

    def test_a_request_with_no_usable_endpoint_is_not_served_and_logs_no_address(
        self, client, repo, caplog
    ):
        # middle-school has neither a zone nor an address.
        queue(repo, Leg("home", "middle-school", FRIDAY, 32))
        with caplog.at_level(logging.WARNING):
            response = client.get(GET, headers=GOOD)
        assert response.json() == {"requests": []}
        assert "route_relay.request_unroutable" in caplog.text
        assert HOME_ADDRESS not in caplog.text
        assert STUDIO_ADDRESS not in caplog.text

    def test_an_address_that_cannot_be_decrypted_is_not_served(
        self, tmp_path, engine, clock, repo, caplog
    ):
        wrong_key = make_settings(
            tmp_path,
            data_key=SecretStr("A" * 43 + "="),
        )
        client = build_client(wrong_key, engine, clock)
        queue(repo, HOME_TO_STUDIO)
        with caplog.at_level(logging.WARNING):
            response = client.get(GET, headers=GOOD)
        assert response.json() == {"requests": []}
        assert "route_relay.request_unroutable" in caplog.text
        assert STUDIO_ADDRESS not in caplog.text

    def test_an_address_is_not_served_without_a_data_key(
        self, tmp_path, engine, clock, repo
    ):
        no_key = make_settings(tmp_path, data_key=None)
        client = build_client(no_key, engine, clock)
        queue(repo, HOME_TO_STUDIO)
        assert client.get(GET, headers=GOOD).json() == {"requests": []}

    @pytest.mark.parametrize("cause", ["no-endpoint", "wrong-key", "no-key"])
    def test_unroutable_requests_do_not_starve_a_routable_one(
        self, tmp_path, engine, clock, repo, cause
    ):
        # 50 unroutable requests are queued before the routable one. They are
        # dropped first and the poll limit applies to what is left.
        if cause == "no-endpoint":
            client, destination = (
                build_client(make_settings(tmp_path), engine, clock),
                ("middle-school"),
            )
        else:
            key = {"wrong-key": SecretStr("A" * 43 + "="), "no-key": None}[cause]
            client = build_client(make_settings(tmp_path, data_key=key), engine, clock)
            destination = "studio"
        for bucket in range(40, 40 + MAX_REQUESTS_PER_POLL):
            repo.enqueue(Leg("home", destination, FRIDAY, bucket), now=NOW)
        ids = queue(repo, HOME_TO_SCHOOL, now=NOW + timedelta(seconds=1))
        items = client.get(GET, headers=GOOD).json()["requests"]
        assert [item["request_id"] for item in items] == [ids[HOME_TO_SCHOOL]]

    def test_the_poll_limit_applies_after_unroutable_requests_are_dropped(
        self, client, repo, monkeypatch
    ):
        monkeypatch.setattr(route_relay_api, "MAX_REQUESTS_PER_POLL", 2)
        for bucket in (40, 41, 42):
            repo.enqueue(Leg("home", "middle-school", FRIDAY, bucket), now=NOW)
        routable = [Leg("home", "high-school", FRIDAY, b) for b in (50, 51, 52)]
        for index, leg in enumerate(routable, start=1):
            repo.enqueue(leg, now=NOW + timedelta(seconds=index))
        items = client.get(GET, headers=GOOD).json()["requests"]
        assert [item["departure_time"] for item in items] == ["12:30:00", "12:45:00"]

    def test_a_zone_pair_needs_no_data_key(self, tmp_path, engine, clock, repo):
        no_key = make_settings(tmp_path, data_key=None)
        client = build_client(no_key, engine, clock)
        queue(repo, HOME_TO_SCHOOL)
        assert len(client.get(GET, headers=GOOD).json()["requests"]) == 1

    def test_only_due_requests_are_served(self, client, repo, clock):
        today = Leg("home", "high-school", FRIDAY, 32)  # Friday 08:00
        tomorrow = Leg("home", "xc", 5, 32)  # Saturday 08:00
        queue(repo, today, tomorrow)

        def served():
            return {
                (r["weekday"], r["departure_time"])
                for r in client.get(GET, headers=GOOD).json()["requests"]
            }

        clock.now = local(7, 0)
        assert served() == {("fri", "08:00:00")}
        # After 08:00 the next 08:00 is Saturday's: the Friday request waits a
        # week and the Saturday one is now the due one.
        clock.now = local(9, 0)
        assert served() == {("sat", "08:00:00")}

    def test_a_request_is_served_only_when_its_time_is_the_lead_time_away(
        self, client, repo, clock
    ):
        queue(repo, HOME_TO_SCHOOL)
        clock.now = local(7, 50)  # 10 minutes ahead: too close
        assert client.get(GET, headers=GOOD).json() == {"requests": []}
        clock.now = local(7, 29)  # 31 minutes ahead
        assert len(client.get(GET, headers=GOOD).json()["requests"]) == 1

    def test_a_midnight_request_is_due_on_the_day_that_starts_at_midnight(
        self, client, repo, clock
    ):
        saturday_midnight = Leg("home", "high-school", 5, 0)
        queue(repo, saturday_midnight)
        clock.now = local(23, 20)  # Friday evening: the next 00:00 is Saturday's
        assert len(client.get(GET, headers=GOOD).json()["requests"]) == 1
        clock.now = local(23, 50)  # inside the 30 minute lead time: not served
        assert client.get(GET, headers=GOOD).json() == {"requests": []}
        clock.now = local(0, 5, day=24)  # just after midnight: next is Sunday's
        assert client.get(GET, headers=GOOD).json() == {"requests": []}

    def test_an_expired_request_is_not_served(self, client, repo, clock):
        queue(repo, HOME_TO_SCHOOL)
        clock.now = NOW.replace(year=2027)
        assert client.get(GET, headers=GOOD).json() == {"requests": []}

    def test_an_answered_request_is_not_served_again(self, client, repo):
        ids = queue(repo, HOME_TO_SCHOOL)
        repo.record_results([RouteAnswer(ids[HOME_TO_SCHOOL], 600)], now=NOW, zone=ZONE)
        assert client.get(GET, headers=GOOD).json() == {"requests": []}

    def test_a_poll_writes_nothing_and_logs_only_a_count(
        self, client, repo, engine, caplog
    ):
        queue(repo, HOME_TO_STUDIO)
        with caplog.at_level(logging.INFO):
            client.get(GET, headers=GOOD)
        assert audit_rows(engine) == []
        assert STUDIO_ADDRESS not in caplog.text

    def test_a_location_that_vanished_mid_poll_is_not_served(
        self, client, repo, monkeypatch
    ):
        queue(repo, HOME_TO_SCHOOL)
        monkeypatch.setattr(RouteRelayRepository, "location_sources", lambda *_: {})
        assert client.get(GET, headers=GOOD).json() == {"requests": []}

    def test_a_store_error_is_a_503_not_a_500(self, client, engine):
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE route_request"))
        response = client.get(GET, headers=GOOD)
        assert response.status_code == 503


class TestAddressRelease:
    """The owner's address-release gate at serve time (domain/address_release)."""

    @pytest.fixture
    def decrypted(self, monkeypatch):
        """Record every address decryption attempted while a poll is built."""
        calls = []
        original = AddressCipher.decrypt

        def spy(cipher, token):
            calls.append(token)
            return original(cipher, token)

        monkeypatch.setattr(AddressCipher, "decrypt", spy)
        return calls

    def ciphertext(self, engine, location_id):
        with engine.connect() as connection:
            stored = connection.execute(
                text("SELECT address_ciphertext FROM location WHERE location_id = :i"),
                {"i": location_id},
            ).scalar_one()
        return stored.decode() if isinstance(stored, bytes) else stored

    def test_a_zone_less_other_family_home_never_appears_in_a_route_request_payload(
        self, client, repo, engine, decrypted, caplog
    ):
        register_person(engine, "driver-s", PersonRole.CONTACT, "driver-s-home")
        leg_to = Leg("home", "driver-s-home", FRIDAY, 32)
        leg_from = Leg("driver-s-home", "studio", FRIDAY, 33)
        ids = queue(repo, leg_to, leg_from, HOME_TO_SCHOOL)
        with caplog.at_level(logging.WARNING):
            response = client.get(GET, headers=GOOD)
        assert response.status_code == 200
        assert [r["request_id"] for r in response.json()["requests"]] == [
            ids[HOME_TO_SCHOOL]
        ]
        ciphertext = self.ciphertext(engine, "driver-s-home")
        assert ciphertext.startswith("gAAAAA")
        for secret in (DRIVER_S_ADDRESS, ciphertext):
            assert secret not in response.text
            assert secret not in caplog.text
        assert "driver-s-home" not in response.text
        assert ids[leg_to] not in response.text
        assert ids[leg_from] not in response.text
        # Not decrypted at all: not the withheld address, and not the studio
        # address of the leg that merely touches a withheld home.
        assert decrypted == []
        assert STUDIO_ADDRESS not in response.text + caplog.text
        assert "route_relay.request_withheld" in caplog.text

    def test_a_withheld_request_stays_pending_and_is_cleared_by_expiry(
        self, client, repo, engine, clock
    ):
        register_person(engine, "driver-s", PersonRole.CONTACT, "driver-s-home")
        queue(repo, Leg("home", "driver-s-home", FRIDAY, 32))
        assert client.get(GET, headers=GOOD).json() == {"requests": []}
        assert len(repo.pending(now=clock())) == 1
        later = clock() + timedelta(days=9)
        assert repo.pending(now=later) == []

    def test_a_registry_change_after_enqueue_stops_release_at_get_time(
        self, client, repo, engine, clock
    ):
        # The leg is queued by the provider while nobody is registered at the
        # home, so the enqueue gate lets it through (it is a venue then).
        provider = HaRelayRouteProvider(repo, ZONE, clock=clock)
        assert (
            provider.estimate("home", "driver-s-home", local(8, 5), "driving") is None
        )
        (queued,) = repo.pending(now=clock())
        released = client.get(GET, headers=GOOD).json()["requests"]
        assert [r["request_id"] for r in released] == [queued.request_id]
        assert released[0]["destination"] == {
            "kind": "address",
            "value": DRIVER_S_ADDRESS,
        }

        # A contact is registered for that home: the same request is no longer
        # served, and nothing is decrypted for it.
        register_person(engine, "driver-s", PersonRole.CONTACT, "driver-s-home")
        response = client.get(GET, headers=GOOD)
        assert response.json() == {"requests": []}
        assert DRIVER_S_ADDRESS not in response.text
        assert [r.request_id for r in repo.pending(now=clock())] == [queued.request_id]

    def test_the_households_own_zone_less_home_and_a_venue_are_still_sent_as_addresses(
        self, client, repo, engine
    ):
        clear_zone(engine, "home")
        register_person(engine, "morgan", PersonRole.PARENT, "home")
        register_person(engine, "alex", PersonRole.CHILD, "home")
        queue(repo, HOME_TO_STUDIO)
        (item,) = client.get(GET, headers=GOOD).json()["requests"]
        assert item["origin"] == {"kind": "address", "value": HOME_ADDRESS}
        assert item["destination"] == {"kind": "address", "value": STUDIO_ADDRESS}

    def test_a_passengers_default_pickup_alone_is_a_venue_and_is_sent(
        self, client, repo, engine
    ):
        register_passenger_default(engine, "passenger-e", "passenger-e-home")
        queue(repo, Leg("home", "passenger-e-home", FRIDAY, 32))
        (item,) = client.get(GET, headers=GOOD).json()["requests"]
        assert item["destination"] == {
            "kind": "address",
            "value": PASSENGER_E_ADDRESS,
        }

    def test_a_contact_from_the_passengers_household_withholds_that_home(
        self, client, repo, engine
    ):
        register_passenger_default(engine, "passenger-e", "passenger-e-home")
        register_person(engine, "driver-s", PersonRole.CONTACT, "passenger-e-home")
        queue(repo, Leg("home", "passenger-e-home", FRIDAY, 32))
        response = client.get(GET, headers=GOOD)
        assert response.json() == {"requests": []}
        assert PASSENGER_E_ADDRESS not in response.text

    def test_a_home_shared_by_a_household_member_and_a_contact_is_withheld(
        self, client, repo, engine
    ):
        register_person(engine, "alex", PersonRole.CHILD, "driver-s-home")
        register_person(engine, "driver-s", PersonRole.CONTACT, "driver-s-home")
        queue(repo, Leg("home", "driver-s-home", FRIDAY, 32))
        response = client.get(GET, headers=GOOD)
        assert response.json() == {"requests": []}
        assert DRIVER_S_ADDRESS not in response.text

    def test_a_zoned_contact_home_is_sent_as_its_zone_id_never_its_address(
        self, client, repo, engine, decrypted
    ):
        register_person(engine, "driver-s", PersonRole.CONTACT, "high-school")
        queue(repo, HOME_TO_SCHOOL)
        (item,) = client.get(GET, headers=GOOD).json()["requests"]
        assert item["destination"] == {"kind": "zone", "value": "zone.school_a"}
        assert decrypted == []

    def test_a_contact_home_whose_zone_was_removed_is_withheld(
        self, client, repo, engine
    ):
        register_person(engine, "driver-s", PersonRole.CONTACT, "high-school")
        clear_zone(engine, "high-school")
        queue(repo, HOME_TO_SCHOOL)
        assert client.get(GET, headers=GOOD).json() == {"requests": []}

    def test_withheld_requests_do_not_starve_a_releasable_one(
        self, client, repo, engine, monkeypatch
    ):
        monkeypatch.setattr(route_relay_api, "MAX_REQUESTS_PER_POLL", 1)
        register_person(engine, "driver-s", PersonRole.CONTACT, "driver-s-home")
        for bucket in (40, 41, 42):
            repo.enqueue(Leg("home", "driver-s-home", FRIDAY, bucket), now=NOW)
        ids = queue(repo, HOME_TO_SCHOOL, now=NOW + timedelta(seconds=1))
        items = client.get(GET, headers=GOOD).json()["requests"]
        assert [i["request_id"] for i in items] == [ids[HOME_TO_SCHOOL]]


class TestPostRouteResults:
    def post(self, client, *items):
        return client.post(POST, headers=GOOD, json={"results": list(items)})

    @staticmethod
    def item(request_id, seconds=900):
        return {"request_id": request_id, "duration_seconds": seconds}

    def test_a_result_fills_the_cache_for_the_requests_own_leg(self, client, repo):
        ids = queue(repo, HOME_TO_SCHOOL)
        response = self.post(client, self.item(ids[HOME_TO_SCHOOL], 1260))
        assert response.status_code == 200
        assert response.json() == {
            "accepted": 1,
            "unchanged": 0,
            "rejected": 0,
            "results": [
                {
                    "request_id": ids[HOME_TO_SCHOOL],
                    "status": "accepted",
                    "reason": None,
                }
            ],
        }
        found = repo.lookup(HOME_TO_SCHOOL, now=NOW)
        assert found is not None
        assert found.duration.total_seconds() == 1260

    def test_a_result_after_the_time_arrived_is_not_due_and_stays_pending(
        self, client, repo, clock
    ):
        # Friday 07:59:59: too close to 08:00 to be served at all.
        ids = queue(repo, HOME_TO_SCHOOL)
        clock.now = local(7, 59, second=59)
        assert client.get(GET, headers=GOOD).json() == {"requests": []}
        # A result arriving at 08:00:02 (a poll served earlier, a slow call)
        # would have priced Saturday: it is refused and nothing is cached.
        clock.now = local(8, 0, second=2)
        body = self.post(client, self.item(ids[HOME_TO_SCHOOL], 900)).json()
        assert body["results"] == [
            {
                "request_id": ids[HOME_TO_SCHOOL],
                "status": "rejected",
                "reason": "not_due",
            }
        ]
        assert (body["accepted"], body["rejected"]) == (0, 1)
        assert repo.lookup(HOME_TO_SCHOOL, now=clock.now) is None
        assert [r.request_id for r in repo.pending(now=clock.now)] == [
            ids[HOME_TO_SCHOOL]
        ]

    def test_a_sender_claimed_weekday_or_endpoint_is_ignored(self, client, repo):
        ids = queue(repo, HOME_TO_SCHOOL)
        claim = {
            **self.item(ids[HOME_TO_SCHOOL], 700),
            "weekday": "sun",
            "bucket": 5,
            "origin": {"kind": "address", "value": "somewhere else"},
            "destination": "zone.elsewhere",
        }
        assert self.post(client, claim).json()["accepted"] == 1
        assert repo.lookup(HOME_TO_SCHOOL, now=NOW) is not None
        assert repo.lookup(Leg("home", "high-school", 6, 5), now=NOW) is None

    def test_an_unknown_id_is_rejected_for_that_item_only(self, client, repo):
        ids = queue(repo, HOME_TO_SCHOOL)
        response = self.post(
            client, self.item("0" * 32), self.item(ids[HOME_TO_SCHOOL], 600)
        )
        assert response.status_code == 200
        body = response.json()
        assert (body["accepted"], body["rejected"]) == (1, 1)
        assert body["results"][0] == {
            "request_id": "0" * 32,
            "status": "rejected",
            "reason": "unknown_request",
        }
        assert repo.lookup(HOME_TO_SCHOOL, now=NOW) is not None

    def test_an_expired_id_is_rejected(self, client, repo, clock):
        ids = queue(repo, HOME_TO_SCHOOL)
        clock.now = NOW.replace(year=2027)
        body = self.post(client, self.item(ids[HOME_TO_SCHOOL])).json()
        assert body["results"][0]["reason"] == "expired"
        assert repo.lookup(HOME_TO_SCHOOL, now=clock.now) is None

    def test_the_same_duration_replayed_is_a_no_op(self, client, repo):
        ids = queue(repo, HOME_TO_SCHOOL)
        self.post(client, self.item(ids[HOME_TO_SCHOOL], 600))
        body = self.post(client, self.item(ids[HOME_TO_SCHOOL], 600)).json()
        assert (body["accepted"], body["unchanged"], body["rejected"]) == (0, 1, 0)
        assert body["results"][0]["status"] == "unchanged"

    def test_a_different_duration_replayed_is_a_conflict(self, client, repo):
        ids = queue(repo, HOME_TO_SCHOOL)
        self.post(client, self.item(ids[HOME_TO_SCHOOL], 600))
        body = self.post(client, self.item(ids[HOME_TO_SCHOOL], 601)).json()
        assert body["results"][0] == {
            "request_id": ids[HOME_TO_SCHOOL],
            "status": "rejected",
            "reason": "conflict",
        }
        found = repo.lookup(HOME_TO_SCHOOL, now=NOW)
        assert found is not None
        assert found.duration.total_seconds() == 600

    @pytest.mark.parametrize(
        "seconds",
        [
            -1,
            0,
            "600",
            "",
            600.5,
            600.0,
            True,
            None,
            [600],
            MAX_ROUTE_DURATION_SECONDS + 1,
            10**12,
        ],
        ids=[
            "negative",
            "zero",
            "string",
            "empty-string",
            "float",
            "integral-float",
            "bool",
            "null",
            "list",
            "over-12-hours",
            "huge",
        ],
    )
    def test_a_bad_duration_is_a_422_and_nothing_is_stored(
        self, client, repo, engine, seconds
    ):
        ids = queue(repo, HOME_TO_SCHOOL)
        response = self.post(client, self.item(ids[HOME_TO_SCHOOL], seconds))
        assert response.status_code == 422
        assert repo.lookup(HOME_TO_SCHOOL, now=NOW) is None
        assert audit_rows(engine) == []

    def test_the_largest_allowed_duration_is_accepted(self, client, repo):
        ids = queue(repo, HOME_TO_SCHOOL)
        response = self.post(
            client, self.item(ids[HOME_TO_SCHOOL], MAX_ROUTE_DURATION_SECONDS)
        )
        assert response.json()["accepted"] == 1

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"results": []},
            {"results": "x"},
            {"results": [{"duration_seconds": 60}]},
            {"results": [{"request_id": "a"}]},
            {"results": [{"request_id": "", "duration_seconds": 60}]},
            {"results": [{"request_id": "a" * 65, "duration_seconds": 60}]},
            {"results": [{"request_id": 5, "duration_seconds": 60}]},
            {"results": ["a"]},
            [],
        ],
        ids=[
            "no-results",
            "empty-batch",
            "results-not-a-list",
            "no-request-id",
            "no-duration",
            "empty-request-id",
            "long-request-id",
            "numeric-request-id",
            "item-not-an-object",
            "body-is-a-list",
        ],
    )
    def test_a_malformed_body_is_a_422(self, client, body):
        assert client.post(POST, headers=GOOD, json=body).status_code == 422

    def test_an_oversized_batch_is_a_422(self, client):
        items = [self.item(f"{n:032d}", 60) for n in range(MAX_RESULTS_PER_POST + 1)]
        assert self.post(client, *items).status_code == 422

    def test_a_full_batch_is_processed(self, client):
        items = [self.item(f"{n:032d}", 60) for n in range(MAX_RESULTS_PER_POST)]
        body = self.post(client, *items).json()
        assert body["rejected"] == MAX_RESULTS_PER_POST

    def test_a_body_that_is_not_json_is_a_422(self, client):
        response = client.post(POST, headers=GOOD, content=b"not json")
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "content", [b"\xff\xfe", b'{"results": [', b"", b"[" * 60_000]
    )
    def test_other_unreadable_bodies_are_a_422(self, client, content):
        assert client.post(POST, headers=GOOD, content=content).status_code == 422

    def test_a_422_does_not_echo_the_posted_input(self, client):
        marker = "secret-marker-value"
        body = {
            "results": [{"request_id": marker, "duration_seconds": marker}],
            marker: marker,
        }
        response = client.post(POST, headers=GOOD, json=body)
        assert response.status_code == 422
        assert marker not in response.text
        (error, *_) = response.json()["detail"]
        assert set(error) == {"type", "loc", "msg"}
        malformed = client.post(POST, headers=GOOD, content=marker.encode())
        assert malformed.status_code == 422
        assert marker not in malformed.text

    def test_an_oversized_body_is_a_413_and_stores_nothing(self, client, engine):
        content = b" " * (MAX_BODY_BYTES + 1)
        response = client.post(POST, headers=GOOD, content=content)
        assert response.status_code == 413
        assert response.json() == {"detail": "Request body too large"}
        assert audit_rows(engine) == []

    def test_an_oversized_chunked_body_is_a_413(self, client):
        def chunks():
            for _ in range(MAX_BODY_BYTES // 1024 + 2):
                yield b" " * 1024

        # No Content-Length: the byte count of what is actually read decides.
        response = client.post(POST, headers=GOOD, content=chunks())
        assert response.status_code == 413

    def test_a_body_of_exactly_the_cap_is_read(self, client):
        prefix = b'{"results": [{"request_id": "x", "duration_seconds": 60}]}'
        content = prefix + b" " * (MAX_BODY_BYTES - len(prefix))
        assert len(content) == MAX_BODY_BYTES
        response = client.post(POST, headers=GOOD, content=content)
        assert response.status_code == 200
        assert response.json()["rejected"] == 1

    def test_the_body_cap_is_64_kib(self):
        assert MAX_BODY_BYTES == 64 * 1024

    def test_one_audit_row_per_post_with_counts_only(self, client, repo, engine):
        ids = queue(repo, HOME_TO_SCHOOL, HOME_TO_STUDIO)
        self.post(
            client,
            self.item(ids[HOME_TO_SCHOOL], 1234),
            self.item(ids[HOME_TO_STUDIO], 4321),
            self.item("0" * 32),
        )
        self.post(client, self.item(ids[HOME_TO_SCHOOL], 1234))
        rows = audit_rows(engine)
        assert [row.action for row in rows] == [
            "route-results a2 r1 u0",
            "route-results a0 r0 u1",
        ]
        assert {row.object_ref for row in rows} == {"route-results"}
        assert all(row.new_hash and len(row.new_hash) == 64 for row in rows)
        everything = " ".join(str(tuple(row)) for row in rows)
        for secret in (HOME_ADDRESS, STUDIO_ADDRESS, "1234", "4321", "zone."):
            assert secret not in everything

    def test_a_rejected_only_post_is_still_audited(self, client, engine):
        self.post(client, self.item("0" * 32))
        assert [row.action for row in audit_rows(engine)] == ["route-results a0 r1 u0"]

    def test_a_missing_audit_key_stores_nothing_and_is_a_503(
        self, tmp_path, engine, clock, repo
    ):
        unaudited = make_settings(tmp_path, audit_key=None)
        client = build_client(unaudited, engine, clock)
        ids = queue(repo, HOME_TO_SCHOOL)
        response = self.post(client, self.item(ids[HOME_TO_SCHOOL], 600))
        assert response.status_code == 503
        assert repo.lookup(HOME_TO_SCHOOL, now=NOW) is None
        assert [r.request_id for r in repo.pending(now=NOW)] == [ids[HOME_TO_SCHOOL]]

    def test_a_busy_database_is_a_503_with_retry_after_and_stores_nothing(
        self, settings, engine, clock, repo, tmp_path
    ):
        client = build_client(settings, engine, clock, lock_timeout_ms=50)
        ids = queue(repo, HOME_TO_SCHOOL)
        holder = create_sqlite_engine(tmp_path / "fl.db")
        try:
            with holder.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                response = self.post(client, self.item(ids[HOME_TO_SCHOOL], 600))
                connection.rollback()
        finally:
            holder.dispose()
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "5"
        assert repo.lookup(HOME_TO_SCHOOL, now=NOW) is None
        assert audit_rows(engine) == []

    def test_a_non_lock_database_error_is_a_503_without_retry_after(
        self, settings, engine, clock
    ):
        client = build_client(settings, engine, clock)
        make_writes_fail(engine)
        response = self.post(client, self.item("0" * 32, 600))
        assert response.status_code == 503
        assert "Retry-After" not in response.headers
        assert response.json() == {"detail": "Route store unavailable"}

    def test_a_store_error_is_a_503_and_stores_nothing(self, client, repo, engine):
        ids = queue(repo, HOME_TO_SCHOOL)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE route_cache"))
        response = self.post(client, self.item(ids[HOME_TO_SCHOOL], 600))
        assert response.status_code == 503
        assert response.json() == {"detail": "Route store unavailable"}
        assert audit_rows(engine) == []
        assert [r.request_id for r in repo.pending(now=NOW)] == [ids[HOME_TO_SCHOOL]]

    def test_the_response_carries_no_location_text(self, client, repo):
        ids = queue(repo, HOME_TO_STUDIO)
        response = self.post(client, self.item(ids[HOME_TO_STUDIO], 600))
        assert STUDIO_ADDRESS not in response.text
        assert "studio" not in response.text
