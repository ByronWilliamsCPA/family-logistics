"""Audit log: hashes only; no update, no overwrite, delete only after 365 days."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

from family_logistics.core import exceptions
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import audit, migrations_api
from family_logistics.db.audit import AuditEntry, AuditRepository, state_hash
from family_logistics.domain.models import SensitiveStr

INSERT = {
    "plain": "INSERT INTO audit_log (id, recorded_at, actor, object_ref, action) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create')",
    "OR REPLACE": "INSERT OR REPLACE INTO audit_log "
    "(id, recorded_at, actor, object_ref, action) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create')",
    "OR IGNORE": "INSERT OR IGNORE INTO audit_log "
    "(id, recorded_at, actor, object_ref, action) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create')",
    "OR ROLLBACK": "INSERT OR ROLLBACK INTO audit_log "
    "(id, recorded_at, actor, object_ref, action) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create')",
    "REPLACE": "REPLACE INTO audit_log (id, recorded_at, actor, object_ref, action) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create')",
    "UPSERT": "INSERT INTO audit_log (id, recorded_at, actor, object_ref, action) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create') "
    "ON CONFLICT(id) DO UPDATE SET actor = 'forger'",
}
INSERT_FULL = text(
    "INSERT INTO audit_log (id, recorded_at, actor, object_ref, action, "
    "prior_hash, new_hash, correlation_id) VALUES (:id, :recorded_at, :actor, "
    ":object_ref, :action, :prior_hash, :new_hash, :correlation_id)"
)
NOW_TEXT = "2026-10-22 12:00:00.000000"
OLD_TEXT = "2020-01-01 00:00:00.000000"
HEX64 = "a" * 64


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "fl.db"
    engine = create_sqlite_engine(path)
    migrations_api.upgrade(engine)
    engine.dispose()
    return path


@pytest.fixture
def engine(database):
    engine = create_sqlite_engine(database)
    yield engine
    engine.dispose()


def _rows(engine) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text("SELECT id, actor FROM audit_log ORDER BY id")
            )
        ]


@pytest.mark.unit
class TestRecord:
    def test_records_hashes_not_state(self, engine):
        repo = AuditRepository(engine)
        prior = {"destination": "Passenger E home address"}
        new = {"destination": "Music Studio"}
        entry = repo.record(
            actor="morgan",
            object_ref="trip:abc",
            action="update",
            prior_state=prior,
            new_state=new,
            correlation_id="run-1",
        )
        with engine.connect() as connection:
            dump = "".join(
                str(row) for row in connection.execute(text("SELECT * FROM audit_log"))
            )
        assert "address" not in dump
        assert "Music Studio" not in dump
        [stored] = repo.entries_for("trip:abc")
        assert stored == entry
        assert entry.prior_hash != entry.new_hash
        assert entry.recorded_at.tzinfo is UTC

    def test_create_has_no_prior_hash(self, engine):
        entry = AuditRepository(engine).record(
            actor="system", object_ref="person:p1", action="create", new_state={"a": 1}
        )
        assert entry.prior_hash is None
        assert entry.new_hash == (
            "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
        )

    def test_entries_for_is_oldest_first_and_filtered(self, engine):
        repo = AuditRepository(engine)
        for action in ("create", "update", "retire"):
            repo.record(actor="system", object_ref="trip:1", action=action)
        repo.record(actor="system", object_ref="trip:2", action="create")
        assert [e.action for e in repo.entries_for("trip:1")] == [
            "create",
            "update",
            "retire",
        ]
        assert repo.entries_for("trip:unknown") == []

    @pytest.mark.parametrize(
        ("field", "limit"),
        [("actor", 64), ("object_ref", 128), ("action", 32), ("correlation_id", 64)],
    )
    def test_reference_fields_match_column_widths(self, engine, field, limit):
        repository = AuditRepository(engine)
        values = {"actor": "a", "object_ref": "b", "action": "c", field: "x" * limit}
        assert getattr(repository.record(**values), field) == "x" * limit
        for bad in ("", "x" * (limit + 1)):
            values[field] = bad
            with pytest.raises(
                exceptions.ValidationError, match=f"{field} must be 1-{limit}"
            ):
                repository.record(**values)

    def test_unreadable_timestamp_is_rejected(self):
        with pytest.raises(exceptions.DatabaseError, match="recorded_at"):
            audit._entry_from_row(
                {
                    "recorded_at": "yesterday",
                    "actor": "a",
                    "object_ref": "b",
                    "action": "c",
                    "prior_hash": None,
                    "new_hash": None,
                    "correlation_id": None,
                }
            )


@pytest.mark.unit
class TestAuditEntryInvariants:
    def test_rejects_non_hash_values(self):
        with pytest.raises(exceptions.ValidationError, match="prior_hash"):
            AuditEntry(
                recorded_at=datetime.now(UTC),
                actor="a",
                object_ref="b",
                action="c",
                prior_hash="Jane Doe lives at 12 Example St",
                new_hash=None,
                correlation_id=None,
            )

    def test_rejects_naive_timestamp(self):
        with pytest.raises(exceptions.ValidationError, match="timezone"):
            AuditEntry(
                recorded_at=datetime.now(UTC).replace(tzinfo=None),
                actor="a",
                object_ref="b",
                action="c",
                prior_hash=HEX64,
                new_hash=None,
                correlation_id=None,
            )


@pytest.mark.unit
class TestStateHash:
    """Known-answer vectors: SHA-256 of the canonical JSON bytes, computed with
    sha256sum outside this code (non-ASCII is escaped, for example ``\\u00e9``).
    """

    @pytest.mark.parametrize(
        ("state", "digest"),
        [
            (
                {"b": 2, "a": 1},
                "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
            ),
            (
                {"t": time(8, 15), "d": date(2026, 10, 21)},
                "982a93b321a987e5881321204aa9042538a3e2bc674f55643de84787573dd66e",
            ),
            (
                {"seats": [1, 2], "name": "Café"},
                "2f9f5baea7d52a220d06eb9bb806df98d173421d3d1fa4074cb7cbaa4d480cbf",
            ),
        ],
    )
    def test_known_answer(self, state, digest):
        assert state_hash(state) == digest

    def test_hash_is_pinned(self):
        # SHA-256 of the canonical JSON '{"a":1,"b":"Alex"}', computed
        # independently of state_hash so a change of canonical form fails here.
        expected = "cedaa73517674e9aeb8830412088ce0b22552789c3bb369a74bf119c109274ce"  # pragma: allowlist secret
        assert state_hash({"b": "Alex", "a": 1}) == expected

    def test_no_state_has_no_hash(self):
        assert state_hash(None) is None

    @pytest.mark.parametrize(
        "value",
        [
            {"alpha", "beta"},
            object(),
            SensitiveStr("12 Example St"),
            float("nan"),
        ],
        ids=["set", "object", "sensitive", "nan"],
    )
    def test_non_canonical_values_are_refused(self, value):
        with pytest.raises(exceptions.ValidationError, match="canonically"):
            state_hash({"v": value})

    def test_protected_values_never_hash_as_their_mask(self):
        # A masked value would make two different addresses hash alike.
        with pytest.raises(exceptions.ValidationError, match="SensitiveStr"):
            state_hash({"address": SensitiveStr("12 Example St")})


@pytest.mark.unit
class TestAppendOnly:
    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE audit_log SET actor='x'",
            "UPDATE audit_log SET recorded_at='2000-01-01 00:00:00.000000'",
            "DELETE FROM audit_log",
            "DELETE FROM audit_log WHERE id = 1",
        ],
    )
    def test_rows_cannot_change(self, engine, statement):
        AuditRepository(engine).record(
            actor="system", object_ref="x:1", action="create"
        )
        with engine.begin() as connection:
            query = text(statement)
            with pytest.raises(DatabaseError, match="append-only"):
                connection.execute(query)
        assert _rows(engine) == [(1, "system")]

    @pytest.mark.parametrize("verb", ["OR REPLACE", "OR IGNORE", "OR ROLLBACK"])
    def test_existing_row_cannot_be_overwritten_via_sqlalchemy(self, engine, verb):
        AuditRepository(engine).record(
            actor="system", object_ref="x:1", action="create"
        )
        with engine.begin() as connection:
            statement = text(INSERT[verb])
            params = {"id": 1, "recorded_at": NOW_TEXT}
            with pytest.raises(DatabaseError, match="append-only"):
                connection.execute(statement, params)
        assert _rows(engine) == [(1, "system")]

    @pytest.mark.parametrize("verb", ["OR REPLACE", "REPLACE", "UPSERT"])
    def test_existing_row_cannot_be_overwritten_via_raw_sqlite3(
        self, database, engine, verb
    ):
        AuditRepository(engine).record(
            actor="system", object_ref="x:1", action="create"
        )
        engine.dispose()
        # A bare stdlib connection: no SQLAlchemy pragmas, recursive_triggers off.
        raw = sqlite3.connect(database)
        try:
            assert raw.execute("PRAGMA recursive_triggers").fetchone() == (0,)
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                raw.execute(INSERT[verb], {"id": 1, "recorded_at": NOW_TEXT})
            raw.rollback()
            rows = raw.execute("SELECT id, actor FROM audit_log").fetchall()
        finally:
            raw.close()
        assert rows == [(1, "system")]

    def test_new_rows_still_append(self, engine):
        repo = AuditRepository(engine)
        repo.record(actor="system", object_ref="x:1", action="create")
        with engine.begin() as connection:
            connection.execute(
                text(INSERT["OR REPLACE"]), {"id": 2, "recorded_at": NOW_TEXT}
            )
        repo.record(actor="system", object_ref="x:1", action="update")
        assert _rows(engine) == [(1, "system"), (2, "forger"), (3, "system")]

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("id", 0),
            ("recorded_at", "not a time"),
            ("prior_hash", "Jane Doe lives at 12 Example St"),
            ("new_hash", "A" * 64),
            ("actor", "a" * 65),
            ("object_ref", ""),
            ("action", "b" * 33),
            ("correlation_id", ""),
        ],
    )
    def test_check_constraints_reject_bad_rows(self, engine, column, value):
        row: dict[str, object] = {
            "id": None,
            "recorded_at": NOW_TEXT,
            "actor": "a",
            "object_ref": "x:1",
            "action": "create",
            "prior_hash": None,
            "new_hash": HEX64,
            "correlation_id": None,
        }
        with engine.begin() as connection:
            connection.execute(INSERT_FULL, row)
        row[column] = value
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(INSERT_FULL, row)


@pytest.mark.unit
class TestRetention:
    """The privacy spec purges audit rows after 365 days; younger rows stay."""

    def _insert(self, engine, row_id: int, recorded_at: str) -> None:
        with engine.begin() as connection:
            connection.execute(
                text(INSERT["plain"]), {"id": row_id, "recorded_at": recorded_at}
            )

    def test_rows_older_than_365_days_can_be_purged(self, engine):
        recent = datetime.now(UTC) - timedelta(days=364)
        self._insert(engine, 1, OLD_TEXT)
        self._insert(engine, 2, recent.strftime("%Y-%m-%d %H:%M:%S.%f"))
        with engine.begin() as connection:
            purged = connection.execute(
                text(
                    "DELETE FROM audit_log "
                    "WHERE julianday(recorded_at) <= julianday('now', '-365 days')"
                )
            )
        assert purged.rowcount == 1
        assert _rows(engine) == [(2, "forger")]

    def test_row_younger_than_365_days_cannot_be_deleted(self, engine):
        recent = datetime.now(UTC) - timedelta(days=364)
        self._insert(engine, 1, recent.strftime("%Y-%m-%d %H:%M:%S.%f"))
        with engine.begin() as connection:
            statement = text("DELETE FROM audit_log WHERE id = 1")
            with pytest.raises(DatabaseError, match="append-only"):
                connection.execute(statement)
        assert _rows(engine) == [(1, "forger")]
