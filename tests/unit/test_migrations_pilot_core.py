"""Revision 0002_pilot_core: upgrade, downgrade, parity with db.schema."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from click.testing import CliRunner
from sqlalchemy import insert, inspect
from sqlalchemy.exc import IntegrityError

from family_logistics.cli import cli
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema

PILOT_TABLES = {
    "alias",
    "analysis_run",
    "calendar_source",
    "external_passenger",
    "finding",
    "household",
    "location",
    "person",
    "source_event",
    "sync_state",
    "transport_default",
    "vehicle",
}
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    yield engine
    engine.dispose()


def table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


@pytest.mark.unit
class TestPilotCoreMigration:
    def test_head_is_pilot_core(self):
        assert migrations_api.head_revision() == "0002_pilot_core"

    def test_upgrade_downgrade_reupgrade(self, engine):
        migrations_api.upgrade(engine)
        assert table_names(engine) >= PILOT_TABLES
        migrations_api.downgrade(engine, "0001_baseline")
        assert table_names(engine) == {"audit_log"}
        assert migrations_api.current_revision(engine) == "0001_baseline"
        migrations_api.upgrade(engine)
        assert migrations_api.is_at_head(engine)
        assert table_names(engine) >= PILOT_TABLES

    def test_migrated_columns_match_schema_module(self, engine):
        migrations_api.upgrade(engine)
        inspector = inspect(engine)
        for name, table in schema.metadata.tables.items():
            migrated = {column["name"] for column in inspector.get_columns(name)}
            assert migrated == set(table.columns.keys()), name

    def test_source_event_occurrence_is_unique(self, engine):
        migrations_api.upgrade(engine)
        row = {
            "calendar_id": "cal-t",
            "uid": "uid-1",
            "recurrence_id": "",
            "start_utc": NOW,
            "end_utc": NOW,
            "status": "confirmed",
            "raw_hash": "0" * 64,
            "first_seen": NOW,
            "last_seen": NOW,
        }
        with engine.begin() as connection:
            connection.execute(
                insert(schema.calendar_source).values(
                    calendar_id="cal-t", display_name="Transport", kind="transport"
                )
            )
            connection.execute(insert(schema.source_event).values(**row))
        with engine.begin() as connection:
            statement = insert(schema.source_event).values(**row)
            with pytest.raises(IntegrityError):
                connection.execute(statement)

    def test_alias_is_case_insensitive_per_kind(self, engine):
        migrations_api.upgrade(engine)
        with engine.begin() as connection:
            connection.execute(
                insert(schema.alias).values(kind="person", alias="Sam", target_id="sam")
            )
            connection.execute(
                insert(schema.alias).values(
                    kind="location", alias="sam", target_id="sams-place"
                )
            )
        with engine.begin() as connection:
            statement = insert(schema.alias).values(
                kind="person", alias="SAM", target_id="other"
            )
            with pytest.raises(IntegrityError):
                connection.execute(statement)

    def test_verify_db_reports_head(self, tmp_path):
        database = tmp_path / "verify.db"
        engine = create_sqlite_engine(database)
        migrations_api.upgrade(engine)
        engine.dispose()
        result = CliRunner().invoke(cli, ["verify-db", str(database)])
        assert result.exit_code == 0, result.output
        assert "0002_pilot_core" in result.output
