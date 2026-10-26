"""POST /api/v1/ingest/ha-snapshot through the authenticated HA router."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fl_engine import NOW, default, routes
from fl_factories import CALENDAR
from fl_ha_snapshot import (
    DISTRICT,
    FRIDAY,
    HIGH_BELL,
    MIDDLE_BELL,
    as_json,
    bell,
    payload,
    seed_with_zones,
    snapshot_settings,
    weekdays,
)
from fl_service import collect
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from family_logistics.adapters.home_assistant.schemas import MAX_SNAPSHOT_BYTES
from family_logistics.adapters.home_assistant.school_days import SnapshotMappingError
from family_logistics.api import ha as ha_module
from family_logistics.api.app import create_app
from family_logistics.api.ha import (
    DUPLICATE_JSON_KEY,
    MAX_FAILURES,
    HaAuthThrottle,
    build_ha_router,
)
from family_logistics.core.config import Settings
from family_logistics.core.database import create_sqlite_engine
from family_logistics.core.exceptions import DatabaseError, ValidationError
from family_logistics.db import migrations_api
from family_logistics.db.audit import AuditRepository
from family_logistics.db.registry import RegistryStore
from family_logistics.db.schema import audit_log, school_day
from family_logistics.db.school_snapshots import (
    SNAPSHOT_RETRY_AFTER_SECONDS,
    SchoolSnapshotBusyError,
    SchoolSnapshotRepository,
)
from family_logistics.domain.events import TransportDirection, TransportMode
from family_logistics.middleware.correlation import CorrelationMiddleware
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
    app.add_middleware(CorrelationMiddleware)
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
            "schools_without_days": [],
            "fetched_at": (NOW - timedelta(minutes=1)).isoformat(),
            "received_at": NOW.isoformat(),
        }
        assert count(engine, school_day) == 3
        assert count(engine, audit_log) == 1

    def test_a_school_without_days_is_stored_and_named_in_the_ack(self, client, engine):
        response = post(client, payload(middle=[]))
        assert response.status_code == 200
        assert response.json()["schools_without_days"] == ["middle-school"]
        assert response.json()["status"] == "stored"
        assert count(engine, school_day) == 2

    def test_the_correlation_id_reaches_the_audit_row(self, client, engine, audit_key):
        headers = GOOD | {"X-Correlation-ID": "corr-0123456789"}
        assert post(client, payload(), headers).status_code == 200
        [entry] = AuditRepository(engine, key=audit_key).entries_for("school_snapshot")
        assert entry.correlation_id == "corr-0123456789"

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
        assert response.json() == {
            "detail": "The snapshot is not newer than the stored one"
        }
        latest = SchoolSnapshotRepository(engine).latest()
        assert latest is not None
        assert latest.received_at == NOW - timedelta(minutes=1)
        assert count(engine, school_day) == 3
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

    def test_the_body_is_not_read_before_authentication(self, engine, audit_key):
        app = build(engine, audit_key)
        sent: list[dict[str, object]] = []

        async def receive():
            msg = "the body was read before the token was checked"
            raise AssertionError(msg)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": URL,
            "raw_path": URL.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("192.0.2.1", 50000),
            "server": ("testserver", 80),
        }
        asyncio.run(app(scope, receive, send))
        assert sent[0]["status"] == 401

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


class FakeRequest:
    """Just enough of a request for ``_read_bounded_body``."""

    def __init__(self, declared: str | None, chunks: list[bytes]) -> None:
        self.headers = {} if declared is None else {"content-length": declared}
        self._chunks = chunks
        self.streamed = 0

    async def stream(self):
        for chunk in self._chunks:
            self.streamed += 1
            yield chunk


class TestSize:
    def test_declared_oversize_is_413(self, client, engine):
        response = post(client, b" " * (MAX_SNAPSHOT_BYTES + 1))
        assert response.status_code == 413
        assert response.json() == {"detail": "Payload too large"}
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0

    def test_a_declared_oversize_is_refused_before_a_byte_is_read(self):
        request = FakeRequest(str(MAX_SNAPSHOT_BYTES + 1), [b"x"])
        with pytest.raises(ha_module._BodyTooLargeError):
            asyncio.run(ha_module._read_bounded_body(request, MAX_SNAPSHOT_BYTES))
        assert request.streamed == 0

    def test_the_buffer_never_holds_more_than_the_limit(self):
        request = FakeRequest(None, [b"x" * 6, b"y" * 6])
        with pytest.raises(ha_module._BodyTooLargeError):
            asyncio.run(ha_module._read_bounded_body(request, 10))
        assert request.streamed == 2

    def test_a_body_exactly_at_the_limit_is_read(self):
        request = FakeRequest("10", [b"x" * 6, b"y" * 4])
        body = asyncio.run(ha_module._read_bounded_body(request, 10))
        assert body == b"x" * 6 + b"y" * 4

    def test_streamed_oversize_is_413(self, client, engine):
        def chunks():
            for _ in range(MAX_SNAPSHOT_BYTES // 65536 + 2):
                yield b" " * 65536

        response = client.post(URL, content=chunks(), headers=GOOD)
        assert response.status_code == 413
        assert response.json() == {"detail": "Payload too large"}
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0

    def test_an_endless_content_length_is_413_not_a_server_error(self, client):
        headers = GOOD | {"Content-Length": "9" * 5000}
        response = client.post(URL, content=b"{}", headers=headers)
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
            b"\xff\xfe" + PRIVATE.encode(),
            b"[" * 500_000,
            b'{"fetched_at": 1e999, "x": "' + PRIVATE.encode() + b'"',
        ],
        ids=[
            "not-json",
            "not-an-object",
            "extra-key",
            "bad-zone",
            "bad-event",
            "empty",
            "not-utf8",
            "too-deep",
            "truncated",
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

    @pytest.mark.parametrize(
        "content_type",
        [
            "text/plain",
            "text/json",
            "application/jsonx",
            "application/json-patch+json",
            None,
        ],
    )
    def test_wrong_content_type_is_415(self, client, engine, content_type):
        headers = {"Authorization": GOOD["Authorization"]}
        if content_type is not None:
            headers["Content-Type"] = content_type
        response = post(client, payload(), headers)
        assert response.status_code == 415
        assert response.json() == {"detail": "Expected application/json"}
        assert count(engine, school_day) == 0

    def test_content_type_is_case_insensitive(self, client):
        headers = GOOD | {"Content-Type": "Application/JSON"}
        assert post(client, payload(), headers).status_code == 200

    def test_the_content_type_is_checked_before_the_size(self, client):
        headers = GOOD | {"Content-Type": "text/plain"}
        huge = b"x" * (MAX_SNAPSHOT_BYTES + 1)
        assert post(client, huge, headers).status_code == 415


class TestDuplicateKeys:
    def duplicated(self, where: str) -> bytes:
        text = as_json(payload())
        if where == "top":
            return b'{"fetched_at": "2000-01-01T00:00:00+00:00", ' + text[1:]
        if where == "event":
            return text.replace(
                b'"summary": "Regular Day"',
                b'"summary": "Regular Day", "summary": "Early Release"',
                1,
            )
        entity = f'"{MIDDLE_BELL}": '.encode()
        return text.replace(entity, entity + b'{"events": []}, ' + entity, 1)

    @pytest.mark.parametrize("where", ["top", "event", "calendar"])
    def test_a_repeated_key_is_422_and_stores_nothing(
        self, client, engine, caplog, where
    ):
        with caplog.at_level("DEBUG"):
            response = post(client, self.duplicated(where))
        assert response.status_code == 422
        assert response.json() == {
            "detail": "Invalid snapshot",
            "code": DUPLICATE_JSON_KEY,
        }
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0

    def test_a_repeated_key_is_not_echoed(self, client, caplog):
        body = b'{"' + PRIVATE.encode() + b'": 1, "' + PRIVATE.encode() + b'": 2}'
        with caplog.at_level("DEBUG"):
            response = post(client, body)
        assert response.json()["code"] == DUPLICATE_JSON_KEY
        assert PRIVATE not in response.text
        assert PRIVATE not in caplog.text

    def test_the_same_key_in_sibling_objects_is_fine(self, client):
        assert post(client, as_json(payload())).status_code == 200


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


class TestPrecedence:
    def test_an_older_snapshot_with_an_unknown_zone_is_422_not_409(
        self, client, engine
    ):
        assert post(client, payload()).status_code == 200
        older = payload(
            fetched_at=(NOW - timedelta(hours=2)).isoformat(),
            zones=["zone.home", f"zone.{PRIVATE}"],
            middle=[bell(FRIDAY, end="12:00", summary="Early Release")],
        )
        response = post(client, older)
        assert (response.status_code, response.json()["code"]) == (422, "UNKNOWN_ZONE")
        assert count(engine, audit_log) == 1

    def test_an_empty_snapshot_is_422_and_keeps_the_previous_one(self, client, engine):
        assert post(client, payload()).status_code == 200
        empty = payload(fetched_at=NOW.isoformat(), middle=[], high=[], district=[])
        response = post(client, empty)
        assert (response.status_code, response.json()["code"]) == (
            422,
            "EMPTY_SNAPSHOT",
        )
        latest = SchoolSnapshotRepository(engine).latest()
        assert latest is not None
        assert latest.received_at == NOW - timedelta(minutes=1)
        assert count(engine, school_day) == 3


class TestBusy:
    def test_a_busy_error_is_503_with_retry_after(self, engine, audit_key, monkeypatch):
        def busy(self, snapshot, *, correlation_id=None):
            msg = "the database is locked by another writer; nothing stored"
            raise SchoolSnapshotBusyError(msg, error_code="SNAPSHOT_BUSY")

        monkeypatch.setattr(SchoolSnapshotIngest, "ingest", busy)
        client = TestClient(build(engine, audit_key), client=("192.0.2.1", 50000))
        response = post(client, payload())
        assert response.status_code == 503
        assert response.headers["Retry-After"] == str(SNAPSHOT_RETRY_AFTER_SECONDS)
        assert response.json() == {"detail": "School snapshot store is busy"}

    def test_a_real_lock_held_by_another_writer_is_503(self, engine, audit_key):
        values = snapshot_settings(audit_key, ha_token=TOKEN)
        ingest = SchoolSnapshotIngest(engine, values, clock=lambda: NOW)
        ingest.repository._lock_timeout_ms = 50
        router = build_ha_router(
            values, throttle=HaAuthThrottle(clock=Clock()), school_ingest=ingest
        )
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, client=("192.0.2.1", 50000))
        with engine.connect() as holder:
            holder.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                response = post(client, payload())
            finally:
                holder.rollback()
        assert response.status_code == 503
        assert response.headers["Retry-After"] == str(SNAPSHOT_RETRY_AFTER_SECONDS)
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0


UNAVAILABLE = {"detail": "School snapshot ingest is not available"}


class TestUnavailable:
    @pytest.mark.parametrize(
        ("reason", "settings", "keyed"),
        [
            ("NO_SCHOOLS", {"ha_schools": []}, True),
            ("NO_AUDIT_KEY", {}, False),
            (
                "REGISTRY_MISMATCH",
                {
                    "ha_schools": [
                        {
                            "location_id": f"loc_{PRIVATE}",
                            "person_ids": ["sam"],
                            "bell_calendars": [MIDDLE_BELL],
                            "district_calendars": [],
                        }
                    ]
                },
                True,
            ),
        ],
    )
    def test_a_configuration_fault_is_503_without_a_leak(
        self, engine, audit_key, caplog, reason, settings, keyed
    ):
        app = build(engine, audit_key if keyed else None, **settings)
        client = TestClient(app, client=("192.0.2.1", 50000))
        body = payload()
        calendars = body["calendars"]
        assert isinstance(calendars, dict)
        for entity in (HIGH_BELL, DISTRICT):
            del calendars[entity]
        with caplog.at_level("DEBUG"):
            response = post(client, body)
        assert response.status_code == 503
        assert response.json() == UNAVAILABLE
        assert "Retry-After" not in response.headers
        assert PRIVATE not in response.text
        assert PRIVATE not in caplog.text
        assert reason in caplog.text
        assert count(engine, school_day) == 0

    def test_a_router_without_the_service_answers_503(self):
        settings = Settings.model_validate({"ha_token": TOKEN})
        app = FastAPI()
        app.include_router(build_ha_router(settings))
        client = TestClient(app, client=("192.0.2.1", 50000))
        response = post(client, payload())
        assert response.status_code == 503
        assert response.json() == UNAVAILABLE

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


def raising(exc: BaseException):
    def ingest(self, snapshot, *, correlation_id=None):
        raise exc

    return ingest


def driver_error(code: int | None = None) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(PRIVATE)
    if code is not None:
        error.sqlite_errorcode = code
    return error


FAILED = {"detail": "School snapshot ingest failed"}


class TestStorageFaults:
    @pytest.mark.parametrize(
        "fault",
        [
            OperationalError(PRIVATE, None, driver_error(sqlite3.SQLITE_IOERR)),
            OperationalError(PRIVATE, None, driver_error(sqlite3.SQLITE_READONLY)),
            OperationalError(PRIVATE, None, driver_error()),
            IntegrityError(PRIVATE, None, sqlite3.IntegrityError(PRIVATE)),
            DatabaseError(PRIVATE),
            ValidationError(PRIVATE, field="action"),
        ],
        ids=["ioerr", "readonly", "no-code", "integrity", "project", "internal-422"],
    )
    def test_a_non_lock_fault_is_500_never_busy(
        self, engine, audit_key, monkeypatch, caplog, fault
    ):
        monkeypatch.setattr(SchoolSnapshotIngest, "ingest", raising(fault))
        client = TestClient(build(engine, audit_key), client=("192.0.2.1", 50000))
        with caplog.at_level("DEBUG"):
            response = post(client, payload())
        assert response.status_code == 500
        assert response.json() == FAILED
        assert "Retry-After" not in response.headers
        assert PRIVATE not in response.text
        assert PRIVATE not in caplog.text
        assert "ha_snapshot.failed" in caplog.text
        assert type(fault).__name__ in caplog.text
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (sqlite3.SQLITE_IOERR, 500),
            (sqlite3.SQLITE_READONLY, 500),
            (sqlite3.SQLITE_BUSY, 503),
            (sqlite3.SQLITE_LOCKED, 503),
        ],
    )
    def test_only_lock_contention_taking_the_write_lock_is_busy(
        self, engine, audit_key, code, expected
    ):
        def fail(conn, cursor, statement, parameters, context, executemany):
            if statement == "BEGIN IMMEDIATE":
                raise OperationalError(statement, None, driver_error(code))

        event.listen(engine, "before_cursor_execute", fail)
        client = TestClient(build(engine, audit_key), client=("192.0.2.1", 50000))
        response = post(client, payload())
        assert response.status_code == expected
        assert ("Retry-After" in response.headers) == (expected == 503)
        assert count(engine, school_day) == 0

    def test_an_unexpected_error_leaks_nothing(
        self, engine, audit_key, monkeypatch, caplog
    ):
        fault = RuntimeError(PRIVATE)
        monkeypatch.setattr(SchoolSnapshotIngest, "ingest", raising(fault))
        app = build(engine, audit_key)
        client = TestClient(
            app, client=("192.0.2.1", 50000), raise_server_exceptions=False
        )
        with caplog.at_level("DEBUG"):
            response = post(client, payload())
        assert response.status_code == 500
        assert PRIVATE not in response.text
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0


class TestInvalidTime:
    # The schema bounds every payload year to 2000-2100, so INVALID_TIME cannot
    # be provoked by a payload; these pin the route's mapping of the two places
    # that still raise it (the mapper and the store) as defense in depth.
    @pytest.mark.parametrize(
        "fault",
        [
            SnapshotMappingError(PRIVATE, error_code="INVALID_TIME"),
            ValidationError(PRIVATE, field="days", error_code="INVALID_TIME"),
        ],
        ids=["mapper", "store"],
    )
    def test_an_unrepresentable_time_is_422(
        self, engine, audit_key, monkeypatch, caplog, fault
    ):
        monkeypatch.setattr(SchoolSnapshotIngest, "ingest", raising(fault))
        client = TestClient(build(engine, audit_key), client=("192.0.2.1", 50000))
        with caplog.at_level("DEBUG"):
            response = post(client, payload())
        assert response.status_code == 422
        assert response.json() == {
            "detail": "Invalid snapshot",
            "code": "INVALID_TIME",
        }
        assert PRIVATE not in response.text
        assert PRIVATE not in caplog.text


class TestThreadpool:
    def test_the_ingest_runs_off_the_event_loop_thread(
        self, engine, audit_key, monkeypatch
    ):
        seen: dict[str, int] = {}
        original = ha_module._read_bounded_body

        async def recording(request, limit):
            seen["loop"] = threading.get_ident()
            return await original(request, limit)

        def ingest(self, snapshot, *, correlation_id=None):
            seen["ingest"] = threading.get_ident()
            raise SchoolSnapshotBusyError("x", error_code="SNAPSHOT_BUSY")

        monkeypatch.setattr(ha_module, "_read_bounded_body", recording)
        monkeypatch.setattr(SchoolSnapshotIngest, "ingest", ingest)
        client = TestClient(build(engine, audit_key), client=("192.0.2.1", 50000))
        assert post(client, payload()).status_code == 503
        assert seen["ingest"] != seen["loop"]


class TestOpenApi:
    def operation(self, engine, audit_key) -> dict:
        document = build(engine, audit_key).openapi()
        return document["paths"][URL]["post"]

    def test_the_request_body_is_documented(self, engine, audit_key):
        body = self.operation(engine, audit_key)["requestBody"]
        schema = body["content"]["application/json"]["schema"]
        assert body["required"] is True
        assert schema["title"] == "HaSnapshotPayload"
        assert set(schema["required"]) == {"fetched_at", "calendars"}
        assert "$ref" not in json.dumps(schema)
        assert "$defs" not in schema

    def test_every_status_is_documented(self, engine, audit_key):
        responses = self.operation(engine, audit_key)["responses"]
        assert set(responses) == {
            "200", "400", "401", "409", "413", "415", "422", "429", "500", "503",
        }  # fmt: skip
        ok = responses["200"]["content"]["application/json"]["schema"]
        assert ok["$ref"].endswith("/IngestAckBody")
        error = responses["422"]["content"]["application/json"]["schema"]
        assert error["$ref"].endswith("/ErrorBody")
        assert "Retry-After" in responses["503"]["headers"]

    def test_the_route_takes_no_fastapi_body_parameter(self, engine, audit_key):
        # A FastAPI body would be parsed before the size limit and the
        # duplicate key check, so the schema must come from openapi_extra.
        operation = self.operation(engine, audit_key)
        assert [item["name"] for item in operation.get("parameters", [])] == [
            "authorization"
        ]


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
