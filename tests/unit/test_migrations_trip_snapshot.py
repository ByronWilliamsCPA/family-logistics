"""Revision 0003_trip_snapshot: upgrade, downgrade and parity with db.schema."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema

pytestmark = pytest.mark.unit


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    yield engine
    engine.dispose()


def test_head_is_trip_snapshot():
    assert migrations_api.head_revision() == "0003_trip_snapshot"


def test_table_matches_the_declared_schema(engine):
    migrations_api.upgrade(engine)
    columns = inspect(engine).get_columns("trip_snapshot")
    assert {c["name"]: c["nullable"] for c in columns} == {
        c.name: c.nullable for c in schema.trip_snapshot.columns
    }
    keys = inspect(engine).get_pk_constraint("trip_snapshot")["constrained_columns"]
    assert keys == ["trip_key"]
    foreign = inspect(engine).get_foreign_keys("trip_snapshot")
    assert [(f["referred_table"], f["constrained_columns"]) for f in foreign] == [
        ("analysis_run", ["run_id"])
    ]


def test_downgrade_removes_only_the_snapshot_table(engine):
    migrations_api.upgrade(engine)
    migrations_api.downgrade(engine, "0002_pilot_core")
    tables = set(inspect(engine).get_table_names())
    assert "trip_snapshot" not in tables
    assert {"analysis_run", "finding"} <= tables
    migrations_api.upgrade(engine)
    assert migrations_api.is_at_head(engine)
    assert "trip_snapshot" in set(inspect(engine).get_table_names())
