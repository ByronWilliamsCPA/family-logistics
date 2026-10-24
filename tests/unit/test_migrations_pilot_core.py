"""Revision 0002_pilot_core: upgrade, downgrade, parity with db.schema."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import get_args

import pytest
from click.testing import CliRunner
from sqlalchemy import (
    CheckConstraint,
    Column,
    UniqueConstraint,
    func,
    insert,
    inspect,
    select,
)
from sqlalchemy.exc import IntegrityError

from family_logistics.cli import cli
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema
from family_logistics.domain.events import (
    CalendarKind,
    EventStatus,
    TransportDirection,
    TransportMode,
)
from family_logistics.domain.models import PersonRole, RunStatus, Severity

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
AUDIT_TRIGGERS = {
    "audit_log_no_update",
    "audit_log_no_replace",
    "audit_log_no_delete",
}
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
EARLIER = NOW - timedelta(minutes=1)

# One valid row per table. Foreign keys point at the rows ``seed_registry``
# inserts; primary keys do not collide with them.
BASE_ROWS: dict[str, dict[str, object]] = {
    "calendar_source": {
        "calendar_id": "cal-new",
        "display_name": "Transport",
        "kind": "transport",
    },
    "source_event": {
        "calendar_id": "cal-1",
        "uid": "uid-1",
        "recurrence_id": "",
        "start_utc": NOW,
        "end_utc": LATER,
        "status": "confirmed",
        "raw_hash": "0" * 64,
        "first_seen": NOW,
        "last_seen": NOW,
    },
    "sync_state": {"calendar_id": "cal-1"},
    "person": {"person_id": "p-new", "display_name": "Riley", "role": "parent"},
    "external_passenger": {
        "passenger_id": "pass-1",
        "display_first_name": "Passenger E",
        "household_id": "hh-1",
    },
    "alias": {"kind": "person", "alias": "Sam", "target_id": "p-child"},
    "transport_default": {
        "person_id": "p-child",
        "weekdays": [0, 1],
        "direction": "to_school",
        "mode": "bike",
        "origin_location_id": "loc-home",
        "destination_location_id": "loc-school",
    },
    "analysis_run": {
        "run_id": "run-1",
        "trigger": "hourly",
        "window_start": NOW,
        "window_end": LATER,
        "started_at": NOW,
        "status": "clear",
        "rules_version": "1",
    },
    "finding": {
        "issue_id": "iss-1",
        "rule_id": "R1",
        "severity": "critical",
        "message": "Example finding",
        "lifecycle": "new",
        "first_seen": NOW,
        "last_seen": NOW,
    },
}

# (case id, constraint, table, overrides violating only that constraint,
# overrides of the neighbouring valid row). ck_calendar_source_person_owner has
# its own four-way test below.
CHECK_CASES = [
    (
        "calendar-kind",
        "ck_calendar_source_kind",
        "calendar_source",
        {"kind": "bogus"},
        {"kind": "school_bell"},
    ),
    (
        "event-status",
        "ck_source_event_status",
        "source_event",
        {"status": "postponed"},
        {"status": "cancelled"},
    ),
    (
        "event-end-before-start",
        "ck_source_event_end_after_start",
        "source_event",
        {"end_utc": EARLIER, "start_utc": NOW},
        {"end_utc": NOW, "start_utc": NOW},
    ),
    (
        "person-role",
        "ck_person_role",
        "person",
        {"role": "grandparent"},
        {"role": "contact"},
    ),
    (
        "person-monitoring-range",
        "ck_person_monitoring_range",
        "person",
        {"monitoring_start": date(2026, 9, 2), "monitoring_end": date(2026, 9, 1)},
        {"monitoring_start": date(2026, 9, 1), "monitoring_end": date(2026, 9, 1)},
    ),
    (
        "passenger-name-blank",
        "ck_external_passenger_first_name",
        "external_passenger",
        {"display_first_name": "   "},
        {"display_first_name": "E"},
    ),
    (
        "passenger-name-empty",
        "ck_external_passenger_first_name",
        "external_passenger",
        {"display_first_name": ""},
        {"display_first_name": "E"},
    ),
    (
        "passenger-name-too-long",
        "ck_external_passenger_first_name",
        "external_passenger",
        {"display_first_name": "x" * 61},
        {"display_first_name": "x" * 60},
    ),
    (
        "alias-kind",
        "ck_alias_kind",
        "alias",
        {"kind": "vehicle"},
        {"kind": "passenger"},
    ),
    (
        "transport-direction",
        "ck_transport_default_direction",
        "transport_default",
        {"direction": "sideways"},
        {"direction": "from_school"},
    ),
    (
        "transport-mode",
        "ck_transport_default_mode",
        "transport_default",
        {"mode": "teleport"},
        {"mode": "carpool"},
    ),
    (
        "transport-effective-range",
        "ck_transport_default_effective_range",
        "transport_default",
        {"effective_from": date(2026, 9, 2), "effective_to": date(2026, 9, 1)},
        {"effective_from": date(2026, 9, 1), "effective_to": date(2026, 9, 1)},
    ),
    (
        "run-status",
        "ck_analysis_run_status",
        "analysis_run",
        {"status": "unknown"},
        {"status": "failed"},
    ),
    (
        "run-window",
        "ck_analysis_run_window",
        "analysis_run",
        {"window_start": NOW, "window_end": EARLIER},
        {"window_start": NOW, "window_end": NOW},
    ),
    (
        "finding-severity",
        "ck_finding_severity",
        "finding",
        {"severity": "fatal"},
        {"severity": "incomplete"},
    ),
    (
        "finding-lifecycle",
        "ck_finding_lifecycle",
        "finding",
        {"lifecycle": "closed"},
        {"lifecycle": "regressed"},
    ),
]
OWNER_CHECK = "ck_calendar_source_person_owner"


def enum_values(enum_class) -> set[str]:
    return {member.value for member in enum_class}


# Enum-backed CHECK domains: (table, constraint, Python values). The alias
# ``kind`` and finding ``lifecycle`` domains have no Python enum in the domain
# package yet, so only their behaviour tests above pin them.
ENUM_CHECKS = [
    ("calendar_source", "ck_calendar_source_kind", enum_values(CalendarKind)),
    ("source_event", "ck_source_event_status", set(get_args(EventStatus))),
    ("person", "ck_person_role", enum_values(PersonRole)),
    (
        "transport_default",
        "ck_transport_default_direction",
        enum_values(TransportDirection),
    ),
    ("transport_default", "ck_transport_default_mode", enum_values(TransportMode)),
    ("analysis_run", "ck_analysis_run_status", enum_values(RunStatus)),
    ("finding", "ck_finding_severity", enum_values(Severity)),
]

# Every foreign key that can be hit with a single bad value on a valid row:
# (table, overrides carrying an id that does not exist).
ORPHAN_CASES = [
    ("calendar_source", {"kind": "person", "person_id": "ghost"}),
    ("source_event", {"calendar_id": "ghost"}),
    ("sync_state", {"calendar_id": "ghost"}),
    ("person", {"default_vehicle_id": "ghost"}),
    ("person", {"home_location_id": "ghost"}),
    ("external_passenger", {"household_id": "ghost"}),
    ("external_passenger", {"contact_id": "ghost"}),
    ("external_passenger", {"default_pickup_location_id": "ghost"}),
    ("transport_default", {"person_id": "ghost"}),
    ("transport_default", {"driver_id": "ghost"}),
    ("transport_default", {"origin_location_id": "ghost"}),
    ("transport_default", {"destination_location_id": "ghost"}),
    ("finding", {"run_id": "ghost"}),
]


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    yield engine
    engine.dispose()


@pytest.fixture
def seeded(engine):
    """A database at head holding the parent rows the base rows point at."""
    migrations_api.upgrade(engine)
    with engine.begin() as connection:
        seed_registry(connection)
    return engine


def table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def put(connection, table_name: str, **values: object) -> None:
    connection.execute(insert(schema.metadata.tables[table_name]).values(**values))


def insert_row(engine, table_name: str, row: dict[str, object]) -> None:
    with engine.begin() as connection:
        put(connection, table_name, **row)


def row_count(engine, table_name: str) -> int:
    table = schema.metadata.tables[table_name]
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(table)) or 0


def object_names(engine, object_type: str) -> set[str]:
    """Names of sqlite_master objects of one type, without SQLite internals."""
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = ?", (object_type,)
        )
        return {row[0] for row in rows if not row[0].startswith("sqlite_")}


def seed_registry(connection) -> None:
    """Insert the parent rows referenced by ``BASE_ROWS``."""
    put(connection, "location", location_id="loc-home", name="Home")
    put(connection, "location", location_id="loc-school", name="School")
    put(connection, "vehicle", vehicle_id="veh-1", display_name="Family van")
    put(connection, "household", household_id="hh-1", label="Household E")
    put(
        connection,
        "person",
        person_id="p-parent",
        display_name="Morgan",
        role="parent",
        default_vehicle_id="veh-1",
        home_location_id="loc-home",
    )
    put(connection, "person", person_id="p-child", display_name="Alex", role="child")
    put(
        connection,
        "calendar_source",
        calendar_id="cal-1",
        display_name="Transport",
        kind="transport",
    )


def seed_every_table(connection) -> None:
    """Put at least one row in each pilot table plus one audit row."""
    seed_registry(connection)
    put(
        connection,
        "calendar_source",
        calendar_id="cal-parent",
        display_name="Morgan",
        kind="person",
        person_id="p-parent",
    )
    put(connection, "source_event", **BASE_ROWS["source_event"])
    put(connection, "sync_state", **BASE_ROWS["sync_state"])
    put(
        connection,
        "external_passenger",
        **BASE_ROWS["external_passenger"],
        contact_id="p-parent",
        default_pickup_location_id="loc-home",
        default_dropoff_location_id="loc-school",
    )
    put(connection, "alias", **BASE_ROWS["alias"])
    put(
        connection,
        "transport_default",
        **BASE_ROWS["transport_default"],
        driver_id="p-parent",
    )
    put(connection, "analysis_run", **BASE_ROWS["analysis_run"])
    put(connection, "finding", **BASE_ROWS["finding"], run_id="run-1")
    put(
        connection,
        "audit_log",
        id=1,
        recorded_at=NOW,
        actor="morgan",
        object_ref="person:p-child",
        action="create",
        hash_key_version=1,
    )


def normalize_sql(text: str) -> str:
    """Collapse whitespace and drop it around parentheses.

    SQLite keeps the CREATE TABLE text verbatim and SQLAlchemy strips the
    outer CHECK parentheses on reflection, so both sides can differ only in
    whitespace; this absorbs that and nothing else.
    """
    collapsed = " ".join(text.split())
    return re.sub(r"\s*([()])\s*", r"\1", collapsed)


def declared_checks(table) -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def migrated_checks(inspector, table_name: str) -> dict[str, str]:
    return {
        check["name"]: check["sqltext"]
        for check in inspector.get_check_constraints(table_name)
    }


def declared_indexes(table) -> dict[str, tuple[tuple[str | None, ...], bool]]:
    """Index name -> (column names, unique); an expression column is None."""
    return {
        index.name: (
            tuple(
                expression.name if isinstance(expression, Column) else None
                for expression in index.expressions
            ),
            bool(index.unique),
        )
        for index in table.indexes
    }


def migrated_indexes(connection, table_name: str) -> dict[str, tuple]:
    """Explicit (CREATE INDEX) indexes of a table, read with SQLite pragmas.

    The inspector skips expression indexes such as ``ux_alias_kind_alias``, so
    the pragmas are queried directly.
    """
    found = {}
    listing = connection.exec_driver_sql(
        "SELECT * FROM pragma_index_list(?)", (table_name,)
    ).fetchall()
    for row in listing:
        name, unique, origin = row[1], row[2], row[3]
        if origin != "c":
            continue
        info = connection.exec_driver_sql(
            "SELECT * FROM pragma_index_xinfo(?)", (name,)
        ).fetchall()
        columns = tuple(
            item[2] for item in sorted(info, key=lambda item: item[0]) if item[5] == 1
        )
        found[name] = (columns, bool(unique))
    return found


def parse_default(text: str) -> object:
    """Turn a reflected SQLite DEFAULT into a Python value (int or str)."""
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    return int(text)


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

    def test_downgrade_with_data_in_every_table(self, engine):
        migrations_api.upgrade(engine)
        with engine.begin() as connection:
            seed_every_table(connection)
        for name in PILOT_TABLES | {"audit_log"}:
            assert row_count(engine, name) >= 1, name

        # Dropping a parent before its children raises "FOREIGN KEY constraint
        # failed" here because the engine enforces foreign keys.
        migrations_api.downgrade(engine, "0001_baseline")

        assert table_names(engine) == {"audit_log"}
        assert migrations_api.current_revision(engine) == "0001_baseline"
        assert object_names(engine, "trigger") >= AUDIT_TRIGGERS
        assert object_names(engine, "index") == {"ix_audit_log_object_ref"}
        assert row_count(engine, "audit_log") == 1

        migrations_api.upgrade(engine)

        assert migrations_api.is_at_head(engine)
        assert table_names(engine) == PILOT_TABLES | {"audit_log"}
        assert object_names(engine, "trigger") >= AUDIT_TRIGGERS
        for name in PILOT_TABLES:
            assert row_count(engine, name) == 0, name
        assert row_count(engine, "audit_log") == 1

    def test_migrated_columns_match_schema_module(self, engine):
        migrations_api.upgrade(engine)
        inspector = inspect(engine)
        for name, table in schema.metadata.tables.items():
            migrated = {
                column["name"]: column["nullable"]
                for column in inspector.get_columns(name)
            }
            declared = {column.name: column.nullable for column in table.columns}
            assert migrated == declared, name
            migrated_fks = {
                (tuple(fk["constrained_columns"]), fk["referred_table"])
                for fk in inspector.get_foreign_keys(name)
            }
            declared_fks = {
                (tuple(c.name for c in fk.columns), fk.referred_table.name)
                for fk in table.foreign_key_constraints
            }
            assert migrated_fks == declared_fks, name

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


@pytest.mark.unit
class TestOccurrenceKey:
    def occurrence(self, calendar_id: str, uid: str, recurrence_id: str):
        return {
            **BASE_ROWS["source_event"],
            "calendar_id": calendar_id,
            "uid": uid,
            "recurrence_id": recurrence_id,
        }

    def test_key_is_calendar_uid_and_recurrence_id(self, seeded):
        override = "2026-10-22T14:00:00Z"
        insert_row(seeded, "calendar_source", {**BASE_ROWS["calendar_source"]})
        insert_row(seeded, "source_event", self.occurrence("cal-1", "uid-1", ""))
        # Same series, different RECURRENCE-ID: an override or later instance.
        insert_row(seeded, "source_event", self.occurrence("cal-1", "uid-1", override))
        insert_row(seeded, "source_event", self.occurrence("cal-1", "uid-2", ""))
        # Same UID on another calendar is another occurrence.
        insert_row(seeded, "source_event", self.occurrence("cal-new", "uid-1", ""))
        assert row_count(seeded, "source_event") == 4

        duplicates = [
            ("cal-1", "uid-1", ""),
            ("cal-1", "uid-1", override),
            ("cal-new", "uid-1", ""),
        ]
        for calendar_id, uid, recurrence_id in duplicates:
            with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
                insert_row(
                    seeded,
                    "source_event",
                    self.occurrence(calendar_id, uid, recurrence_id),
                )
        assert row_count(seeded, "source_event") == 4

    def test_unique_constraint_columns(self, seeded):
        constraints = inspect(seeded).get_unique_constraints("source_event")
        assert {uc["name"]: tuple(uc["column_names"]) for uc in constraints} == {
            "uq_source_event_occurrence": ("calendar_id", "uid", "recurrence_id")
        }


@pytest.mark.unit
class TestForeignKeyEnforcement:
    def test_application_engine_enables_foreign_keys(self, engine):
        migrations_api.upgrade(engine)
        with engine.connect() as connection:
            enabled = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
        assert enabled == 1

    def test_calendar_owner_must_exist(self, seeded):
        # kind 'person' with an owner satisfies ck_calendar_source_person_owner,
        # so only the foreign key can reject this row.
        row = {**BASE_ROWS["calendar_source"], "kind": "person", "person_id": "ghost"}
        before = row_count(seeded, "calendar_source")
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            insert_row(seeded, "calendar_source", row)
        assert row_count(seeded, "calendar_source") == before

    def test_calendar_owner_that_exists_is_accepted(self, seeded):
        row = {
            **BASE_ROWS["calendar_source"],
            "kind": "person",
            "person_id": "p-parent",
        }
        insert_row(seeded, "calendar_source", row)
        table = schema.calendar_source
        with seeded.connect() as connection:
            stored = connection.scalar(
                select(table.c.person_id).where(
                    table.c.calendar_id == row["calendar_id"]
                )
            )
        assert stored == "p-parent"

    @pytest.mark.parametrize(
        ("table_name", "overrides"),
        ORPHAN_CASES,
        ids=[f"{name}-{next(iter(over))}" for name, over in ORPHAN_CASES],
    )
    def test_orphan_reference_is_rejected(self, seeded, table_name, overrides):
        row = {**BASE_ROWS[table_name], **overrides}
        before = row_count(seeded, table_name)
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            insert_row(seeded, table_name, row)
        assert row_count(seeded, table_name) == before


@pytest.mark.unit
class TestCheckConstraints:
    @pytest.mark.parametrize(
        ("constraint", "table_name", "bad", "good"),
        [pytest.param(*case[1:], id=case[0]) for case in CHECK_CASES],
    )
    def test_violation_rejected_and_neighbour_accepted(
        self, seeded, constraint, table_name, bad, good
    ):
        before = row_count(seeded, table_name)
        with pytest.raises(IntegrityError, match=constraint):
            insert_row(seeded, table_name, {**BASE_ROWS[table_name], **bad})
        assert row_count(seeded, table_name) == before
        insert_row(seeded, table_name, {**BASE_ROWS[table_name], **good})
        assert row_count(seeded, table_name) == before + 1

    @pytest.mark.parametrize(
        ("kind", "owner", "accepted"),
        [
            ("person", "p-parent", True),
            ("person", None, False),
            ("transport", "p-parent", False),
            ("transport", None, True),
            ("school_district", "p-parent", False),
        ],
        ids=[
            "person-with-owner",
            "person-without-owner",
            "transport-with-owner",
            "transport-without-owner",
            "school-district-with-owner",
        ],
    )
    def test_calendar_owner_rule(self, seeded, kind, owner, accepted):
        row = {**BASE_ROWS["calendar_source"], "kind": kind, "person_id": owner}
        before = row_count(seeded, "calendar_source")
        if accepted:
            insert_row(seeded, "calendar_source", row)
            assert row_count(seeded, "calendar_source") == before + 1
        else:
            with pytest.raises(IntegrityError, match=OWNER_CHECK):
                insert_row(seeded, "calendar_source", row)
            assert row_count(seeded, "calendar_source") == before

    def test_every_declared_check_has_a_behaviour_test(self):
        declared = {
            name
            for table_name in PILOT_TABLES
            for name in declared_checks(schema.metadata.tables[table_name])
        }
        covered = {case[1] for case in CHECK_CASES} | {OWNER_CHECK}
        assert covered == declared

    @pytest.mark.parametrize(
        ("table_name", "constraint", "expected"),
        ENUM_CHECKS,
        ids=[constraint for _, constraint, _ in ENUM_CHECKS],
    )
    def test_enum_domain_matches_python_enum(
        self, engine, table_name, constraint, expected
    ):
        migrations_api.upgrade(engine)
        declared = declared_checks(schema.metadata.tables[table_name])[constraint]
        migrated = migrated_checks(inspect(engine), table_name)[constraint]
        assert set(re.findall(r"'([^']*)'", declared)) == expected
        assert set(re.findall(r"'([^']*)'", migrated)) == expected


@pytest.mark.unit
class TestMetadataParity:
    """The migrated database and ``db.schema`` describe the same schema."""

    def test_table_set_is_exact(self, engine):
        migrations_api.upgrade(engine)
        assert table_names(engine) == set(schema.metadata.tables)
        assert table_names(engine) == PILOT_TABLES | {"audit_log"}

    def test_column_types_and_primary_keys_match(self, engine):
        migrations_api.upgrade(engine)
        inspector = inspect(engine)
        dialect = engine.dialect
        for name, table in schema.metadata.tables.items():
            # TypeDecorator.compile renders the wrapped impl, so UtcDateTime
            # compares as the DATETIME the migration creates.
            migrated = {
                column["name"]: column["type"].compile(dialect=dialect)
                for column in inspector.get_columns(name)
            }
            declared = {
                column.name: column.type.compile(dialect=dialect)
                for column in table.columns
            }
            assert migrated == declared, name
            migrated_pk = tuple(
                inspector.get_pk_constraint(name)["constrained_columns"]
            )
            declared_pk = tuple(column.name for column in table.primary_key.columns)
            assert migrated_pk == declared_pk, name

    def test_server_defaults_reproduce_declared_defaults(self, engine):
        migrations_api.upgrade(engine)
        inspector = inspect(engine)
        checked = []
        for name in sorted(PILOT_TABLES):
            reflected = {
                column["name"]: column["default"]
                for column in inspector.get_columns(name)
            }
            for column in schema.metadata.tables[name].columns:
                scalar = column.default is not None and column.default.is_scalar
                expects_default = scalar or column.server_default is not None
                actual = reflected[column.name]
                assert (actual is not None) == expects_default, (name, column.name)
                if not scalar:
                    continue
                expected = column.default.arg
                if isinstance(expected, bool):
                    expected = int(expected)
                assert parse_default(actual) == expected, (name, column.name)
                checked.append((name, column.name))
        assert ("location", "verification_state") in checked
        assert ("source_event", "recurrence_id") in checked

    def test_indexes_and_unique_constraints_match(self, engine):
        migrations_api.upgrade(engine)
        inspector = inspect(engine)
        with engine.connect() as connection:
            for name in sorted(PILOT_TABLES):
                table = schema.metadata.tables[name]
                assert migrated_indexes(connection, name) == declared_indexes(table), (
                    name
                )
                declared_unique = {
                    constraint.name: tuple(column.name for column in constraint.columns)
                    for constraint in table.constraints
                    if isinstance(constraint, UniqueConstraint)
                }
                migrated_unique = {
                    constraint["name"]: tuple(constraint["column_names"])
                    for constraint in inspector.get_unique_constraints(name)
                }
                assert migrated_unique == declared_unique, name

    def test_alias_index_is_case_insensitive_expression(self, engine):
        migrations_api.upgrade(engine)
        with engine.connect() as connection:
            ddl = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = 'ux_alias_kind_alias'"
            ).scalar()
        assert "(kind,lower(alias))" in "".join(ddl.split())
        assert ddl.upper().startswith("CREATE UNIQUE INDEX")

    def test_check_constraints_match(self, engine):
        migrations_api.upgrade(engine)
        inspector = inspect(engine)
        for name in sorted(PILOT_TABLES):
            declared = {
                check: normalize_sql(text)
                for check, text in declared_checks(schema.metadata.tables[name]).items()
            }
            migrated = {
                check: normalize_sql(text)
                for check, text in migrated_checks(inspector, name).items()
            }
            assert migrated == declared, name
