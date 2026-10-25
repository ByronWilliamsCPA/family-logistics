"""POST /api/v1/ingest/ha-snapshot through the authenticated HA router."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fl_engine import NOW, default, routes
from fl_factories import CALENDAR
from fl_ha_snapshot import (
    FRIDAY,
    HIGH_BELL,
    as_json,
    bell,
    payload,
    seed_with_zones,
    snapshot_settings,
    weekdays,
)
from fl_service import collect
from sqlalchemy import func, select

from family_logistics.adapters.home_assistant.schemas import MAX_SNAPSHOT_BYTES
from family_logistics.api.app import create_app
from family_logistics.api.ha import MAX_FAILURES, HaAuthThrottle, build_ha_router
from family_logistics.core.config import Settings
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.registry import RegistryStore
from family_logistics.db.schema import audit_log, school_day
from family_logistics.db.school_snapshots import SchoolSnapshotRepository
from family_logistics.domain.events import TransportDirection, TransportMode
from family_logistics.services.assessments import AssessmentService
from family_logistics.services.school_snapshot import SchoolSnapshotIngest

pytestmark = pytest.mark.unit

TOKEN = "t" * 40
GOOD = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
URL = "/api/v1/ingest/ha-snapshot"
PRIVATE = "private_marker_text"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    seed_with_zones(engine)
    yield engine
    engine.dispose()


def build(engine, audit_key, *, clock=lambda: NOW, **settings) -> FastAPI:
    values = snapshot_settings(audit_key, ha_token=TOKEN, **settings)
    ingest = SchoolSnapshotIngest(engine, values, clock=clock)
    router = build_ha_router(
        values, throttle=HaAuthThrottle(clock=Clock()), school_ingest=ingest
    )
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(engine, audit_key) -> TestClient:
    return TestClient(build(engine, audit_key), client=("192.0.2.1", 50000))


def post(client: TestClient, body: object, headers=None):
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return client.post(URL, content=content, headers=headers or GOOD)


def count(engine, table) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


class TestAccepted:
    def test_a_snapshot_is_stored_and_acknowledged(self, client, engine):
        response = post(client, payload())
        assert response.status_code == 200
        assert response.json() == {
            "status": "stored",
            "schools": 2,
            "days": 3,
            "fetched_at": (NOW - timedelta(minutes=1)).isoformat(),
            "received_at": NOW.isoformat(),
        }
        assert count(engine, school_day) == 3
        assert count(engine, audit_log) == 1

    def test_an_identical_repost_is_unchanged(self, client, engine):
        first = post(client, payload()).json()
        second = post(client, payload()).json()
        assert second == first | {"status": "unchanged"}
        assert count(engine, school_day) == 3
        assert count(engine, audit_log) == 2

    def test_json_with_a_charset_is_accepted(self, client):
        headers = GOOD | {"Content-Type": "application/json; charset=utf-8"}
        assert post(client, payload(), headers).status_code == 200

    def test_an_older_snapshot_is_a_conflict(self, client, engine):
        post(client, payload())
        older = payload(
            fetched_at=(NOW - timedelta(hours=2)).isoformat(),
            middle=[bell(FRIDAY, end="12:00", summary="Early Release")],
        )
        response = post(client, older)
        assert response.status_code == 409
        assert count(engine, audit_log) == 1


class TestAuthentication:
    @pytest.mark.parametrize(
        "headers",
        [
            {"Content-Type": "application/json"},
            {"Authorization": "Bearer " + "w" * 40, "Content-Type": "application/json"},
        ],
        ids=["missing", "wrong"],
    )
    def test_without_the_token_nothing_is_read_or_stored(self, client, engine, headers):
        response = post(client, payload(), headers)
        assert response.status_code == 401
        assert count(engine, school_day) == 0

    def test_authentication_comes_before_the_size_limit(self, client):
        huge = b"x" * (MAX_SNAPSHOT_BYTES + 1)
        response = post(client, huge, {"Content-Type": "application/json"})
        assert response.status_code == 401

    def test_failures_are_throttled(self, client):
        wrong = {"Authorization": "Bearer " + "w" * 40}
        for _ in range(MAX_FAILURES):
            assert post(client, payload(), wrong).status_code == 401
        response = post(client, payload())
        assert response.status_code == 429
        assert "Retry-After" in response.headers


class TestSize:
    def test_declared_oversize_is_413(self, client, engine):
        response = post(client, b" " * (MAX_SNAPSHOT_BYTES + 1))
        assert response.status_code == 413
        assert count(engine, school_day) == 0

    def test_streamed_oversize_is_413(self, client):
        def chunks():
            for _ in range(MAX_SNAPSHOT_BYTES // 65536 + 2):
                yield b" " * 65536

        response = client.post(URL, content=chunks(), headers=GOOD)
        assert response.status_code == 413

    @pytest.mark.parametrize("declared", ["lots", b"\xb2", "-1"])
    def test_invalid_content_length_is_400(self, client, declared):
        headers = GOOD | {"Content-Length": declared}
        response = client.post(URL, content=b"{}", headers=headers)
        assert response.status_code == 400


class TestMalformed:
    @pytest.mark.parametrize(
        "body",
        [
            b"{not json " + PRIVATE.encode(),
            b'["' + PRIVATE.encode() + b'"]',
            as_json(payload(**{PRIVATE: 1})),
            as_json(payload(zones=[f"zone.{PRIVATE}", "Zone." + PRIVATE])),
            as_json(payload(middle=[{"start": PRIVATE, "end": "x", "summary": "x"}])),
            b"",
        ],
        ids=[
            "not-json",
            "not-an-object",
            "extra-key",
            "bad-zone",
            "bad-event",
            "empty",
        ],
    )
    def test_422_never_echoes_the_payload(self, client, engine, caplog, body):
        with caplog.at_level("DEBUG"):
            response = post(client, body)
        assert response.status_code == 422
        assert PRIVATE not in response.text
        assert PRIVATE not in caplog.text
        assert response.json()["detail"] == "Invalid snapshot"
        assert count(engine, school_day) == 0

    def test_errors_carry_redacted_locations(self, client):
        body = payload(middle=[{"start": PRIVATE, "end": "x", "summary": "x"}])
        errors = post(client, body).json()["errors"]
        assert {
            "loc": ["calendars", "*", "events", 0, "start"],
            "type": "value_error",
        } in (errors)

    def test_wrong_content_type_is_415(self, client):
        headers = GOOD | {"Content-Type": "text/plain"}
        assert post(client, payload(), headers).status_code == 415


class TestTimestamps:
    def test_naive_fetched_at_is_422(self, client):
        response = post(client, payload(fetched_at="2026-10-23T06:59:00"))
        assert response.status_code == 422
        assert response.json()["errors"] == [
            {"loc": ["fetched_at"], "type": "value_error"}
        ]

    def test_future_fetched_at_is_422(self, client, engine):
        ahead = (NOW + timedelta(minutes=6)).isoformat()
        response = post(client, payload(fetched_at=ahead))
        assert response.status_code == 422
        assert response.json() == {
            "detail": "Invalid snapshot",
            "code": "FETCHED_AT_IN_FUTURE",
        }
        assert count(engine, school_day) == 0


class TestUnknownReferences:
    def test_unknown_entity_is_422_and_not_echoed(self, client, engine, caplog):
        body = payload()
        calendars = body["calendars"]
        assert isinstance(calendars, dict)
        calendars[f"calendar.{PRIVATE}"] = {"events": []}
        with caplog.at_level("DEBUG"):
            response = post(client, body)
        assert (response.status_code, response.json()["code"]) == (
            422,
            "UNKNOWN_CALENDAR",
        )
        assert PRIVATE not in response.text
        assert PRIVATE not in caplog.text
        assert count(engine, school_day) == 0

    def test_missing_configured_entity_is_422(self, client):
        body = payload()
        calendars = body["calendars"]
        assert isinstance(calendars, dict)
        del calendars[HIGH_BELL]
        response = post(client, body)
        assert (response.status_code, response.json()["code"]) == (
            422,
            "MISSING_CALENDAR",
        )

    def test_unknown_zone_rolls_back_and_keeps_the_previous_snapshot(
        self, client, engine, caplog
    ):
        assert post(client, payload()).status_code == 200
        newer = payload(
            fetched_at=NOW.isoformat(),
            zones=["zone.home", f"zone.{PRIVATE}"],
            middle=[bell(FRIDAY, end="12:00", summary="Early Release")],
        )
        with caplog.at_level("DEBUG"):
            response = post(client, newer)
        assert (response.status_code, response.json()["code"]) == (422, "UNKNOWN_ZONE")
        assert PRIVATE not in response.text
        assert PRIVATE not in caplog.text
        latest = SchoolSnapshotRepository(engine).latest()
        assert latest is not None
        assert latest.received_at == NOW - timedelta(minutes=1)
        assert count(engine, school_day) == 3
        assert count(engine, audit_log) == 1


class TestUnavailable:
    def test_no_configured_school_is_503(self, engine, audit_key):
        app = build(engine, audit_key, ha_schools=[])
        client = TestClient(app, client=("192.0.2.1", 50000))
        assert post(client, payload()).status_code == 503

    def test_no_audit_key_is_503(self, engine):
        app = build(engine, None)
        client = TestClient(app, client=("192.0.2.1", 50000))
        assert post(client, payload()).status_code == 503
        assert count(engine, school_day) == 0

    def test_a_router_without_the_service_answers_503(self):
        settings = Settings.model_validate({"ha_token": TOKEN})
        app = FastAPI()
        app.include_router(build_ha_router(settings))
        client = TestClient(app, client=("192.0.2.1", 50000))
        assert post(client, payload()).status_code == 503

    def test_create_app_mounts_the_route(self, tmp_path):
        settings = Settings.model_validate(
            {"ha_token": TOKEN, "database_path": tmp_path / "fl.db"}
        )
        client = TestClient(create_app(settings), client=("192.0.2.1", 50000))
        assert (
            post(client, payload(), {"Content-Type": "application/json"}).status_code
            == 401
        )
        assert post(client, payload()).status_code == 503


class TestEndToEnd:
    def test_an_ingested_snapshot_clears_the_missing_schedule(
        self, client, engine, audit_key
    ):
        with engine.begin() as connection:
            RegistryStore(connection).replace_transport_defaults(
                "sam",
                [
                    default_to_school("sam"),
                ],
            )
        collect(engine, CALENDAR, (), at_time=NOW - timedelta(minutes=20))
        settings = snapshot_settings(audit_key, caldav_calendars="")
        service = AssessmentService(engine, settings, routes(), clock=lambda: NOW)

        before = service.run(school=SchoolSnapshotRepository(engine).latest())
        assert school_009_days(before) == {
            f"day:{day.isoformat()}" for day in weekdays(FRIDAY, 15)
        }

        middle = [bell(day) for day in weekdays(FRIDAY, 17)]
        assert post(client, payload(middle=middle, high=[])).status_code == 200
        latest = SchoolSnapshotRepository(engine).latest()
        after = service.run(school=latest)
        assert school_009_days(after) == set()
        assert not [
            t
            for t in after.result.findings
            if t.finding.rule_id == "DATA-011"
            and "ha_snapshot" in "".join(t.finding.entity_keys)
        ]


def default_to_school(person_id: str):
    return default(
        person_id,
        TransportDirection.TO_SCHOOL,
        TransportMode.BIKE,
        school_id="middle-school",
    )


def school_009_days(outcome) -> set[str]:
    return {
        key
        for tracked in outcome.result.findings
        if tracked.finding.rule_id == "SCHOOL-009"
        for key in tracked.finding.entity_keys
        if key.startswith("day:")
    }
