"""Startup checks, structured logging, migrations and verify-db."""

from __future__ import annotations

import logging
import sqlite3
from http.client import BadStatusLine
from ipaddress import IPv4Address

import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from family_logistics import cli as cli_module
from family_logistics.cli import SCHEMA_BEHIND_MESSAGE, cli, startup_problems
from family_logistics.core import config as config_module
from family_logistics.core.config import Settings
from family_logistics.core.database import (
    create_readonly_engine,
    create_sqlite_engine,
)
from family_logistics.db import migrations_api

GOOD_TOKEN = "x" * 32
APPRISE_URL = "http://apprise.test:8000"
FUTURE_REVISION = "9999_future"


def _set_revision(path, revision: str) -> None:
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute(
                "UPDATE alembic_version SET version_num = ?", (revision,)
            )
    finally:
        connection.close()


def _database_at(path, revision: str | None = None):
    engine = create_sqlite_engine(path)
    migrations_api.upgrade(engine)
    engine.dispose()
    if revision is not None:
        _set_revision(path, revision)
    return path


def _must_not_serve(*_args, **_kwargs):
    msg = "serve must refuse before starting uvicorn"
    raise AssertionError(msg)


@pytest.fixture
def fresh_settings():
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def settings_for(tmp_path, **overrides) -> Settings:
    values: dict[str, object] = {
        "ha_token": GOOD_TOKEN,
        "database_path": tmp_path / "fl.db",
    }
    return Settings.model_validate(values | overrides)


@pytest.mark.unit
class TestStartupProblems:
    def test_fresh_database_needs_migration(self, tmp_path):
        assert startup_problems(settings_for(tmp_path), upgrade_db=False) == [
            SCHEMA_BEHIND_MESSAGE
        ]
        assert not (tmp_path / "fl.db").exists()

    def test_refusal_never_touches_the_database(self, tmp_path):
        settings = Settings.model_validate({"database_path": tmp_path / "fl.db"})
        assert startup_problems(settings, upgrade_db=True) == [
            "FL_HA_TOKEN is required to serve"
        ]
        assert list(tmp_path.iterdir()) == []

    def test_refusal_does_not_upgrade_existing_database(self, tmp_path):
        database = tmp_path / "fl.db"
        sqlite3.connect(database).close()
        settings = Settings.model_validate({"database_path": database})
        startup_problems(settings, upgrade_db=True)
        engine = create_readonly_engine(database)
        assert migrations_api.current_revision(engine) is None
        engine.dispose()

    def test_inspection_never_opens_a_writable_engine(self, tmp_path, monkeypatch):
        at_head = _database_at(tmp_path / "head.db")
        ahead = _database_at(tmp_path / "ahead.db", FUTURE_REVISION)
        behind = tmp_path / "behind.db"
        sqlite3.connect(behind).close()
        monkeypatch.setattr(cli_module, "create_sqlite_engine", _must_not_serve)
        for path, upgrade_db, expected in (
            (at_head, False, []),
            (at_head, True, []),
            (behind, False, [SCHEMA_BEHIND_MESSAGE]),
        ):
            settings = settings_for(tmp_path, database_path=path)
            assert startup_problems(settings, upgrade_db=upgrade_db) == expected
        settings = settings_for(tmp_path, database_path=ahead)
        [problem] = startup_problems(settings, upgrade_db=True)
        assert "newer than this release" in problem

    def test_problems_accumulate(self, tmp_path):
        settings = Settings.model_validate(
            {
                "database_path": tmp_path / "nope" / "fl.db",
                "notification_mode": "production",
            }
        )
        problems = startup_problems(settings, upgrade_db=True)
        assert problems[:3] == [
            "FL_HA_TOKEN is required to serve",
            "FL_DATA_KEY is required when FL_NOTIFICATION_MODE=production",
            "FL_APPRISE_URL is required when FL_NOTIFICATION_MODE=production",
        ]
        assert "does not exist" in problems[3]

    def test_schema_newer_than_head_is_refused(self, tmp_path):
        _database_at(tmp_path / "fl.db", FUTURE_REVISION)
        for upgrade_db in (False, True):
            problems = startup_problems(settings_for(tmp_path), upgrade_db=upgrade_db)
            assert len(problems) == 1
            assert (
                f"revision {FUTURE_REVISION} is newer than this release"
                in (problems[0])
            )

    def test_failed_upgrade_is_a_problem_not_a_crash(self, tmp_path, monkeypatch):
        def locked(_engine):
            msg = "database is locked by another process; schema unchanged"
            raise migrations_api.MigrationLockError(msg)

        monkeypatch.setattr(migrations_api, "upgrade", locked)
        problems = startup_problems(settings_for(tmp_path), upgrade_db=True)
        assert problems == ["database is locked by another process; schema unchanged"]

    def test_upgrade_db_brings_schema_to_head(self, tmp_path):
        settings = settings_for(tmp_path)
        assert startup_problems(settings, upgrade_db=True) == []
        assert startup_problems(settings, upgrade_db=False) == []

    def test_missing_directory(self, tmp_path):
        settings = settings_for(tmp_path, database_path=tmp_path / "nope" / "fl.db")
        problems = startup_problems(settings, upgrade_db=True)
        assert len(problems) == 1
        assert "does not exist" in problems[0]

    def test_unwritable_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("os.access", lambda *_args: False)
        problems = startup_problems(settings_for(tmp_path), upgrade_db=True)
        assert problems == [
            f"database directory {tmp_path} is not writable by this user"
        ]

    def test_unopenable_database(self, tmp_path):
        (tmp_path / "fl.db").mkdir()  # a directory where the file should be
        problems = startup_problems(settings_for(tmp_path), upgrade_db=True)
        assert problems == [f"database {tmp_path / 'fl.db'} cannot be opened"]

    def test_production_requires_data_key(self, tmp_path):
        settings = settings_for(
            tmp_path, notification_mode="production", apprise_url=APPRISE_URL
        )
        problems = startup_problems(settings, upgrade_db=True)
        assert problems == [
            "FL_DATA_KEY is required when FL_NOTIFICATION_MODE=production"
        ]
        keyed = settings_for(
            tmp_path,
            notification_mode="production",
            apprise_url=APPRISE_URL,
            data_key=Fernet.generate_key().decode(),
        )
        assert startup_problems(keyed, upgrade_db=True) == []

    def test_production_requires_apprise(self, tmp_path):
        settings = settings_for(
            tmp_path,
            notification_mode="production",
            data_key=Fernet.generate_key().decode(),
        )
        assert startup_problems(settings, upgrade_db=True) == [
            "FL_APPRISE_URL is required when FL_NOTIFICATION_MODE=production"
        ]

    def test_missing_token(self, tmp_path):
        settings = Settings.model_validate({"database_path": tmp_path / "fl.db"})
        assert "FL_HA_TOKEN is required to serve" in startup_problems(
            settings, upgrade_db=True
        )


@pytest.mark.unit
class TestServe:
    def test_serve_configures_structured_logging(
        self, monkeypatch, tmp_path, fresh_settings
    ):
        seen = {}
        monkeypatch.setenv("FL_HA_TOKEN", GOOD_TOKEN)
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
        monkeypatch.setenv("FL_JSON_LOGS", "true")
        monkeypatch.setattr(
            cli_module, "setup_logging", lambda **kwargs: seen.update(kwargs)
        )
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: None)
        result = CliRunner().invoke(cli, ["serve", "--upgrade-db"])
        assert result.exit_code == 0, result.output
        assert seen == {
            "level": "INFO",
            "json_logs": True,
            "include_timestamp": True,
            "include_correlation": True,
        }

    def test_serve_refuses_on_old_schema(self, monkeypatch, tmp_path, fresh_settings):
        monkeypatch.setenv("FL_HA_TOKEN", GOOD_TOKEN)
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
        monkeypatch.setattr(cli_module, "setup_logging", lambda **_kwargs: None)
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", _must_not_serve)
        result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 2
        assert "migrate" in result.output

    @pytest.mark.parametrize("args", [["serve"], ["serve", "--upgrade-db"]])
    def test_serve_refuses_newer_schema_cleanly(
        self, args, monkeypatch, tmp_path, fresh_settings
    ):
        _database_at(tmp_path / "fl.db", FUTURE_REVISION)
        monkeypatch.setenv("FL_HA_TOKEN", GOOD_TOKEN)
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
        monkeypatch.setattr(cli_module, "setup_logging", lambda **_kwargs: None)
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", _must_not_serve)
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 2
        assert isinstance(result.exception, SystemExit)
        assert "newer than this release" in result.output
        assert "Traceback" not in result.output

    def test_uvicorn_loggers_route_through_structlog(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_module, "setup_logging", lambda **_kwargs: None)
        loggers = [logging.getLogger(name) for name in cli_module.UVICORN_LOGGERS]
        for logger in loggers:
            monkeypatch.setattr(logger, "handlers", [logging.NullHandler()])
            monkeypatch.setattr(logger, "propagate", False)
        cli_module._configure_logging(settings_for(tmp_path))
        assert [(logger.handlers, logger.propagate) for logger in loggers] == [
            ([], True)
        ] * len(loggers)


@pytest.mark.unit
class TestMigrateAndVerify:
    def test_upgrade_then_downgrade(self, tmp_path):
        engine = create_sqlite_engine(tmp_path / "fl.db")
        assert migrations_api.current_revision(engine) is None
        migrations_api.upgrade(engine)
        assert migrations_api.is_at_head(engine)
        migrations_api.downgrade(engine, "base")
        assert migrations_api.current_revision(engine) is None
        engine.dispose()

    def test_migrate_command(self, monkeypatch, tmp_path, fresh_settings):
        database = tmp_path / "fl.db"
        monkeypatch.setenv("FL_DATABASE_PATH", str(database))
        result = CliRunner().invoke(cli, ["migrate"])
        assert result.exit_code == 0, result.output
        assert migrations_api.head_revision() in result.output
        down = CliRunner().invoke(
            cli, ["migrate", "--downgrade", "--revision", "base", "--yes"]
        )
        assert down.exit_code == 0
        assert "Schema revision: base" in down.output

    def test_lower_revision_without_downgrade_is_refused(
        self, monkeypatch, tmp_path, fresh_settings
    ):
        database = _database_at(tmp_path / "fl.db")
        monkeypatch.setenv("FL_DATABASE_PATH", str(database))
        result = CliRunner().invoke(cli, ["migrate", "--revision", "base"])
        assert result.exit_code == 2
        assert "use an explicit downgrade" in result.output
        assert "Traceback" not in result.output
        engine = create_sqlite_engine(database)
        assert migrations_api.is_at_head(engine)
        engine.dispose()

    def test_downgrade_needs_a_revision(self, monkeypatch, tmp_path, fresh_settings):
        monkeypatch.setenv("FL_DATABASE_PATH", str(_database_at(tmp_path / "fl.db")))
        result = CliRunner().invoke(cli, ["migrate", "--downgrade"])
        assert result.exit_code == 2
        assert "--revision" in result.output

    def test_downgrade_asks_for_confirmation(
        self, monkeypatch, tmp_path, fresh_settings
    ):
        database = _database_at(tmp_path / "fl.db")
        monkeypatch.setenv("FL_DATABASE_PATH", str(database))
        args = ["migrate", "--downgrade", "--revision", "base"]
        declined = CliRunner().invoke(cli, args, input="n\n")
        assert declined.exit_code == 1
        assert "audit history" in declined.output
        engine = create_sqlite_engine(database)
        assert migrations_api.is_at_head(engine)
        engine.dispose()
        accepted = CliRunner().invoke(cli, args, input="y\n")
        assert accepted.exit_code == 0, accepted.output
        assert "Schema revision: base" in accepted.output

    def test_migrate_refuses_newer_schema(self, monkeypatch, tmp_path, fresh_settings):
        database = _database_at(tmp_path / "fl.db", FUTURE_REVISION)
        monkeypatch.setenv("FL_DATABASE_PATH", str(database))
        result = CliRunner().invoke(cli, ["migrate"])
        assert result.exit_code == 2
        assert "newer than this release" in result.output
        assert "Traceback" not in result.output

    def test_migrate_bad_revision_is_clean_error(
        self, monkeypatch, tmp_path, fresh_settings
    ):
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
        result = CliRunner().invoke(cli, ["migrate", "--revision", "nope"])
        assert result.exit_code == 2
        assert "unknown revision 'nope'" in result.output
        assert "Traceback" not in result.output

    def test_migrate_missing_directory(self, monkeypatch, tmp_path, fresh_settings):
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "nope" / "fl.db"))
        result = CliRunner().invoke(cli, ["migrate"])
        assert result.exit_code == 2

    def test_verify_db_ok_and_read_only(self, tmp_path, monkeypatch):
        database = _database_at(tmp_path / "fl.db")
        opened = []
        readonly = cli_module.create_readonly_engine

        def spy(path):
            opened.append(path)
            return readonly(path)

        monkeypatch.setattr(cli_module, "create_readonly_engine", spy)
        monkeypatch.setattr(cli_module, "create_sqlite_engine", _must_not_serve)
        before = database.read_bytes()
        result = CliRunner().invoke(cli, ["verify-db", str(database)])
        assert result.exit_code == 0, result.output
        assert "Integrity: ok" in result.output
        assert "Schema state: at head" in result.output
        assert opened == [database]
        assert database.read_bytes() == before

    def test_readonly_engine_rejects_writes(self, tmp_path):
        database = tmp_path / "fl.db"
        sqlite3.connect(database).close()
        engine = create_readonly_engine(database)
        statement = text("CREATE TABLE t (id INTEGER)")
        with (
            engine.connect() as connection,
            pytest.raises(OperationalError, match="readonly"),
        ):
            connection.execute(statement)
        engine.dispose()

    def test_verify_db_newer_schema(self, tmp_path):
        database = _database_at(tmp_path / "fl.db", FUTURE_REVISION)
        result = CliRunner().invoke(cli, ["verify-db", str(database)])
        assert result.exit_code == 1
        assert "Schema state: unknown revision, newer than this release" in (
            result.output
        )

    def test_verify_db_behind_head(self, tmp_path):
        database = tmp_path / "old.db"
        sqlite3.connect(database).close()
        result = CliRunner().invoke(cli, ["verify-db", str(database)])
        assert result.exit_code == 1
        assert "Schema state: behind head" in result.output
        assert "Schema revision: none" in result.output

    def test_verify_db_corrupt(self, tmp_path):
        database = tmp_path / "corrupt.db"
        database.write_bytes(b"this is not a sqlite database" * 100)
        result = CliRunner().invoke(cli, ["verify-db", str(database)])
        assert result.exit_code == 1
        assert "Integrity: FAILED" in result.output


@pytest.mark.unit
class TestHealthcheck:
    @pytest.mark.parametrize(
        ("bind_host", "probed"),
        [
            (str(IPv4Address(0)), "127.0.0.1"),  # the IPv4 wildcard
            ("::", "::1"),
            ("127.0.0.1", "127.0.0.1"),
            ("192.0.2.10", "192.0.2.10"),
            ("localhost", "localhost"),
        ],
    )
    def test_probe_host(self, bind_host, probed):
        assert cli_module._probe_host(bind_host) == probed

    def test_malformed_response_is_unhealthy(self, monkeypatch, fresh_settings):
        def bad_status(_self):
            raise BadStatusLine("garbage")

        monkeypatch.setattr(cli_module.HTTPConnection, "request", lambda *_a: None)
        monkeypatch.setattr(cli_module.HTTPConnection, "getresponse", bad_status)
        result = CliRunner().invoke(cli, ["healthcheck"])
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
