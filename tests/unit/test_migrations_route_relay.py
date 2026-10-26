"""Revision 0005_route_relay: upgrade, downgrade, parity and invalidation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema

pytestmark = pytest.mark.unit

TABLES = ("route_cache", "route_request")
TRIGGER = "trg_location_route_relay_invalidate"
OBSERVED = datetime(2026, 10, 23, 13, 0, tzinfo=UTC)
EXPIRES = OBSERVED + timedelta(days=8)


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    yield engine
    engine.dispose()


def add_locations(engine, *ids):
    with engine.begin() as connection:
        for location_id in ids:
            connection.execute(
                insert(schema.location).values(
                    location_id=location_id,
                    name=location_id,
                    ha_zone_entity_id=f"zone.{location_id}",
                    verification_state="verified",
                )
            )


def cache_row(**overrides):
    values = {
        "origin_location_id": "home",
        "destination_location_id": "school-a",
        "weekday": 4,
        "bucket": 32,
        "duration_seconds": 900,
        "observed_at": OBSERVED,
    }
    return values | overrides


def request_row(**overrides):
    values = {
        "request_id": "r" * 32,
        "origin_location_id": "home",
        "destination_location_id": "school-a",
        "weekday": 4,
        "bucket": 32,
        "created_at": OBSERVED,
        "expires_at": EXPIRES,
        "status": "pending",
        "answered_duration_seconds": None,
        "answered_at": None,
    }
    return values | overrides


def test_the_route_relay_revision_follows_the_school_snapshot(engine):
    assert migrations_api.is_at_head(engine)
    migrations_api.downgrade(engine, "0005_route_relay")
    assert migrations_api.current_revision(engine) == "0005_route_relay"
    migrations_api.downgrade(engine, "0004_school_snapshot")
    assert migrations_api.current_revision(engine) == "0004_school_snapshot"


@pytest.mark.parametrize("table_name", TABLES)
def test_tables_match_the_declared_schema(engine, table_name):
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


def test_no_address_or_free_text_columns_exist(engine):
    names = {c["name"] for table in TABLES for c in inspect(engine).get_columns(table)}
    assert not names & {"address", "zone", "endpoint", "summary", "payload", "raw"}


@pytest.mark.parametrize(
    "row",
    [
        cache_row(weekday=7),
        cache_row(weekday=-1),
        cache_row(bucket=96),
        cache_row(bucket=-1),
        cache_row(duration_seconds=0),
        cache_row(duration_seconds=-5),
        cache_row(duration_seconds=12 * 3600 + 1),
        cache_row(destination_location_id="home"),
    ],
    ids=[
        "weekday-high",
        "weekday-low",
        "bucket-high",
        "bucket-low",
        "zero-duration",
        "negative-duration",
        "huge-duration",
        "same-endpoints",
    ],
)
def test_cache_checks_reject_bad_rows(engine, row):
    add_locations(engine, "home", "school-a")
    statement = insert(schema.route_cache).values(row)
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(statement)


@pytest.mark.parametrize(
    "row",
    [
        request_row(status="done"),
        request_row(weekday=9),
        request_row(bucket=96),
        request_row(expires_at=OBSERVED),
        request_row(destination_location_id="home"),
        request_row(status="answered"),
        request_row(answered_duration_seconds=900),
        request_row(
            status="answered", answered_duration_seconds=0, answered_at=OBSERVED
        ),
        request_row(
            status="answered",
            answered_duration_seconds=12 * 3600 + 1,
            answered_at=OBSERVED,
        ),
    ],
    ids=[
        "unknown-status",
        "weekday",
        "bucket",
        "expiry-not-after-creation",
        "same-endpoints",
        "answered-without-value",
        "pending-with-value",
        "answered-zero",
        "answered-huge",
    ],
)
def test_request_checks_reject_bad_rows(engine, row):
    add_locations(engine, "home", "school-a")
    statement = insert(schema.route_request).values(row)
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(statement)


def test_an_unknown_location_is_refused_by_the_foreign_keys(engine):
    add_locations(engine, "home")
    cache_statement = insert(schema.route_cache).values(cache_row())
    request_statement = insert(schema.route_request).values(request_row())
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(cache_statement)
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(request_statement)


def test_one_pending_request_per_key_but_answered_rows_may_repeat(engine):
    add_locations(engine, "home", "school-a")
    answered = {
        "status": "answered",
        "answered_duration_seconds": 900,
        "answered_at": OBSERVED,
    }
    with engine.begin() as connection:
        connection.execute(insert(schema.route_request).values(request_row()))
        connection.execute(
            insert(schema.route_request).values(
                request_row(request_id="a" * 32, **answered)
            )
        )
        connection.execute(
            insert(schema.route_request).values(
                request_row(request_id="b" * 32, **answered)
            )
        )
    duplicate = insert(schema.route_request).values(request_row(request_id="c" * 32))
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(duplicate)


def seed_entries(engine):
    add_locations(engine, "home", "school-a", "studio")
    with engine.begin() as connection:
        for origin, destination in (
            ("home", "school-a"),
            ("school-a", "home"),
            ("home", "studio"),
        ):
            connection.execute(
                insert(schema.route_cache).values(
                    cache_row(
                        origin_location_id=origin, destination_location_id=destination
                    )
                )
            )
            connection.execute(
                insert(schema.route_request).values(
                    request_row(
                        request_id=f"{origin}{destination}".ljust(32, "x"),
                        origin_location_id=origin,
                        destination_location_id=destination,
                    )
                )
            )


def counts(engine):
    with engine.connect() as connection:
        return (
            connection.execute(text("SELECT count(*) FROM route_cache")).scalar_one(),
            connection.execute(text("SELECT count(*) FROM route_request")).scalar_one(),
        )


@pytest.mark.parametrize(
    "statement",
    [
        (
            "UPDATE location SET ha_zone_entity_id = 'zone.elsewhere'"
            " WHERE location_id = 'school-a'"
        ),
        "UPDATE location SET ha_zone_entity_id = NULL WHERE location_id = 'school-a'",
        (
            "UPDATE location SET verification_state = 'unverified'"
            " WHERE location_id = 'school-a'"
        ),
        (
            "UPDATE location SET address_ciphertext = CAST(:token AS BLOB)"
            " WHERE location_id = 'school-a'"
        ),
        (
            "UPDATE location SET latlon_ciphertext = CAST(:token AS BLOB)"
            " WHERE location_id = 'school-a'"
        ),
        "DELETE FROM location WHERE location_id = 'school-a'",
    ],
    ids=["zone", "zone-cleared", "verification", "address", "coordinates", "delete"],
)
def test_a_location_change_drops_its_entries_in_both_directions(engine, statement):
    seed_entries(engine)
    token = "gAAAAA" + "A" * 120
    with engine.begin() as connection:
        params = {"token": token} if ":token" in statement else {}
        connection.execute(text(statement), params)
    # Only the home -> studio leg does not involve school-a.
    assert counts(engine) == (1, 1)
    with engine.connect() as connection:
        remaining = connection.execute(
            select(schema.route_cache.c.destination_location_id)
        ).scalars()
        assert list(remaining) == ["studio"]


def test_a_name_change_or_a_no_op_write_keeps_the_entries(engine):
    seed_entries(engine)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE location SET name = 'Renamed' WHERE location_id = 'school-a'")
        )
        connection.execute(
            text(
                "UPDATE location SET ha_zone_entity_id = ha_zone_entity_id,"
                " verification_state = verification_state"
            )
        )
    assert counts(engine) == (3, 3)


def test_the_invalidation_is_rolled_back_with_a_failed_update(engine):
    seed_entries(engine)
    # The CHECK on the ciphertext column rejects the write after the trigger ran.
    failing_update = text(
        "UPDATE location SET ha_zone_entity_id = 'zone.elsewhere',"
        " address_ciphertext = X'00' WHERE location_id = 'school-a'"
    )
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(failing_update)
    assert counts(engine) == (3, 3)


def test_downgrade_removes_only_the_route_tables_and_the_trigger(engine):
    seed_entries(engine)
    migrations_api.downgrade(engine, "0004_school_snapshot")
    tables = set(inspect(engine).get_table_names())
    assert not tables & set(TABLES)
    assert {"school_day", "location"} <= tables
    with engine.connect() as connection:
        triggers = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).scalars()
        assert TRIGGER not in set(triggers)
    # A location edit still works with the trigger gone.
    with engine.begin() as connection:
        connection.execute(text("UPDATE location SET ha_zone_entity_id = 'zone.x'"))
    migrations_api.upgrade(engine)
    assert migrations_api.is_at_head(engine)
    assert counts(engine) == (0, 0)
    with engine.connect() as connection:
        triggers = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).scalars()
        assert TRIGGER in set(triggers)
