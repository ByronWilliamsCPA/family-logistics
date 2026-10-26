"""Revision 0004_school_snapshot: upgrade, downgrade and parity with db.schema."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import insert, inspect, text
from sqlalchemy.exc import IntegrityError

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema

pytestmark = pytest.mark.unit

TABLES = ("school_day", "school_snapshot")
FETCHED = datetime(2026, 10, 23, 13, 0, tzinfo=UTC)
RECEIVED = datetime(2026, 10, 23, 13, 1, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    yield engine
    engine.dispose()


def test_head_is_school_snapshot():
    assert migrations_api.head_revision() == "0004_school_snapshot"


@pytest.mark.parametrize("table_name", TABLES)
def test_tables_match_the_declared_schema(engine, table_name):
    migrations_api.upgrade(engine)
    declared = schema.metadata.tables[table_name]
    columns = inspect(engine).get_columns(table_name)
    assert {c["name"]: c["nullable"] for c in columns} == {
        c.name: c.nullable for c in declared.columns
    }
    keys = inspect(engine).get_pk_constraint(table_name)["constrained_columns"]
    assert keys == [c.name for c in declared.primary_key.columns]
    checks = {c["name"] for c in inspect(engine).get_check_constraints(table_name)}
    declared_checks = {
        str(c.name)
        for c in declared.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert checks == declared_checks


def test_no_raw_text_columns_exist(engine):
    migrations_api.upgrade(engine)
    names = {c["name"] for table in TABLES for c in inspect(engine).get_columns(table)}
    assert not names & {"summary", "description", "location", "payload", "raw"}


def test_downgrade_removes_only_the_school_tables(engine):
    migrations_api.upgrade(engine)
    migrations_api.downgrade(engine, "0003_trip_snapshot")
    tables = set(inspect(engine).get_table_names())
    assert not tables & set(TABLES)
    assert "trip_snapshot" in tables
    migrations_api.upgrade(engine)
    assert migrations_api.is_at_head(engine)
    assert set(TABLES) <= set(inspect(engine).get_table_names())


def snapshot_row(**overrides):
    values = {
        "id": 1,
        "fetched_at": FETCHED,
        "received_at": RECEIVED,
        "content_hash": "a" * 64,
        "day_count": 0,
    }
    return values | overrides


def day_row(**overrides):
    values = {
        "person_id": "sam",
        "day": date(2026, 10, 23),
        "location_id": "middle-school",
        "start_utc": FETCHED,
        "end_utc": RECEIVED,
        "kind": "normal",
    }
    return values | overrides


@pytest.mark.parametrize(
    "row",
    [
        snapshot_row(id=2),
        snapshot_row(fetched_at=RECEIVED, received_at=FETCHED),
        snapshot_row(content_hash="short"),
        snapshot_row(day_count=-1),
    ],
    ids=["second-row", "fetched-after-received", "bad-hash", "negative-count"],
)
def test_snapshot_checks_reject_bad_rows(engine, row):
    migrations_api.upgrade(engine)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(schema.school_snapshot).values(row))


@pytest.mark.parametrize(
    "row",
    [
        day_row(kind="holiday"),
        day_row(start_utc=RECEIVED, end_utc=FETCHED),
    ],
    ids=["unknown-kind", "end-before-start"],
)
def test_school_day_checks_reject_bad_rows(engine, row):
    migrations_api.upgrade(engine)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(schema.school_day).values(row))


def test_one_row_per_person_and_day(engine):
    migrations_api.upgrade(engine)
    with engine.begin() as connection:
        connection.execute(insert(schema.school_day).values(day_row()))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(schema.school_day).values(day_row(kind="unknown")))


def test_downgrade_with_stored_days_drops_them(engine):
    migrations_api.upgrade(engine)
    with engine.begin() as connection:
        connection.execute(insert(schema.school_snapshot).values(snapshot_row()))
        connection.execute(insert(schema.school_day).values(day_row()))
    migrations_api.downgrade(engine, "0003_trip_snapshot")
    with engine.connect() as connection:
        names = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).scalars()
        assert not set(names) & set(TABLES)
