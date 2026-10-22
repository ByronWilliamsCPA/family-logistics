"""Startup checks, structured logging, migrations and verify-db."""

from __future__ import annotations

import sqlite3

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


@pytest.fixture
def fresh_settings():
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def settings_for(tmp_path, **overrides) -> Settings:
    return Settings(  # type: ignore[arg-type]
        ha_token=GOOD_TOKEN, database_path=tmp_path / "fl.db", **overrides
    )


@pytest.mark.unit
class TestStartupProblems:
    def test_fresh_database_needs_migration(self, tmp_path):
        assert startup_problems(settings_for(tmp_path), upgrade_db=False) == [
            SCHEMA_BEHIND_MESSAGE
        ]

    def test_upgrade_db_brings_schema_to_head(self, tmp_path):
        settings = settings_for(tmp_path)
        assert startup_problems(settings, upgrade_db=True) == []
        assert startup_problems(settings, upgrade_db=False) == []

    def test_missing_directory(self, tmp_path):
        settings = Settings(  # type: ignore[arg-type]
            ha_token=GOOD_TOKEN, database_path=tmp_path / "nope" / "fl.db"
        )
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
        settings = settings_for(tmp_path, notification_mode="production")
        problems = startup_problems(settings, upgrade_db=True)
        assert problems == [
            "FL_DATA_KEY is required when FL_NOTIFICATION_MODE=production"
        ]
        keyed = settings_for(
            tmp_path,
            notification_mode="production",
            data_key=Fernet.generate_key().decode(),
        )
        assert startup_problems(keyed, upgrade_db=True) == []

    def test_missing_token(self, tmp_path):
        settings = Settings(database_path=tmp_path / "fl.db")
        assert "FL_HA_TOKEN is required to serve" in startup_problems(
            settings, upgrade_db=True
        )

    def test_refusal_does_not_create_database(self, tmp_path):
        settings = Settings(database_path=tmp_path / "fl.db")
        startup_problems(settings, upgrade_db=True)
        assert list(tmp_path.iterdir()) == []

    def test_refusal_does_not_upgrade_existing_database(self, tmp_path):
        database = tmp_path / "fl.db"
        sqlite3.connect(database).close()
        settings = Settings(database_path=database)
        startup_problems(settings, upgrade_db=True)
        engine = create_readonly_engine(database)
        assert migrations_api.current_revision(engine) is None
        engine.dispose()

    def test_schema_behind_refusal_does_not_create_file(self, tmp_path):
        problems = startup_problems(settings_for(tmp_path), upgrade_db=False)
        assert problems == [SCHEMA_BEHIND_MESSAGE]
        assert list(tmp_path.iterdir()) == []


def _stamp_unknown_revision(database) -> None:
    engine = create_sqlite_engine(database)
    migrations_api.upgrade(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='9999_newer'"))
    engine.dispose()


@pytest.mark.unit
class TestNewerSchema:
    def test_startup_refuses_unknown_revision(self, tmp_path):
        _stamp_unknown_revision(tmp_path / "fl.db")
        problems = startup_problems(settings_for(tmp_path), upgrade_db=True)
        assert len(problems) == 1
        assert "'9999_newer' is not known to this version" in problems[0]

    def test_schema_state_unknown(self, tmp_path):
        _stamp_unknown_revision(tmp_path / "fl.db")
        engine = create_sqlite_engine(tmp_path / "fl.db")
        state = migrations_api.schema_state(engine)
        engine.dispose()
        assert state is migrations_api.SchemaState.UNKNOWN

    def test_upgrade_refuses_unknown_revision(self, tmp_path):
        _stamp_unknown_revision(tmp_path / "fl.db")
        engine = create_sqlite_engine(tmp_path / "fl.db")
        with pytest.raises(migrations_api.MigrationRefusedError, match="newer build"):
            migrations_api.upgrade(engine)
        engine.dispose()

    def test_migrate_command_reports_cleanly(
        self, monkeypatch, tmp_path, fresh_settings
    ):
        _stamp_unknown_revision(tmp_path / "fl.db")
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
        result = CliRunner().invoke(cli, ["migrate"])
        assert result.exit_code == 2
        assert "Migration refused" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


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
        result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 2
        assert "migrate" in result.output


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
        down = CliRunner().invoke(cli, ["migrate", "--downgrade", "--revision", "base"])
        assert down.exit_code == 0
        assert "Schema revision: base" in down.output

    def test_migrate_lower_revision_needs_downgrade_flag(
        self, monkeypatch, tmp_path, fresh_settings
    ):
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
        assert CliRunner().invoke(cli, ["migrate"]).exit_code == 0
        result = CliRunner().invoke(cli, ["migrate", "--revision", "base"])
        assert result.exit_code == 2
        assert "--downgrade" in result.output
        engine = create_sqlite_engine(tmp_path / "fl.db")
        assert migrations_api.is_at_head(engine)
        engine.dispose()

    def test_migrate_bad_revision_is_clean_error(
        self, monkeypatch, tmp_path, fresh_settings
    ):
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
        result = CliRunner().invoke(cli, ["migrate", "--revision", "nope"])
        assert result.exit_code == 2
        assert "Migration refused" in result.output

    def test_migrate_missing_directory(self, monkeypatch, tmp_path, fresh_settings):
        monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "nope" / "fl.db"))
        result = CliRunner().invoke(cli, ["migrate"])
        assert result.exit_code == 2

    def test_verify_db_ok_and_read_only(self, tmp_path):
        database = tmp_path / "fl.db"
        engine = create_sqlite_engine(database)
        migrations_api.upgrade(engine)
        engine.dispose()
        before = database.read_bytes()
        result = CliRunner().invoke(cli, ["verify-db", str(database)])
        assert result.exit_code == 0, result.output
        assert "Integrity: ok" in result.output
        assert database.read_bytes() == before

    def test_verify_db_never_opens_writable(self, monkeypatch, tmp_path):
        database = tmp_path / "fl.db"
        engine = create_sqlite_engine(database)
        migrations_api.upgrade(engine)
        engine.dispose()

        def refuse(*_args, **_kwargs):
            raise AssertionError("verify-db opened a writable engine")

        monkeypatch.setattr(cli_module, "create_sqlite_engine", refuse)
        result = CliRunner().invoke(cli, ["verify-db", str(database)])
        assert result.exit_code == 0, result.output

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

    def test_verify_db_behind_head(self, tmp_path):
        database = tmp_path / "old.db"
        sqlite3.connect(database).close()
        result = CliRunner().invoke(cli, ["verify-db", str(database)])
        assert result.exit_code == 1
        assert "Schema revision: none" in result.output

    def test_verify_db_corrupt(self, tmp_path):
        database = tmp_path / "corrupt.db"
        database.write_bytes(b"this is not a sqlite database" * 100)
        result = CliRunner().invoke(cli, ["verify-db", str(database)])
        assert result.exit_code == 1
        assert "Integrity: FAILED" in result.output
