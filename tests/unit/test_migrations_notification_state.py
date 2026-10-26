"""Revision 0006_notification_state: upgrade, downgrade, parity and constraints."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema

pytestmark = pytest.mark.unit

TABLES = ("notification_state", "notification_watermark")
NOW = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
ISSUE_KEY = "issue:" + "0123456789abcdef"
RUN_ID = "run-0001"


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    yield engine
    engine.dispose()


def state_row(**overrides):
    values = {
        "subject_key": ISSUE_KEY,
        "mode": "shadow",
        "kind": "notice",
        "tag": "fl-warning",
        "rule_id": "PICK-001",
        "severity": None,
        "is_open": True,
        "delivered": False,
        "bypass_used": False,
        "episode_run_id": RUN_ID,
        "send_count": 0,
        "failed_attempts": 0,
        "first_sent_at": None,
        "last_sent_at": None,
        "last_attempt_at": None,
        "claim_token": None,
        "claimed_at": None,
        "run_id": RUN_ID,
        "evaluated_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return values | overrides


def insert_state(engine, **overrides):
    execute(engine, insert(schema.notification_state).values(state_row(**overrides)))


def execute(engine, statement) -> None:
    with engine.begin() as connection:
        connection.execute(statement)


def normalized(sql: str) -> str:
    """Collapse whitespace and drop the bind-parameter escape of a colon."""
    return re.sub(r"\s+", " ", sql.replace("\\:", ":")).strip()


def test_head_is_the_notification_state_revision(engine):
    assert migrations_api.head_revision() == "0006_notification_state"
    assert migrations_api.is_at_head(engine)


@pytest.mark.parametrize("table_name", TABLES)
def test_tables_match_the_declared_schema(engine, table_name):
    declared = schema.metadata.tables[table_name]
    columns = inspect(engine).get_columns(table_name)
    assert {c["name"]: c["nullable"] for c in columns} == {
        c.name: c.nullable for c in declared.columns
    }
    keys = inspect(engine).get_pk_constraint(table_name)["constrained_columns"]
    assert keys == [c.name for c in declared.primary_key.columns]


@pytest.mark.parametrize("table_name", TABLES)
def test_check_constraints_match_name_and_sql_text(engine, table_name):
    declared = schema.metadata.tables[table_name]
    migrated = {
        c["name"]: normalized(c["sqltext"])
        for c in inspect(engine).get_check_constraints(table_name)
    }
    expected = {
        str(c.name): normalized(str(c.sqltext))
        for c in declared.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert migrated == expected


def test_indexes_match_the_declared_schema(engine):
    migrated = {
        i["name"]: tuple(i["column_names"])
        for i in inspect(engine).get_indexes("notification_state")
    }
    declared = {
        str(i.name): tuple(c.name for c in i.columns)
        for i in schema.notification_state.indexes
    }
    assert migrated == declared
    assert declared == {
        "ix_notification_state_open": ("is_open",),
        "ix_notification_state_updated_at": ("updated_at",),
    }


def test_the_named_checks_exist(engine):
    names = {
        c["name"] for c in inspect(engine).get_check_constraints("notification_state")
    }
    assert names == {
        "ck_notification_state_kind",
        "ck_notification_state_mode",
        "ck_notification_state_tag",
        "ck_notification_state_severity",
        "ck_notification_state_counts",
        "ck_notification_state_claim",
        "ck_notification_state_delivered",
        "ck_notification_state_subject",
    }
    watermark = {
        c["name"]
        for c in inspect(engine).get_check_constraints("notification_watermark")
    }
    assert watermark == {"ck_notification_watermark_singleton"}


def test_no_personal_data_columns_exist(engine):
    names = {c["name"] for table in TABLES for c in inspect(engine).get_columns(table)}
    forbidden = {"name", "address", "title", "body", "message", "summary", "payload"}
    assert not names & forbidden


def test_the_defaults_fill_the_flag_and_counter_columns(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO notification_state (subject_key, mode, kind, tag,"
                " is_open, episode_run_id, run_id, evaluated_at, created_at,"
                " updated_at) VALUES (:key, 'shadow', 'notice', 'fl-warning', 1,"
                " 'r', 'r', :now, :now, :now)"
            ),
            {"key": ISSUE_KEY, "now": "2026-10-26 14:00:00.000000"},
        )
    with engine.connect() as connection:
        row = connection.execute(select(schema.notification_state)).one()
    assert (row.delivered, row.bypass_used, row.send_count, row.failed_attempts) == (
        False,
        False,
        0,
        0,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "reminder"},
        {"mode": "live"},
        {"tag": "warning"},
        {"tag": "fl-"},
        {"tag": "fl-UPPER"},
        {"severity": "fatal"},
        {"send_count": -1},
        {"failed_attempts": -1},
        {"claim_token": "tok"},
        {"claimed_at": NOW},
        {"delivered": True},
        {"delivered": True, "first_sent_at": NOW},
        {"delivered": True, "last_sent_at": NOW},
    ],
    ids=[
        "kind",
        "mode",
        "tag-without-prefix",
        "tag-empty-suffix",
        "tag-uppercase",
        "severity",
        "negative-send-count",
        "negative-failed-attempts",
        "token-without-time",
        "time-without-token",
        "delivered-without-times",
        "delivered-without-last",
        "delivered-without-first",
    ],
)
def test_checks_reject_bad_rows(engine, overrides):
    statement = insert(schema.notification_state).values(state_row(**overrides))
    with pytest.raises(IntegrityError):
        execute(engine, statement)


@pytest.mark.parametrize(
    ("subject_key", "kind"),
    [
        (ISSUE_KEY, "notice"),
        (ISSUE_KEY, "urgent"),
        ("run-failed", "notice"),
        ("run-failed", "urgent"),
        ("run:" + RUN_ID, "status"),
        ("run:" + RUN_ID, "report"),
        ("run:" + "x" * 36, "status"),
        ("ops:run_failed", "ops"),
        ("ops:run_incomplete", "ops"),
    ],
)
def test_valid_subject_and_kind_pairs_are_accepted(engine, subject_key, kind):
    insert_state(engine, subject_key=subject_key, kind=kind)
    with engine.connect() as connection:
        count = connection.execute(select(schema.notification_state)).all()
    assert len(count) == 1


@pytest.mark.parametrize(
    ("subject_key", "kind"),
    [
        (ISSUE_KEY, "status"),
        (ISSUE_KEY, "report"),
        (ISSUE_KEY, "ops"),
        ("issue:" + "0123456789abcde", "notice"),
        ("issue:" + "0123456789abcdeff", "notice"),
        ("issue:" + "0123456789ABCDEF", "notice"),
        ("issue:" + "0123456789abcdeg", "notice"),
        ("run-failed", "status"),
        ("run-failed", "ops"),
        ("run:", "status"),
        ("run:" + "x" * 37, "status"),
        ("run:" + RUN_ID, "notice"),
        ("run:" + RUN_ID, "ops"),
        ("ops:run_failed", "notice"),
        ("ops:run_incomplete", "urgent"),
        ("ops:other", "ops"),
        ("anything-else", "notice"),
    ],
)
def test_invalid_subject_and_kind_pairs_are_rejected(engine, subject_key, kind):
    statement = insert(schema.notification_state).values(
        state_row(subject_key=subject_key, kind=kind)
    )
    with pytest.raises(IntegrityError):
        execute(engine, statement)


def test_the_key_is_per_mode(engine):
    insert_state(engine, mode="shadow")
    insert_state(engine, mode="production")
    statement = insert(schema.notification_state).values(state_row(mode="shadow"))
    with pytest.raises(IntegrityError):
        execute(engine, statement)


def test_the_watermark_is_a_singleton(engine):
    row = {"evaluated_at": NOW, "run_id": RUN_ID, "updated_at": NOW}
    execute(engine, insert(schema.notification_watermark).values(id=1, **row))
    second = insert(schema.notification_watermark).values(id=2, **row)
    duplicate = insert(schema.notification_watermark).values(id=1, **row)
    with pytest.raises(IntegrityError):
        execute(engine, second)
    with pytest.raises(IntegrityError):
        execute(engine, duplicate)


def test_downgrade_removes_only_the_notification_tables_and_round_trips(engine):
    migrations_api.downgrade(engine, "0005_route_relay")
    assert migrations_api.current_revision(engine) == "0005_route_relay"
    tables = set(inspect(engine).get_table_names())
    assert not tables & set(TABLES)
    assert {"route_cache", "route_request", "school_day", "audit_log"} <= tables
    migrations_api.upgrade(engine)
    assert migrations_api.is_at_head(engine)
    assert set(TABLES) <= set(inspect(engine).get_table_names())
    insert_state(engine)
