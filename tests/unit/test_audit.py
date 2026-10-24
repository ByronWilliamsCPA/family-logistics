"""Audit log: keyed hashes only; no update, no overwrite, delete only after 365 days."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import re
import sqlite3
from datetime import UTC, date, datetime, time, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

from family_logistics.core import exceptions
from family_logistics.core.config import Settings
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import audit, migrations_api
from family_logistics.db.audit import AuditEntry, AuditRepository, state_hash
from family_logistics.domain.models import SensitiveStr

INSERT = {
    "plain": "INSERT INTO audit_log (id, recorded_at, actor, object_ref, action, hash_key_version) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create', 1)",
    "OR REPLACE": "INSERT OR REPLACE INTO audit_log "
    "(id, recorded_at, actor, object_ref, action, hash_key_version) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create', 1)",
    "OR IGNORE": "INSERT OR IGNORE INTO audit_log "
    "(id, recorded_at, actor, object_ref, action, hash_key_version) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create', 1)",
    "OR ROLLBACK": "INSERT OR ROLLBACK INTO audit_log "
    "(id, recorded_at, actor, object_ref, action, hash_key_version) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create', 1)",
    "REPLACE": "REPLACE INTO audit_log (id, recorded_at, actor, object_ref, action, hash_key_version) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create', 1)",
    "UPSERT": "INSERT INTO audit_log (id, recorded_at, actor, object_ref, action, hash_key_version) "
    "VALUES (:id, :recorded_at, 'forger', 'x:1', 'create', 1) "
    "ON CONFLICT(id) DO UPDATE SET actor = 'forger'",
}
INSERT_FULL = text(
    "INSERT INTO audit_log (id, recorded_at, actor, object_ref, action, "
    "prior_hash, new_hash, correlation_id, hash_key_version) VALUES (:id, "
    ":recorded_at, :actor, :object_ref, :action, :prior_hash, :new_hash, "
    ":correlation_id, :hash_key_version)"
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


def _versions(engine) -> list[int]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text("SELECT hash_key_version FROM audit_log ORDER BY id")
            )
        ]


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
    def test_records_hashes_not_state(self, audit_key, engine):
        repo = AuditRepository(engine, key=audit_key)
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

    def test_create_has_no_prior_hash(self, audit_key, engine):
        entry = AuditRepository(engine, key=audit_key).record(
            actor="system", object_ref="person:p1", action="create", new_state={"a": 1}
        )
        assert entry.prior_hash is None
        # Computed here from the raw key bytes, independently of state_hash.
        expected = hmac.new(bytes(range(32)), b'{"a":1}', hashlib.sha256).hexdigest()
        assert entry.new_hash == expected

    def test_entries_for_is_oldest_first_and_filtered(self, audit_key, engine):
        repo = AuditRepository(engine, key=audit_key)
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
    def test_reference_fields_match_column_widths(
        self, audit_key, engine, field, limit
    ):
        repository = AuditRepository(engine, key=audit_key)
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
                hash_key_version=1,
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
                hash_key_version=1,
            )

    @pytest.mark.parametrize("version", [0, -1])
    def test_rejects_key_version_below_one(self, version):
        with pytest.raises(exceptions.ValidationError, match="hash_key_version"):
            AuditEntry(
                recorded_at=datetime.now(UTC),
                actor="a",
                object_ref="b",
                action="c",
                prior_hash=None,
                new_hash=HEX64,
                correlation_id=None,
                hash_key_version=version,
            )


def _expected_digest(key: bytes, canonical: bytes) -> str:
    """HMAC-SHA256 hex computed with the standard library, outside state_hash."""
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


@pytest.mark.unit
class TestStateHash:
    """Known answers: HMAC-SHA256 over the canonical JSON bytes, computed in the
    test from the raw key bytes (non-ASCII is escaped, for example ``\\u00e9``).
    """

    @pytest.mark.parametrize(
        ("state", "canonical"),
        [
            ({"b": 2, "a": 1}, b'{"a":1,"b":2}'),
            (
                {"t": time(8, 15), "d": date(2026, 10, 21)},
                b'{"d":"2026-10-21","t":"08:15:00"}',
            ),
            (
                {"seats": [1, 2], "name": "Caf\u00e9"},
                b'{"name":"Caf\\u00e9","seats":[1,2]}',
            ),
            ({"b": "Alex", "a": 1}, b'{"a":1,"b":"Alex"}'),
        ],
    )
    def test_known_answer(self, audit_key, state, canonical):
        assert state_hash(state, key=audit_key) == _expected_digest(
            bytes(range(32)), canonical
        )

    def test_same_input_and_key_give_the_same_hash(self, audit_key, audit_key_text):
        state = {"b": "Alex", "a": [1, 2]}
        same_key = SecretStr(audit_key_text)
        assert state_hash(state, key=audit_key) == state_hash(state, key=same_key)

    def test_key_order_does_not_matter(self, audit_key):
        assert state_hash({"a": 1, "b": 2}, key=audit_key) == state_hash(
            {"b": 2, "a": 1}, key=audit_key
        )

    def test_a_different_key_gives_a_different_hash(self, audit_key, other_audit_key):
        state = {"destination": "Music Studio"}
        assert state_hash(state, key=audit_key) != state_hash(
            state, key=other_audit_key
        )

    def test_it_differs_from_plain_sha256(self, audit_key):
        state = {"b": "Alex", "a": 1}
        plain = hashlib.sha256(b'{"a":1,"b":"Alex"}').hexdigest()
        assert state_hash(state, key=audit_key) != plain

    def test_output_is_64_lowercase_hex(self, audit_key):
        digest = state_hash({"a": 1}, key=audit_key)
        assert digest is not None
        assert re.fullmatch(r"[0-9a-f]{64}", digest)

    def test_no_state_has_no_hash(self, audit_key):
        assert state_hash(None, key=audit_key) is None

    def test_key_is_required_and_keyword_only(self):
        parameter = inspect.signature(state_hash).parameters["key"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "not-a-valid-SECRET-audit-key",
            "k\u00e9y" * 12,
            base64.urlsafe_b64encode(b"short").decode(),
            base64.urlsafe_b64encode(bytes(range(33))).decode(),
        ],
        ids=["empty", "text", "non-ascii", "short", "long"],
    )
    @pytest.mark.parametrize("state", [{"a": 1}, None], ids=["state", "no-state"])
    def test_a_bad_key_is_refused_without_echo(self, bad, state):
        with pytest.raises(exceptions.ConfigurationError) as caught:
            state_hash(state, key=SecretStr(bad))
        assert "FL_AUDIT_KEY" in str(caught.value)
        assert bad == "" or bad not in str(caught.value)
        assert caught.value.__cause__ is None

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
    def test_non_canonical_values_are_refused(self, audit_key, value):
        with pytest.raises(exceptions.ValidationError, match="canonically"):
            state_hash({"v": value}, key=audit_key)

    def test_protected_values_never_hash_as_their_mask(self, audit_key):
        # A masked value would make two different addresses hash alike.
        with pytest.raises(exceptions.ValidationError, match="SensitiveStr"):
            state_hash({"address": SensitiveStr("12 Example St")}, key=audit_key)


@pytest.mark.unit
class TestKeyVersion:
    def test_default_version_is_persisted(self, engine, audit_key):
        entry = AuditRepository(engine, key=audit_key).record(
            actor="system", object_ref="x:1", action="create", new_state={"a": 1}
        )
        assert entry.hash_key_version == 1
        assert _versions(engine) == [1]

    def test_configured_version_is_persisted_and_read_back(self, engine, audit_key):
        repository = AuditRepository(engine, key=audit_key, key_version=7)
        entry = repository.record(actor="system", object_ref="x:1", action="update")
        assert entry.hash_key_version == 7
        assert _versions(engine) == [7]
        assert repository.entries_for("x:1") == [entry]

    def test_from_settings_uses_the_key_and_version(self, engine, audit_key_text):
        settings = Settings.model_validate(
            {"audit_key": audit_key_text, "audit_key_version": 4}
        )
        entry = AuditRepository.from_settings(engine, settings).record(
            actor="system", object_ref="x:1", action="create", new_state={"a": 1}
        )
        assert entry.hash_key_version == 4
        expected = _expected_digest(bytes(range(32)), b'{"a":1}')
        assert entry.new_hash == expected

    @pytest.mark.parametrize("version", [0, -3])
    def test_a_version_below_one_is_refused(self, engine, audit_key, version):
        with pytest.raises(exceptions.ConfigurationError, match="version"):
            AuditRepository(engine, key=audit_key, key_version=version)

    def test_rotation_is_visible_per_row(self, engine, audit_key, other_audit_key):
        old = AuditRepository(engine, key=audit_key, key_version=1)
        new = AuditRepository(engine, key=other_audit_key, key_version=2)
        state = {"a": 1}
        first = old.record(
            actor="s", object_ref="x:1", action="create", new_state=state
        )
        second = new.record(
            actor="s", object_ref="x:1", action="update", new_state=state
        )
        assert first.new_hash != second.new_hash
        assert [e.hash_key_version for e in new.entries_for("x:1")] == [1, 2]

    def test_a_row_without_a_version_cannot_be_inserted(self, engine):
        statement = text(
            "INSERT INTO audit_log (recorded_at, actor, object_ref, action) "
            "VALUES (:recorded_at, 'a', 'x:1', 'create')"
        )
        with (
            engine.begin() as connection,
            pytest.raises(IntegrityError, match="NOT NULL"),
        ):
            connection.execute(statement, {"recorded_at": NOW_TEXT})

    def test_a_non_integer_stored_version_is_a_database_error(self):
        with pytest.raises(exceptions.DatabaseError, match="hash_key_version"):
            audit._key_version("1")
        flag = True  # a bool is an int in Python but never a stored version
        with pytest.raises(exceptions.DatabaseError, match="hash_key_version"):
            audit._key_version(flag)


@pytest.mark.unit
class TestKeyRequired:
    def test_record_without_a_key_raises_and_writes_nothing(self, engine):
        repository = AuditRepository(engine, key=None)
        with pytest.raises(exceptions.ConfigurationError, match="FL_AUDIT_KEY"):
            repository.record(
                actor="system", object_ref="x:1", action="create", new_state={"a": 1}
            )
        assert _rows(engine) == []

    def test_from_settings_without_a_key_cannot_record(self, engine):
        repository = AuditRepository.from_settings(engine, Settings())
        with pytest.raises(exceptions.ConfigurationError, match="FL_AUDIT_KEY"):
            repository.record(actor="system", object_ref="x:1", action="create")
        assert _rows(engine) == []

    def test_reading_needs_no_key(self, engine, audit_key):
        AuditRepository(engine, key=audit_key).record(
            actor="system", object_ref="x:1", action="create"
        )
        [entry] = AuditRepository(engine, key=None).entries_for("x:1")
        assert entry.action == "create"

    def test_a_malformed_key_writes_nothing(self, engine):
        repository = AuditRepository(engine, key=SecretStr("short"))
        with pytest.raises(exceptions.ConfigurationError):
            repository.record(
                actor="system", object_ref="x:1", action="create", new_state={"a": 1}
            )
        assert _rows(engine) == []

    def test_a_stored_row_holds_no_key_material_or_state(self, engine, audit_key):
        AuditRepository(engine, key=audit_key).record(
            actor="system",
            object_ref="x:1",
            action="create",
            new_state={"address": "12 Example St"},
        )
        with engine.connect() as connection:
            dump = "".join(
                str(row) for row in connection.execute(text("SELECT * FROM audit_log"))
            )
        assert audit_key.get_secret_value() not in dump
        assert "Example" not in dump


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
    def test_rows_cannot_change(self, audit_key, engine, statement):
        AuditRepository(engine, key=audit_key).record(
            actor="system", object_ref="x:1", action="create"
        )
        with engine.begin() as connection:
            query = text(statement)
            with pytest.raises(DatabaseError, match="append-only"):
                connection.execute(query)
        assert _rows(engine) == [(1, "system")]

    @pytest.mark.parametrize("verb", ["OR REPLACE", "OR IGNORE", "OR ROLLBACK"])
    def test_existing_row_cannot_be_overwritten_via_sqlalchemy(
        self, audit_key, engine, verb
    ):
        AuditRepository(engine, key=audit_key).record(
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
        self, audit_key, database, engine, verb
    ):
        AuditRepository(engine, key=audit_key).record(
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

    def test_new_rows_still_append(self, audit_key, engine):
        repo = AuditRepository(engine, key=audit_key)
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
            ("hash_key_version", 0),
            ("hash_key_version", -1),
            ("hash_key_version", None),
            ("hash_key_version", "abc"),
            ("hash_key_version", 1.5),
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
            "hash_key_version": 1,
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
