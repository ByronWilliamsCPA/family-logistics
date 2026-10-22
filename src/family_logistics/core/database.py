"""SQLite engine factory (WAL, single writer).

#CRITICAL: Timing: SQLite is safe here only because one container is the only
writer. Never mount the database file into a second writer container, and
keep it on the host's local disk (never NFS), where SQLite locking is reliable.
#ASSUME: Timing: a 5 s busy timeout covers the longest write transaction
(one assessment run's batch). #VERIFY with run-duration metrics in shadow mode;
a "database is locked" error means a second writer or a too-long transaction.
#ASSUME: External resources: the filesystem supports WAL; SQLite silently
keeps another journal mode where it cannot (network or some bind mounts).
#VERIFY: ``_set_pragmas`` reads the mode back and logs ``journal_mode_not_wal``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path
    from sqlite3 import Connection as SQLiteConnection

    from sqlalchemy.engine import Engine
    from sqlalchemy.pool import ConnectionPoolEntry

BUSY_TIMEOUT_MS = 5000
"""How long a connection waits for a lock held by another connection."""

logger = get_logger(__name__)


def _set_pragmas(
    dbapi_connection: SQLiteConnection, _record: ConnectionPoolEntry
) -> None:
    """Set a busy timeout, WAL and foreign keys on each new connection.

    The busy timeout comes first so that switching a new file to WAL waits for
    a concurrent connection instead of failing.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        row = cast(
            "tuple[object, ...] | None",
            cursor.execute("PRAGMA journal_mode=WAL").fetchone(),
        )
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
    mode = None if row is None else row[0]
    if mode != "wal":
        logger.warning("journal_mode_not_wal", journal_mode=str(mode))


def create_sqlite_engine(path: Path) -> Engine:
    """Create an engine for the application database with WAL enabled.

    Args:
        path: Database file path; the parent directory must exist.

    Returns:
        A SQLAlchemy engine.
    """
    # URL.create passes the path through verbatim; an f-string URL would treat
    # "?" or "#" in the path as URL syntax.
    engine = create_engine(URL.create("sqlite", database=str(path)), future=True)
    event.listen(engine, "connect", _set_pragmas)
    return engine


def create_readonly_engine(path: Path) -> Engine:
    """Open a database file read-only, without changing its journal mode.

    Used to inspect files such as restored backups without modifying them.
    The database file itself is never written; for a WAL database SQLite may
    still create empty ``-wal`` and ``-shm`` files next to it.

    Args:
        path: Database file path.

    Returns:
        A SQLAlchemy engine whose connections cannot write.
    """
    # as_uri() percent-encodes the path, so "?", "#" and "%" stay part of the
    # file name in SQLite's URI syntax.
    url = URL.create(
        "sqlite",
        database=path.resolve().as_uri(),
        query={"mode": "ro", "uri": "true"},
    )
    return create_engine(url, future=True)


def check_database(engine: Engine) -> bool:
    """Run a trivial query to prove the database is reachable.

    Args:
        engine: The application engine.

    Returns:
        True when ``SELECT 1`` succeeds.
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        _log_failure("database_unreachable", exc)
        return False
    return True


def directory_problems(path: Path) -> list[str]:
    """Check that the database's directory exists and is writable.

    Args:
        path: Database file path.

    Returns:
        Human-readable problems; empty when the directory is usable. Messages
        name the path only, never configuration values.
    """
    parent = path.parent
    if not parent.is_dir():
        return [f"database directory {parent} does not exist"]
    if not os.access(parent, os.W_OK | os.X_OK):
        return [f"database directory {parent} is not writable by this user"]
    return []


def integrity_ok(engine: Engine) -> bool:
    """Run SQLite's integrity check.

    Args:
        engine: Engine for the database to check.

    Returns:
        True when ``PRAGMA integrity_check`` reports ``ok``.
    """
    try:
        with engine.connect() as connection:
            result: object = connection.execute(text("PRAGMA integrity_check")).scalar()
    except SQLAlchemyError as exc:
        _log_failure("integrity_check_failed", exc)
        return False
    if result != "ok":
        logger.warning("integrity_check_failed", detail=str(result))
    return result == "ok"


def _log_failure(event_name: str, exc: SQLAlchemyError) -> None:
    """Log why a database check failed (the driver message, no statement)."""
    cause = exc.orig if isinstance(exc, DBAPIError) else exc
    logger.warning(event_name, error=type(cause).__name__, detail=str(cause))
