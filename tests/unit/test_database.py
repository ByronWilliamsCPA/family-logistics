"""SQLite engine factories: path handling, read-only mode, logged failures."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    MetaData,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.exc import IntegrityError, OperationalError

from family_logistics.core import database
from family_logistics.core.database import (
    check_database,
    create_readonly_engine,
    create_sqlite_engine,
    integrity_ok,
)

ODD_NAME = "odd dir?#%41/a?b#c%41 x.db"


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


@pytest.fixture
def recorder(monkeypatch):
    logger = RecordingLogger()
    monkeypatch.setattr(database, "logger", logger)
    return logger


@pytest.mark.unit
class TestEngines:
    def test_url_characters_in_path_name_the_same_file(self, tmp_path):
        path = tmp_path / ODD_NAME
        path.parent.mkdir()
        writer = create_sqlite_engine(path)
        with writer.begin() as connection:
            connection.execute(text("CREATE TABLE t (x INTEGER)"))
            connection.execute(text("INSERT INTO t VALUES (7)"))
        writer.dispose()
        assert path.is_file()
        reader = create_readonly_engine(path)
        try:
            with reader.connect() as connection:
                assert connection.execute(text("SELECT x FROM t")).scalar() == 7
        finally:
            reader.dispose()

    def test_readonly_engine_cannot_write(self, tmp_path):
        path = tmp_path / "fl.db"
        writer = create_sqlite_engine(path)
        with writer.begin() as connection:
            connection.execute(text("CREATE TABLE t (x INTEGER)"))
        writer.dispose()
        before = path.read_bytes()
        reader = create_readonly_engine(path)
        try:
            with reader.connect() as connection:
                statement = text("INSERT INTO t VALUES (1)")
                with pytest.raises(OperationalError, match="readonly"):
                    connection.execute(statement)
        finally:
            reader.dispose()
        assert path.read_bytes() == before

    def test_writer_uses_wal_and_busy_timeout(self, tmp_path, recorder):
        engine = create_sqlite_engine(tmp_path / "fl.db")
        try:
            with engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == (
                    "wal"
                )
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == (
                    database.BUSY_TIMEOUT_MS
                )
        finally:
            engine.dispose()
        assert recorder.events == []

    def test_wal_switch_waits_for_a_concurrent_writer(self, tmp_path, recorder):
        # SQLite fails a journal-mode change at once (no busy handler) while
        # another connection holds the write lock, as when several containers
        # start on a fresh volume and the first one is migrating.
        path = tmp_path / "fl.db"
        holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        holder.execute("CREATE TABLE t (x INTEGER)")
        holder.execute("BEGIN IMMEDIATE")
        release = threading.Timer(0.3, holder.execute, args=("ROLLBACK",))
        release.start()
        engine = create_sqlite_engine(path)
        try:
            with engine.connect() as connection:
                mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        finally:
            engine.dispose()
            release.join()
            holder.close()
        assert mode == "wal"
        assert recorder.events == []

    def test_journal_mode_fallback_is_logged(self, recorder):
        engine = create_sqlite_engine(Path(":memory:"))
        try:
            assert check_database(engine)
        finally:
            engine.dispose()
        assert recorder.events == [("journal_mode_not_wal", {"journal_mode": "memory"})]

    @pytest.mark.parametrize("hidden", [True, False], ids=["factory", "default"])
    def test_rejected_value_stays_out_of_error_text(self, tmp_path, hidden):
        # A database-level rejection (here a CHECK violation, as a raw plaintext
        # write to a ciphertext column would be) raises an error that lists the
        # bound parameters by default. The default engine is the control that
        # proves the factory engines are what keeps the value out of logs and
        # tracebacks.
        plaintext = "1 Example Way, Sample Town"
        probe = Table(
            "probe",
            MetaData(),
            Column("note", Text, CheckConstraint("length(note) < 5")),
        )
        path = tmp_path / "probe.db"
        engine = (
            create_sqlite_engine(path)
            if hidden
            else create_engine(f"sqlite:///{path}", future=True)
        )
        with engine.begin() as connection:
            probe.create(connection)
        with pytest.raises(IntegrityError) as excinfo, engine.begin() as connection:
            connection.execute(probe.insert().values(note=plaintext))
        assert (plaintext in str(excinfo.value)) is not hidden
        engine.dispose()

    def test_readonly_engine_hides_parameters(self, tmp_path):
        path = tmp_path / "ro.db"
        create_sqlite_engine(path).dispose()
        engine = create_readonly_engine(path)
        assert engine.hide_parameters is True
        engine.dispose()


@pytest.mark.unit
class TestChecks:
    def test_failed_checks_log_the_cause(self, tmp_path, recorder):
        path = tmp_path / "corrupt.db"
        path.write_bytes(b"not a database" * 100)
        engine = create_readonly_engine(path)
        try:
            assert not check_database(engine)
            assert not integrity_ok(engine)
        finally:
            engine.dispose()
        assert [event for event, _ in recorder.events] == [
            "database_unreachable",
            "integrity_check_failed",
        ]
        assert recorder.events[0][1]["error"] == "DatabaseError"
        assert "not a database" in str(recorder.events[0][1]["detail"])
