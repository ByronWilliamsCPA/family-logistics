"""Revision 0003_trip_snapshot: upgrade, downgrade and parity with db.schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fl_factories import notes, trip
from sqlalchemy import func, inspect, select

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.domain.engine import AssessmentResult
from family_logistics.domain.lifecycle import Lifecycle, TrackedFinding
from family_logistics.domain.models import Finding, RunStatus, Severity

pytestmark = pytest.mark.unit


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    yield engine
    engine.dispose()


def test_trip_snapshot_revision_upgrades_on_its_own(engine):
    migrations_api.upgrade(engine, "0003_trip_snapshot")
    assert migrations_api.current_revision(engine) == "0003_trip_snapshot"
    assert "trip_snapshot" in set(inspect(engine).get_table_names())


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


# Indexes revision 0003 adds to the 0002 tables: table -> (name, columns).
RUN_INDEXES = {
    "analysis_run": ("ix_analysis_run_started_at", ["started_at"]),
    "finding": ("ix_finding_run_id", ["run_id"]),
}


def index_columns(engine, table_name):
    return {
        index["name"]: index["column_names"]
        for index in inspect(engine).get_indexes(table_name)
    }


def row_count(engine, table_name):
    table = schema.metadata.tables[table_name]
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def test_upgrade_creates_the_run_and_finding_indexes(engine):
    migrations_api.upgrade(engine)
    for table_name, (name, columns) in RUN_INDEXES.items():
        assert index_columns(engine, table_name)[name] == columns
        declared = {index.name for index in schema.metadata.tables[table_name].indexes}
        assert name in declared


def test_downgrade_drops_the_new_indexes_and_keeps_the_older_ones(engine):
    migrations_api.upgrade(engine)
    migrations_api.downgrade(engine, "0002_pilot_core")
    for table_name, (name, _) in RUN_INDEXES.items():
        assert name not in index_columns(engine, table_name)
    assert "ix_finding_lifecycle" in index_columns(engine, "finding")
    migrations_api.upgrade(engine)
    for table_name, (name, columns) in RUN_INDEXES.items():
        assert index_columns(engine, table_name)[name] == columns


def test_downgrade_with_a_stored_snapshot_keeps_runs_and_findings(engine):
    migrations_api.upgrade(engine)
    repo = AssessmentRepository(engine)
    started = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    run_id = repo.begin_run(
        "on_demand", started, started + timedelta(days=14), started, rules_version="x"
    )
    issue = Finding("RIDE-005", Severity.CRITICAL, "no ride", ("person:sam",), None)
    outcome = AssessmentResult(
        RunStatus.ATTENTION_REQUIRED,
        (TrackedFinding(issue, Lifecycle.NEW),),
        (),
        (),
        started,
    )
    repo.complete_run(run_id, outcome, started, [trip(notes())])
    assert row_count(engine, "trip_snapshot") == 1

    migrations_api.downgrade(engine, "0002_pilot_core")

    assert "trip_snapshot" not in set(inspect(engine).get_table_names())
    assert row_count(engine, "analysis_run") == 1
    assert row_count(engine, "finding") == 1
    migrations_api.upgrade(engine)
    assert migrations_api.is_at_head(engine)
    assert row_count(engine, "trip_snapshot") == 0
