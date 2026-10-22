"""Append-only audit log: hashes only, no updates or deletes."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.audit import AuditRepository, state_hash

ROW_ONE = (
    "audit_log (id, recorded_at, actor, object_ref, action)"
    " VALUES (1, '2026-01-01 00:00:00', 'intruder', 'x:1', 'create')"
)


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    yield engine
    engine.dispose()


@pytest.mark.unit
class TestAudit:
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
        assert entry.prior_hash == state_hash(prior)
        assert entry.new_hash == state_hash(new)
        with engine.connect() as connection:
            dump = "".join(
                str(row) for row in connection.execute(text("SELECT * FROM audit_log"))
            )
        assert "address" not in dump
        assert "Music Studio" not in dump
        [stored] = repo.entries_for("trip:abc")
        assert (stored.actor, stored.action, stored.correlation_id) == (
            "morgan",
            "update",
            "run-1",
        )
        assert stored.prior_hash == entry.prior_hash

    def test_create_has_no_prior_hash(self, engine):
        entry = AuditRepository(engine).record(
            actor="system", object_ref="person:p1", action="create", new_state={"a": 1}
        )
        assert entry.prior_hash is None
        assert entry.new_hash is not None

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE audit_log SET actor='x'",
            "DELETE FROM audit_log",
            f"INSERT OR REPLACE INTO {ROW_ONE}",
            f"REPLACE INTO {ROW_ONE}",
            f"INSERT INTO {ROW_ONE} ON CONFLICT(id) DO UPDATE SET actor = 'x'",
        ],
        ids=["update", "delete", "insert-or-replace", "replace-into", "upsert"],
    )
    def test_rows_cannot_change(self, engine, statement):
        AuditRepository(engine).record(
            actor="system", object_ref="x:1", action="create"
        )
        with engine.begin() as connection:
            query = text(statement)
            with pytest.raises(DatabaseError, match="append-only"):
                connection.execute(query)

    @pytest.mark.parametrize("field", ["actor", "object_ref", "action"])
    def test_reference_fields_validated(self, engine, field):
        values = {"actor": "a", "object_ref": "b", "action": "c"}
        repository = AuditRepository(engine)
        values[field] = ""
        with pytest.raises(ValueError, match=field):
            repository.record(**values)
        values[field] = "x" * 200
        with pytest.raises(ValueError, match=field):
            repository.record(**values)

    def test_rows_survive_refused_replace(self, engine):
        repository = AuditRepository(engine)
        repository.record(actor="system", object_ref="x:1", action="create")
        statement = text(f"INSERT OR REPLACE INTO {ROW_ONE}")
        with engine.connect() as connection, pytest.raises(DatabaseError):
            connection.execute(statement)
        [stored] = repository.entries_for("x:1")
        assert stored.actor == "system"

    def test_new_ids_still_insert(self, engine):
        repository = AuditRepository(engine)
        repository.record(actor="system", object_ref="x:1", action="create")
        repository.record(actor="system", object_ref="x:1", action="update")
        actions = [entry.action for entry in repository.entries_for("x:1")]
        assert actions == ["create", "update"]

    def test_hash_is_canonical(self):
        assert state_hash({"a": 1, "b": 2}) == state_hash({"b": 2, "a": 1})
        assert state_hash(None) is None

    def test_hash_is_pinned(self):
        # SHA-256 of the canonical JSON '{"a":1,"b":"Alex"}', computed
        # independently of state_hash so a change of canonical form fails here.
        expected = "cedaa73517674e9aeb8830412088ce0b22552789c3bb369a74bf119c109274ce"
        assert state_hash({"b": "Alex", "a": 1}) == expected
