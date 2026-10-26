"""Retention purge of closed notification state rows (ADR 0008, D9, N9)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fl_notification_state import (
    ISSUE,
    ISSUE_B,
    T0,
    all_rows,
    get_watermark,
    make_engine,
    put_row,
)
from sqlalchemy import insert

from family_logistics.core.exceptions import ValidationError
from family_logistics.db import schema
from family_logistics.db.retention import (
    NOTIFICATION_STATE_RETENTION_DAYS,
    purge_expired_notification_state,
)

pytestmark = pytest.mark.unit

NOW = T0 + timedelta(days=200)
CUTOFF = NOW - timedelta(days=NOTIFICATION_STATE_RETENTION_DAYS)
NAIVE = datetime.fromisoformat("2026-10-23T07:00:00")
OPS = "ops:run_failed"
RUN = "run:run-0001"


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


def purge(engine, now=NOW):
    with engine.begin() as connection:
        return purge_expired_notification_state(connection, now=now)


def keys(engine):
    return {(row.subject_key, row.mode) for row in all_rows(engine)}


def closed(updated_at, **overrides):
    return {
        "is_open": False,
        "updated_at": updated_at,
        "created_at": min(updated_at, T0),
        **overrides,
    }


def test_the_retention_period_is_ninety_days():
    assert NOTIFICATION_STATE_RETENTION_DAYS == 90


class TestBoundary:
    def test_a_closed_row_older_than_the_period_is_deleted(self, engine):
        put_row(engine, **closed(CUTOFF - timedelta(seconds=1)))
        assert purge(engine) == 1
        assert all_rows(engine) == []

    def test_a_closed_row_exactly_at_the_period_is_kept(self, engine):
        put_row(engine, **closed(CUTOFF))
        assert purge(engine) == 0
        assert keys(engine) == {(ISSUE, "shadow")}

    def test_a_closed_row_just_inside_the_period_is_kept(self, engine):
        put_row(engine, **closed(CUTOFF + timedelta(seconds=1)))
        assert purge(engine) == 0
        assert len(all_rows(engine)) == 1

    def test_the_age_is_measured_from_updated_at_not_creation(self, engine):
        put_row(engine, **closed(CUTOFF, created_at=T0))
        assert purge(engine) == 0


class TestOnlyClosedRowsGo:
    def test_an_open_row_is_never_purged_however_old(self, engine):
        put_row(engine, updated_at=T0, created_at=T0)
        assert purge(engine) == 0
        assert keys(engine) == {(ISSUE, "shadow")}

    def test_an_old_open_ops_row_is_kept(self, engine):
        put_row(
            engine,
            subject_key=OPS,
            kind="ops",
            tag="fl-ops",
            rule_id=None,
            updated_at=T0,
        )
        assert purge(engine) == 0

    def test_old_closed_ops_and_run_rows_are_deleted(self, engine):
        old = CUTOFF - timedelta(days=1)
        put_row(
            engine,
            **closed(old, subject_key=OPS, kind="ops", tag="fl-ops", rule_id=None),
        )
        put_row(
            engine,
            **closed(
                old, subject_key=RUN, kind="status", tag="fl-status", rule_id=None
            ),
        )
        put_row(engine, **closed(old, subject_key="run-failed", rule_id=None))
        assert purge(engine) == 3
        assert all_rows(engine) == []

    def test_both_modes_are_purged_independently_by_age(self, engine):
        put_row(engine, **closed(CUTOFF - timedelta(days=1), mode="shadow"))
        put_row(engine, **closed(CUTOFF + timedelta(days=1), mode="production"))
        assert purge(engine) == 1
        assert keys(engine) == {(ISSUE, "production")}

    def test_a_mix_deletes_only_the_old_closed_rows(self, engine):
        old = CUTOFF - timedelta(days=30)
        put_row(engine, **closed(old, subject_key=ISSUE))
        put_row(engine, **closed(CUTOFF + timedelta(days=30), subject_key=ISSUE_B))
        put_row(engine, subject_key="issue:aaaaaaaaaaaaaaaa", updated_at=old)
        assert purge(engine) == 1
        assert keys(engine) == {
            (ISSUE_B, "shadow"),
            ("issue:aaaaaaaaaaaaaaaa", "shadow"),
        }


class TestScope:
    def test_no_other_table_is_touched(self, engine):
        old = CUTOFF - timedelta(days=30)
        put_row(engine, **closed(old))
        with engine.begin() as connection:
            connection.execute(
                insert(schema.notification_watermark).values(
                    id=1, evaluated_at=old, run_id="run-1", updated_at=old
                )
            )
        others = [
            name for name in schema.metadata.tables if name != "notification_state"
        ]
        before = {name: _dump(engine, name) for name in others}
        assert purge(engine) == 1
        assert {name: _dump(engine, name) for name in others} == before
        assert get_watermark(engine) is not None

    def test_it_is_idempotent(self, engine):
        put_row(engine, **closed(CUTOFF - timedelta(days=1)))
        assert purge(engine) == 1
        assert purge(engine) == 0

    def test_an_empty_table_deletes_nothing(self, engine):
        assert purge(engine) == 0

    def test_the_caller_owns_the_transaction(self, engine):
        put_row(engine, **closed(CUTOFF - timedelta(days=1)))
        with engine.connect() as connection:
            assert purge_expired_notification_state(connection, now=NOW) == 1
            connection.rollback()
        assert len(all_rows(engine)) == 1


class TestNaiveClock:
    def test_a_naive_now_is_refused_without_echoing_it(self, engine):
        put_row(engine, **closed(T0))
        with engine.connect() as connection, pytest.raises(ValidationError) as info:
            purge_expired_notification_state(connection, now=NAIVE)
        assert "2026" not in str(info.value)
        assert len(all_rows(engine)) == 1


def _dump(engine, table):
    """Return every row of ``table`` (the name comes from the schema metadata)."""
    with engine.connect() as connection:
        return connection.execute(schema.metadata.tables[table].select()).all()
