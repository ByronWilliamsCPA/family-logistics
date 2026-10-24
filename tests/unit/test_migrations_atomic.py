"""A failing migration leaves the schema exactly at the prior revision."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api

if TYPE_CHECKING:
    from pathlib import Path

BROKEN_MIGRATION = '''"""Broken migration used only by tests.

Revision ID: 9999_broken
Revises: {down}
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op

revision = "9999_broken"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("probe", sa.Column("id", sa.Integer, primary_key=True))
    op.execute("INSERT INTO probe (id) VALUES (1)")
    raise RuntimeError("migration failed halfway")


def downgrade() -> None:
    op.drop_table("probe")
'''


@pytest.fixture
def broken_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Copy the packaged migrations and add a failing head revision."""
    base_head = migrations_api.head_revision()
    target = tmp_path / "migrations"
    shutil.copytree(
        migrations_api.MIGRATIONS_DIR,
        target,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (target / "versions" / "9999_broken.py").write_text(
        BROKEN_MIGRATION.format(down=base_head), encoding="utf-8"
    )
    monkeypatch.setattr(migrations_api, "MIGRATIONS_DIR", target)
    return base_head


def _tables(database: Path) -> set[str]:
    engine = create_sqlite_engine(database)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


@pytest.mark.unit
class TestAtomicMigrations:
    def test_failure_on_fresh_database_leaves_it_empty(
        self, tmp_path: Path, broken_head: str
    ) -> None:
        del broken_head
        database = tmp_path / "fl.db"
        engine = create_sqlite_engine(database)
        with pytest.raises(RuntimeError, match="halfway"):
            migrations_api.upgrade(engine)
        assert migrations_api.current_revision(engine) is None
        engine.dispose()
        assert _tables(database) == set()

    def test_failure_keeps_prior_revision(
        self, tmp_path: Path, broken_head: str
    ) -> None:
        database = tmp_path / "fl.db"
        engine = create_sqlite_engine(database)
        migrations_api.upgrade(engine, broken_head)
        with pytest.raises(RuntimeError, match="halfway"):
            migrations_api.upgrade(engine)
        assert migrations_api.current_revision(engine) == broken_head
        engine.dispose()
        assert "probe" not in _tables(database)
        assert "audit_log" in _tables(database)

    def test_connection_isolation_restored(self, tmp_path: Path) -> None:
        engine = create_sqlite_engine(tmp_path / "fl.db")
        migrations_api.upgrade(engine)
        with engine.connect() as connection:
            driver = connection.connection.driver_connection
            level = getattr(driver, "isolation_level", None)
        engine.dispose()
        assert level == ""
