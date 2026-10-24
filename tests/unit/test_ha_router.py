"""Router-level Home Assistant authentication and failure throttle."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from family_logistics.api import app as app_module
from family_logistics.api.app import create_app
from family_logistics.api.ha import (
    FAILURE_WINDOW_SECONDS,
    MAX_FAILURES,
    HaAuthThrottle,
    build_ha_router,
)
from family_logistics.core.config import Settings

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "t" * 40
GOOD = {"Authorization": f"Bearer {TOKEN}"}
UNAUTHORIZED = {"detail": "Unauthorized"}


class Clock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build_app(
    ha_token: str | None = TOKEN, clock: Clock | None = None
) -> tuple[FastAPI, HaAuthThrottle, Clock]:
    clock = clock or Clock()
    settings = Settings.model_validate({"ha_token": ha_token})
    throttle = HaAuthThrottle(clock=clock)
    router = build_ha_router(settings, throttle=throttle)

    @router.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    return app, throttle, clock


def client_for(app: FastAPI, host: str = "192.0.2.1") -> TestClient:
    return TestClient(app, client=(host, 50000))


@pytest.mark.unit
class TestAuthentication:
    def test_valid_token_passes(self):
        app, _, _ = build_app()
        response = client_for(app).get("/api/v1/ping", headers=GOOD)
        assert (response.status_code, response.json()) == (200, {"ok": True})

    def test_router_shape(self):
        settings = Settings.model_validate({"ha_token": TOKEN})
        router = build_ha_router(settings)
        assert router.prefix == "/api/v1"
        assert router.tags == ["home-assistant"]
        assert len(router.dependencies) == 1

    @pytest.mark.parametrize(
        ("headers", "ha_token"),
        [
            ({}, TOKEN),
            ({"Authorization": f"Basic {TOKEN}"}, TOKEN),
            ({"Authorization": f"Bearer {'x' * 40}"}, TOKEN),
            ({"Authorization": "Bearer "}, TOKEN),
            (GOOD, None),
            ({}, None),
        ],
        ids=[
            "missing",
            "wrong-scheme",
            "wrong-token",
            "empty-token",
            "unset-config",
            "unset-config-no-header",
        ],
    )
    def test_every_failure_is_the_same_401(self, headers, ha_token):
        app, _, _ = build_app(ha_token=ha_token)
        response = client_for(app).get("/api/v1/ping", headers=headers)
        assert response.status_code == 401
        assert response.json() == UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert "Retry-After" not in response.headers


@pytest.mark.unit
class TestThrottle:
    def test_lockout_after_max_failures_even_with_correct_token(self):
        app, _, _ = build_app()
        client = client_for(app)
        for _ in range(MAX_FAILURES):
            assert client.get("/api/v1/ping").status_code == 401
        response = client.get("/api/v1/ping", headers=GOOD)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == str(int(FAILURE_WINDOW_SECONDS))
        assert TOKEN not in response.text

    def test_below_threshold_is_not_locked(self):
        app, _, _ = build_app()
        client = client_for(app)
        for _ in range(MAX_FAILURES - 1):
            assert client.get("/api/v1/ping").status_code == 401
        assert client.get("/api/v1/ping", headers=GOOD).status_code == 200

    def test_retry_after_counts_down_and_lock_expires(self):
        app, _, clock = build_app()
        client = client_for(app)
        for _ in range(MAX_FAILURES):
            client.get("/api/v1/ping")
        clock.now += 20.5
        response = client.get("/api/v1/ping", headers=GOOD)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "40"
        clock.now += FAILURE_WINDOW_SECONDS
        assert client.get("/api/v1/ping", headers=GOOD).status_code == 200

    def test_old_failures_age_out_of_the_window(self):
        app, _, clock = build_app()
        client = client_for(app)
        for _ in range(MAX_FAILURES - 1):
            client.get("/api/v1/ping")
        clock.now += FAILURE_WINDOW_SECONDS
        for _ in range(MAX_FAILURES - 1):
            assert client.get("/api/v1/ping").status_code == 401
        assert client.get("/api/v1/ping", headers=GOOD).status_code == 200

    def test_locked_client_does_not_affect_another(self):
        app, _, _ = build_app()
        attacker = client_for(app, "192.0.2.1")
        friend = client_for(app, "192.0.2.2")
        for _ in range(MAX_FAILURES):
            attacker.get("/api/v1/ping")
        assert attacker.get("/api/v1/ping", headers=GOOD).status_code == 429
        assert friend.get("/api/v1/ping", headers=GOOD).status_code == 200

    def test_success_does_not_reset_failures(self):
        app, _, _ = build_app()
        attacker = client_for(app, "192.0.2.1")
        friend = client_for(app, "192.0.2.2")
        for _ in range(MAX_FAILURES - 1):
            attacker.get("/api/v1/ping")
        assert friend.get("/api/v1/ping", headers=GOOD).status_code == 200
        attacker.get("/api/v1/ping")
        assert attacker.get("/api/v1/ping", headers=GOOD).status_code == 429

    def test_missing_client_address_shares_the_unknown_bucket(self):
        app, throttle, _ = build_app()

        async def run() -> list[int]:
            transport = httpx.ASGITransport(app=app, client=None)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                codes = [
                    (await client.get("/api/v1/ping")).status_code
                    for _ in range(MAX_FAILURES)
                ]
                codes.append(
                    (await client.get("/api/v1/ping", headers=GOOD)).status_code
                )
                return codes

        assert asyncio.run(run()) == [401] * MAX_FAILURES + [429]
        assert throttle.retry_after("unknown") > 0


@pytest.mark.unit
class TestThrottleBounds:
    def test_expired_entries_are_pruned(self):
        clock = Clock()
        throttle = HaAuthThrottle(clock=clock)
        for index in range(10):
            throttle.record_failure(f"198.51.100.{index}")
        clock.now += FAILURE_WINDOW_SECONDS + 1
        assert throttle.retry_after("198.51.100.0") == 0
        assert len(throttle._clients) == 0

    def test_tracked_clients_are_capped_evicting_the_oldest(self):
        throttle = HaAuthThrottle(max_clients=3, clock=Clock())
        for index in range(5):
            throttle.record_failure(f"203.0.113.{index}")
        assert list(throttle._clients) == ["203.0.113.2", "203.0.113.3", "203.0.113.4"]

    def test_concurrent_failures_are_all_counted(self):
        throttle = HaAuthThrottle(max_failures=1000, clock=Clock())
        workers = [
            threading.Thread(
                target=lambda: [
                    throttle.record_failure("192.0.2.99") for _ in range(100)
                ]
            )
            for _ in range(8)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        state = throttle._clients["192.0.2.99"]
        assert len(state.failures) == 800


@pytest.mark.unit
class TestRouterLevelGuarantee:
    def test_route_added_after_creation_still_requires_auth(self):
        settings = Settings.model_validate({"ha_token": TOKEN})
        router = build_ha_router(settings, throttle=HaAuthThrottle(clock=Clock()))

        @router.post("/later")
        def later() -> dict[str, str]:
            return {"reached": "handler"}

        app = FastAPI()
        app.include_router(router)
        client = client_for(app)
        denied = client.post("/api/v1/later")
        assert (denied.status_code, denied.json()) == (401, UNAUTHORIZED)
        assert client.post("/api/v1/later", headers=GOOD).json() == {
            "reached": "handler"
        }


@pytest.mark.unit
class TestAppWiring:
    def test_healthz_stays_unauthenticated_with_ha_router_mounted(self, tmp_path: Path):
        app = create_app(
            Settings.model_validate(
                {"ha_token": TOKEN, "database_path": tmp_path / "fl.db"}
            )
        )
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200

    def test_create_app_mounts_the_ha_router(self, tmp_path: Path, monkeypatch):
        mounted: list[Settings] = []
        real = app_module.build_ha_router

        def spy(settings, **kwargs):
            mounted.append(settings)
            return real(settings, **kwargs)

        monkeypatch.setattr(app_module, "build_ha_router", spy)
        settings = Settings.model_validate(
            {"ha_token": TOKEN, "database_path": tmp_path / "fl.db"}
        )
        create_app(settings)
        assert mounted == [settings]


@pytest.mark.unit
class TestLogging:
    def test_secrets_never_reach_the_logs(self, caplog):
        app, _, _ = build_app()
        client = client_for(app)
        wrong = "w" * 40
        with caplog.at_level("INFO"):
            for _ in range(MAX_FAILURES):
                client.get("/api/v1/ping", headers={"Authorization": f"Bearer {wrong}"})
            client.get("/api/v1/ping", headers=GOOD)
        assert "ha_auth.failed" in caplog.text
        assert "ha_auth.locked_out" in caplog.text
        assert "192.0.2.1" in caplog.text
        assert TOKEN not in caplog.text
        assert wrong not in caplog.text
        assert "Bearer" not in caplog.text
