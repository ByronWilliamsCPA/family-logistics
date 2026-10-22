"""Migration safety: atomic upgrades, one migrator at a time, no unknown schema."""

from __future__ import annotations

import multiprocessing
import shutil
import sqlite3
from typing import TYPE_CHECKING

import pytest
from alembic import command
from sqlalchemy import inspect

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema

if TYPE_CHECKING:
    from multiprocessing.queues import Queue
    from multiprocessing.synchronize import Barrier
    from pathlib import Path

FAILING_REVISION = '''"""Test-only revision that fails half-way."""

import sqlalchemy as sa
from alembic import op

revision = "9000_fails"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("half_done", sa.Column("id", sa.Integer, primary_key=True))
    op.execute("INSERT INTO no_such_table VALUES (1)")


def downgrade() -> None:
    op.drop_table("half_done")
'''
UPGRADERS = 3


def _set_revision(path: Path, revision: str) -> None:
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute(
                "UPDATE alembic_version SET version_num = ?", (revision,)
            )
    finally:
        connection.close()


def _upgrade_worker(path: str, barrier: Barrier, results: Queue[str]) -> None:
    """Run one upgrade in a separate process, like a second container."""
    from pathlib import Path

    engine = create_sqlite_engine(Path(path))
    barrier.wait()
    try:
        migrations_api.upgrade(engine)
        results.put("ok")
    except migrations_api.MigrationError as exc:
        results.put(f"error: {exc}")
    finally:
        engine.dispose()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "fl.db"


@pytest.fixture
def engine(path):
    engine = create_sqlite_engine(path)
    yield engine
    engine.dispose()


@pytest.mark.unit
class TestAtomicUpgrade:
    def test_failed_upgrade_leaves_no_partial_schema(
        self, engine, tmp_path, monkeypatch
    ):
        broken = tmp_path / "migrations"
        shutil.copytree(migrations_api.MIGRATIONS_DIR, broken)
        (broken / "versions" / "9000_fails.py").write_text(
            FAILING_REVISION.format(down=migrations_api.head_revision())
        )
        monkeypatch.setattr(migrations_api, "MIGRATIONS_DIR", broken)
        with pytest.raises(migrations_api.MigrationError, match="rolled back"):
            migrations_api.upgrade(engine)
        assert inspect(engine).get_table_names() == []
        assert migrations_api.current_revision(engine) is None
        monkeypatch.undo()
        migrations_api.upgrade(engine)
        assert migrations_api.is_at_head(engine)

    def test_concurrent_upgraders_are_serialized(self, path):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(UPGRADERS)
        results = context.Queue()
        workers = [
            context.Process(target=_upgrade_worker, args=(str(path), barrier, results))
            for _ in range(UPGRADERS)
        ]
        for worker in workers:
            worker.start()
        outcomes = sorted(results.get(timeout=60) for _ in workers)
        for worker in workers:
            worker.join(timeout=60)
        assert outcomes == ["ok"] * UPGRADERS
        connection = sqlite3.connect(path)
        try:
            versions = connection.execute("SELECT version_num FROM alembic_version")
            assert versions.fetchall() == [(migrations_api.head_revision(),)]
        finally:
            connection.close()

    def test_lock_timeout_is_reported(self, engine, path, monkeypatch):
        migrations_api.upgrade(engine)
        monkeypatch.setattr(migrations_api, "MIGRATION_LOCK_TIMEOUT_MS", 100)
        holder = sqlite3.connect(path, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            with pytest.raises(migrations_api.MigrationLockError, match="locked"):
                migrations_api.upgrade(engine)
        finally:
            holder.execute("ROLLBACK")
            holder.close()


@pytest.mark.unit
class TestRevisionChecks:
    def test_schema_newer_than_head_is_refused(self, engine, path):
        migrations_api.upgrade(engine)
        engine.dispose()
        _set_revision(path, "9999_future")
        assert migrations_api.schema_state(engine) is migrations_api.SchemaState.AHEAD
        with pytest.raises(migrations_api.SchemaAheadError, match="newer"):
            migrations_api.upgrade(engine)
        with pytest.raises(migrations_api.SchemaAheadError):
            migrations_api.downgrade(engine, "base")
        assert migrations_api.current_revision(engine) == "9999_future"

    def test_schema_states(self, engine):
        assert migrations_api.schema_state(engine) is migrations_api.SchemaState.BEHIND
        migrations_api.upgrade(engine)
        assert migrations_api.schema_state(engine) is migrations_api.SchemaState.AT_HEAD

    def test_upgrade_never_moves_down(self, engine):
        migrations_api.upgrade(engine)
        with pytest.raises(migrations_api.MigrationDirectionError, match="downgrade"):
            migrations_api.upgrade(engine, "base")
        assert migrations_api.is_at_head(engine)

    def test_downgrade_never_moves_up(self, engine):
        with pytest.raises(migrations_api.MigrationDirectionError, match="below"):
            migrations_api.downgrade(engine, "head")
        migrations_api.upgrade(engine)
        with pytest.raises(migrations_api.MigrationDirectionError, match="below"):
            migrations_api.downgrade(engine, migrations_api.head_revision())
        assert migrations_api.is_at_head(engine)

    def test_unknown_target_is_reported(self, engine):
        with pytest.raises(migrations_api.MigrationError, match="unknown revision"):
            migrations_api.upgrade(engine, "no-such-revision")


@pytest.mark.unit
class TestHeadSchema:
    def test_append_only_triggers_and_index_exist(self, engine):
        migrations_api.upgrade(engine)
        inspector = inspect(engine)
        with engine.connect() as connection:
            triggers = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
        assert {
            "audit_log_no_update",
            "audit_log_no_replace",
            "audit_log_no_delete",
        } <= triggers
        assert "ix_audit_log_object_ref" in {
            index["name"] for index in inspector.get_indexes("audit_log")
        }

    def test_audit_columns_match_schema_module(self, engine):
        migrations_api.upgrade(engine)
        migrated = {
            column["name"]: column["nullable"]
            for column in inspect(engine).get_columns("audit_log")
        }
        declared = {column.name: column.nullable for column in schema.audit_log.columns}
        assert migrated == declared

    def test_alembic_without_a_connection_fails_clearly(self):
        config = migrations_api._config()
        with pytest.raises(RuntimeError, match="migrations_api"):
            command.upgrade(config, "head")
