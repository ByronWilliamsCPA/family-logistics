"""Append-only audit log: hashes only, no updates or deletes."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.audit import AuditRepository, state_hash


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
        "statement", ["UPDATE audit_log SET actor='x'", "DELETE FROM audit_log"]
    )
    def test_rows_cannot_change(self, engine, statement):
        AuditRepository(engine).record(
            actor="system", object_ref="x:1", action="create"
        )
        with (
            pytest.raises(DatabaseError, match="append-only"),
            engine.begin() as connection,
        ):
            connection.execute(text(statement))

    @pytest.mark.parametrize("field", ["actor", "object_ref", "action"])
    def test_reference_fields_validated(self, engine, field):
        values = {"actor": "a", "object_ref": "b", "action": "c"}
        values[field] = ""
        with pytest.raises(ValueError, match=field):
            AuditRepository(engine).record(**values)
        values[field] = "x" * 200
        with pytest.raises(ValueError, match=field):
            AuditRepository(engine).record(**values)

    def test_hash_is_canonical(self):
        assert state_hash({"a": 1, "b": 2}) == state_hash({"b": 2, "a": 1})
        assert state_hash(None) is None
