"""``family-logistics collect --once`` and the collector wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from caldav_fakes import (
    ALLOWED_HOSTS,
    BASE_URL,
    PARTITION,
    SAM_PATH,
    default_server,
    register_person_calendar,
)
from click.testing import CliRunner

from family_logistics.adapters.caldav.ids import calendar_id_for_href
from family_logistics.cli import SCHEMA_BEHIND_MESSAGE, cli
from family_logistics.core import config as config_module
from family_logistics.core.config import Settings
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.services import wiring

# The fixture events are dated in October 2026 and the collection window is
# relative to "now", so the CLI runs against the same fixed clock as
# test_collector.py; otherwise the occurrence counts drift as real time passes.
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path, monkeypatch):
    config_module.get_settings.cache_clear()
    monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
    monkeypatch.setenv("FL_CALDAV_URL", BASE_URL)
    monkeypatch.setenv("FL_CALDAV_ALLOWED_HOSTS", ",".join(ALLOWED_HOSTS))
    monkeypatch.setenv("FL_CALDAV_USERNAME", "collector@example.test")
    monkeypatch.setenv("FL_CALDAV_APP_PASSWORD", "app-password-for-tests")
    monkeypatch.setenv("FL_CALDAV_CALENDARS", "Sam")
    yield tmp_path
    config_module.get_settings.cache_clear()


def migrated(path) -> None:
    engine = create_sqlite_engine(path / "fl.db")
    try:
        migrations_api.upgrade(engine)
        register_person_calendar(
            engine, calendar_id_for_href(f"{PARTITION}{SAM_PATH}"), "Sam"
        )
    finally:
        engine.dispose()


@pytest.fixture
def fake_server(monkeypatch):
    server = default_server()
    real = wiring.build_collector

    def build(settings, engine, inner=None):
        collector, http = real(settings, engine, inner=inner or server.transport())
        collector.clock = lambda: NOW
        return collector, http

    real_client = wiring.build_caldav_client

    def build_client(settings, inner=None):
        return real_client(settings, inner or server.transport())

    monkeypatch.setattr(wiring, "build_collector", build)
    monkeypatch.setattr(wiring, "build_caldav_client", build_client)
    return server


@pytest.mark.unit
class TestCollectCommand:
    def test_requires_once(self, env):
        result = CliRunner().invoke(cli, ["collect"])
        assert result.exit_code == 2
        assert "collect --once" in result.output

    def test_requires_credentials(self, env, monkeypatch):
        monkeypatch.delenv("FL_CALDAV_APP_PASSWORD")
        result = CliRunner().invoke(cli, ["collect", "--once"])
        assert result.exit_code == 2
        assert "FL_CALDAV_APP_PASSWORD" in result.output
        assert "app-password-for-tests" not in result.output

    def test_requires_schema_at_head(self, env):
        result = CliRunner().invoke(cli, ["collect", "--once"])
        assert result.exit_code == 2
        assert SCHEMA_BEHIND_MESSAGE in result.output

    def test_successful_run_prints_ids_and_counts_only(self, env, fake_server):
        migrated(env)
        result = CliRunner().invoke(cli, ["collect", "--once"])
        assert result.exit_code == 0, result.output
        assert "transport: 1 occurrences, complete (+1 ~0 -0)" in result.output
        assert "person: 3 occurrences, complete (+3 ~0 -0)" in result.output
        assert "DATA-011 findings: 0" in result.output
        assert "Sam" not in result.output
        assert "example.test" not in result.output

    def test_incomplete_run_exits_one(self, env, fake_server):
        migrated(env)
        fake_server.fail_query = {SAM_PATH}
        result = CliRunner().invoke(cli, ["collect", "--once", "--calendar", "Sam"])
        assert result.exit_code == 1
        assert "INCOMPLETE" in result.output
        assert "DATA-011 findings: 1" in result.output

    def test_discovery_failure_is_reported(self, env, fake_server):
        migrated(env)
        fake_server.fail_discovery = True
        result = CliRunner().invoke(cli, ["collect", "--once"])
        assert result.exit_code == 1
        assert "Discovery failed: PROPFIND failed: HTTP 503" in result.output


@pytest.mark.unit
class TestDiscoverCommand:
    def test_lists_ids_flags_and_names_without_urls(self, env, fake_server):
        result = CliRunner().invoke(cli, ["collect", "discover"])
        assert result.exit_code == 0, result.output
        lines = result.output.strip().splitlines()
        assert lines[0] == "calendar_id\tevents\tshared\twritable\tname"
        rows = [line.split("\t") for line in lines[1:]]
        assert [r[4] for r in rows] == [
            "Family Transportation",
            "Sam",
            "Riley work",
            "Tasks",
        ]
        assert rows[0][1:4] == ["yes", "yes", "no"]
        assert rows[3][1:4] == ["no", "no", "yes"]
        assert all(r[0].startswith("cal-") and len(r[0]) == 20 for r in rows)
        assert "example.test" not in result.output
        assert "1000" not in result.output

    def test_requires_credentials(self, env, monkeypatch):
        monkeypatch.delenv("FL_CALDAV_USERNAME")
        result = CliRunner().invoke(cli, ["collect", "discover"])
        assert result.exit_code == 2

    def test_failure_exits_one(self, env, fake_server):
        fake_server.fail_discovery = True
        result = CliRunner().invoke(cli, ["collect", "discover"])
        assert result.exit_code == 1
        assert "Discovery failed" in result.output


@pytest.mark.unit
class TestWiring:
    def test_config_from_settings(self):
        settings = Settings(  # type: ignore[arg-type]
            caldav_calendars="Sam, Jordan",
            collect_days_back=1,
            collect_days_ahead=7,
            source_max_staleness_minutes=90,
        )
        config = wiring.collector_config(settings)
        assert config.selected == ("Sam", "Jordan")
        assert (config.days_back, config.days_ahead) == (1, 7)
        assert config.max_staleness == timedelta(minutes=90)
        assert config.transport_calendar == "Family Transportation"

    def test_build_requires_credentials(self, tmp_path):
        engine = create_sqlite_engine(tmp_path / "fl.db")
        try:
            with pytest.raises(ValueError, match="credentials"):
                wiring.build_collector(Settings(), engine)  # type: ignore[call-arg]
        finally:
            engine.dispose()
