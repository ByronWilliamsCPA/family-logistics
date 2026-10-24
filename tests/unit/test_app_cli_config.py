"""Settings, health endpoint and CLI."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import ValidationError

from family_logistics.api.app import create_app
from family_logistics.cli import cli
from family_logistics.core import config as config_module
from family_logistics.core.config import Settings

GOOD_TOKEN = "x" * 32
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the container bind under test


@pytest.mark.unit
class TestSettings:
    def test_defaults_are_safe(self):
        settings = Settings()
        assert settings.notification_mode == "shadow"
        assert settings.routes_monthly_budget_usd == 10
        assert settings.routes_budget_warn_usd == 8
        assert settings.imminent_window_minutes == 180
        assert settings.quiet_hours_start.hour == 22
        assert settings.quiet_hours_end.hour == 6
        assert settings.llm_enabled is False
        assert settings.bind_host == "127.0.0.1"
        assert settings.data_key is None
        assert [
            settings.apprise_tag_report,
            settings.apprise_tag_status,
            settings.apprise_tag_warning,
            settings.apprise_tag_urgent,
            settings.apprise_tag_ops,
            settings.apprise_tag_shadow,
        ] == [
            "fl-report",
            "fl-status",
            "fl-warning",
            "fl-urgent",
            "fl-ops",
            "fl-shadow",
        ]

    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("FL_TIMEZONE", "UTC")
        monkeypatch.setenv("FL_CALDAV_CALENDARS", "a, ,b")
        settings = Settings()
        assert settings.timezone == "UTC"
        assert settings.calendar_list == ["a", "b"]

    def test_short_ha_token_rejected(self):
        with pytest.raises(ValidationError):
            Settings.model_validate({"ha_token": "short"})

    def test_secret_not_in_repr(self):
        settings = Settings.model_validate({"ha_token": GOOD_TOKEN})
        assert GOOD_TOKEN not in repr(settings)

    def test_proxy_and_ui_defaults(self):
        settings = Settings()
        assert settings.proxy_trusted_cidrs == []
        assert settings.ui_allowed_users == []

    def test_comma_separated_lists_from_env(self, monkeypatch):
        monkeypatch.setenv("FL_PROXY_TRUSTED_CIDRS", "192.0.2.10/32, 10.0.0.0/24 ,")
        monkeypatch.setenv("FL_UI_ALLOWED_USERS", "morgan, riley")
        settings = Settings()
        assert settings.proxy_trusted_cidrs == ["192.0.2.10/32", "10.0.0.0/24"]
        assert settings.ui_allowed_users == ["morgan", "riley"]

    def test_empty_ui_list_from_env(self, monkeypatch):
        monkeypatch.setenv("FL_UI_ALLOWED_USERS", "")
        assert Settings().ui_allowed_users == []

    @pytest.mark.parametrize("bad", ["not-an-ip", "10.0.0.1/24", "300.1.1.1/32"])
    def test_invalid_cidr_rejected(self, monkeypatch, bad):
        monkeypatch.setenv("FL_PROXY_TRUSTED_CIDRS", bad)
        with pytest.raises(ValidationError):
            Settings()

    def test_bind_host_and_port_from_env(self, monkeypatch):
        monkeypatch.setenv("FL_BIND_HOST", ALL_INTERFACES)
        monkeypatch.setenv("FL_PORT", "8081")
        settings = Settings()
        assert (settings.bind_host, settings.port) == (ALL_INTERFACES, 8081)

    def test_invalid_notification_mode(self):
        with pytest.raises(ValidationError):
            Settings.model_validate({"notification_mode": "loud"})


@pytest.mark.unit
class TestHealth:
    def test_healthz_ok_with_wal(self, tmp_path):
        db = tmp_path / "fl.db"
        app = create_app(Settings(database_path=db))
        with TestClient(app) as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["database"] is True
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "X-Request-ID" in response.headers

    def test_healthz_degraded_when_db_unreachable(self, tmp_path):
        app = create_app(Settings(database_path=tmp_path / "missing" / "fl.db"))
        with TestClient(app) as client:
            response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"


@pytest.fixture
def fresh_settings():
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


@pytest.mark.unit
class TestCli:
    def test_config_hides_secrets(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("FL_HA_TOKEN", GOOD_TOKEN)
        result = CliRunner().invoke(cli, ["config"])
        assert result.exit_code == 0
        assert "HA token configured: True" in result.output
        assert GOOD_TOKEN not in result.output

    def test_serve_refuses_without_token(self, monkeypatch, fresh_settings):
        monkeypatch.delenv("FL_HA_TOKEN", raising=False)
        result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 2

    def test_serve_starts_uvicorn_with_token(
        self, monkeypatch, tmp_path, fresh_settings
    ):
        calls = {}
        monkeypatch.setenv("FL_HA_TOKEN", GOOD_TOKEN)
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.update(kw))
        monkeypatch.setenv("FL_BIND_HOST", ALL_INTERFACES)
        monkeypatch.setenv("FL_PORT", "8081")
        result = CliRunner().invoke(cli, ["serve", "--upgrade-db"])
        assert result.exit_code == 0, result.output
        assert calls["workers"] == 1
        assert calls["log_config"] is None
        # uvicorn would otherwise rewrite request.client from X-Forwarded-For.
        assert calls["proxy_headers"] is False
        assert (calls["host"], calls["port"]) == (ALL_INTERFACES, 8081)

    def test_healthcheck_fails_when_nothing_listens(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("FL_PORT", "1")
        result = CliRunner().invoke(cli, ["healthcheck"])
        assert result.exit_code == 1

    def test_healthcheck_passes_against_live_server(self, monkeypatch, fresh_settings):
        class FakeResponse:
            status = 200

        class FakeConnection:
            def __init__(self, host, port, **kwargs):
                seen.update(host=host, port=port)

            def request(self, method, path):
                assert (method, path) == ("GET", "/healthz")

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        import family_logistics.cli as cli_module

        seen = {}
        monkeypatch.setenv("FL_PORT", "8081")
        monkeypatch.setattr(cli_module, "HTTPConnection", FakeConnection)
        result = CliRunner().invoke(cli, ["healthcheck"])
        assert result.exit_code == 0
        assert seen == {"host": "127.0.0.1", "port": 8081}
