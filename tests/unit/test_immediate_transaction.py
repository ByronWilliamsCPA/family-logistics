"""The shared BEGIN IMMEDIATE write transaction (ADR 0008, D9)."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from family_logistics.core.database import (
    BUSY_TIMEOUT_MS,
    create_sqlite_engine,
    immediate_transaction,
)

pytestmark = pytest.mark.unit


class BoomError(Exception):
    """Raised inside a transaction body to force a rollback."""


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE t (x INTEGER)"))
    yield engine
    engine.dispose()


def rows(engine) -> list[int]:
    with engine.connect() as connection:
        return [r[0] for r in connection.execute(text("SELECT x FROM t ORDER BY x"))]


@contextmanager
def fail_begin_immediate(engine, code: int | None):
    """Make ``BEGIN IMMEDIATE`` raise a driver error carrying ``code``."""

    def fail(_conn, _cursor, statement, parameters, _context, _executemany):
        if statement == "BEGIN IMMEDIATE":
            orig = sqlite3.OperationalError("simulated")
            if code is not None:
                orig.sqlite_errorcode = code
            raise OperationalError(statement, parameters, orig)

    event.listen(engine, "before_cursor_execute", fail)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", fail)


def test_commits_on_success(engine):
    with immediate_transaction(engine) as connection:
        connection.execute(text("INSERT INTO t VALUES (1)"))
    assert rows(engine) == [1]


def test_rolls_back_when_the_body_raises(engine):
    def failing_body() -> None:
        with immediate_transaction(engine) as connection:
            connection.execute(text("INSERT INTO t VALUES (1)"))
            raise BoomError

    with pytest.raises(BoomError):
        failing_body()
    assert rows(engine) == []


def test_the_default_lock_timeout_is_restored(engine):
    with immediate_transaction(engine, lock_timeout_ms=50) as connection:
        inside = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    with engine.connect() as connection:
        after = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    assert inside == 50
    assert after == BUSY_TIMEOUT_MS


def test_the_lock_timeout_is_restored_after_a_failed_begin(engine):
    holder = sqlite3.connect(
        engine.url.database, isolation_level=None, check_same_thread=False
    )
    holder.execute("BEGIN IMMEDIATE")

    def contend() -> None:
        with immediate_transaction(engine, lock_timeout_ms=50):
            pass

    try:
        with pytest.raises(OperationalError):
            contend()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    with engine.connect() as connection:
        after = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    assert after == BUSY_TIMEOUT_MS


def test_two_writers_are_serialized(engine):
    """The second transaction waits for the first, then sees its commit.

    #CRITICAL: Concurrency: a deferred BEGIN would let both read an empty table
    and fail on the upgrade; BEGIN IMMEDIATE queues the second writer instead.
    #VERIFY: this test fails if the helper issued a plain BEGIN.
    """
    entered = threading.Event()
    release = threading.Event()
    seen_by_second: list[int] = []

    def first() -> None:
        with immediate_transaction(engine) as connection:
            connection.execute(text("INSERT INTO t VALUES (1)"))
            entered.set()
            release.wait(timeout=5)

    def second() -> None:
        entered.wait(timeout=5)
        with immediate_transaction(engine) as connection:
            seen_by_second.extend(
                r[0] for r in connection.execute(text("SELECT x FROM t"))
            )
            connection.execute(text("INSERT INTO t VALUES (2)"))

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    assert entered.wait(timeout=5)
    # The second writer is blocked on the lock, so nothing is inserted yet.
    t2.join(timeout=0.3)
    assert t2.is_alive()
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert seen_by_second == [1]
    assert rows(engine) == [1, 2]


def test_a_failed_begin_without_a_translator_propagates_unchanged(engine):
    def contend() -> None:
        with immediate_transaction(engine):
            pass

    with (
        fail_begin_immediate(engine, sqlite3.SQLITE_BUSY),
        pytest.raises(OperationalError) as info,
    ):
        contend()
    assert info.value.orig is not None


def test_a_translator_replaces_the_begin_error(engine):
    def to_runtime(exc: OperationalError) -> None:
        msg = "translated"
        raise RuntimeError(msg) from exc

    def contend() -> None:
        with immediate_transaction(engine, on_begin_error=to_runtime):
            pass

    with (
        fail_begin_immediate(engine, sqlite3.SQLITE_BUSY),
        pytest.raises(RuntimeError, match="translated") as info,
    ):
        contend()
    assert isinstance(info.value.__cause__, OperationalError)


def test_a_translator_that_returns_reraises_the_original(engine):
    calls: list[OperationalError] = []

    def keep(exc: OperationalError) -> None:
        calls.append(exc)

    def contend() -> None:
        with immediate_transaction(engine, on_begin_error=keep):
            pass

    with (
        fail_begin_immediate(engine, sqlite3.SQLITE_READONLY),
        pytest.raises(OperationalError) as info,
    ):
        contend()
    assert calls == [info.value]


def test_the_translator_is_not_consulted_for_body_errors(engine):
    calls: list[OperationalError] = []

    def spy(exc: OperationalError) -> None:
        calls.append(exc)

    def failing_body() -> None:
        with immediate_transaction(engine, on_begin_error=spy):
            raise BoomError

    with pytest.raises(BoomError):
        failing_body()
    assert calls == []
