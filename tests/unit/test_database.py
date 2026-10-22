"""SQLite engine factories: path handling, read-only mode, logged failures."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

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

    def test_journal_mode_fallback_is_logged(self, recorder):
        engine = create_sqlite_engine(Path(":memory:"))
        try:
            assert check_database(engine)
        finally:
            engine.dispose()
        assert recorder.events == [("journal_mode_not_wal", {"journal_mode": "memory"})]


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
