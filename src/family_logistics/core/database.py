"""SQLite engine factory (WAL, single writer).

#CRITICAL: Timing: SQLite is safe here only because one container is the only
writer. Never mount the database file into a second writer container, and
keep it on the host's local disk (never NFS), where SQLite locking is reliable.
#VERIFY: `serve` runs uvicorn with workers=1, docker-compose.yml runs one
container, and docs/runbook.md says never to run two instances on one file.
#ASSUME: Timing: a 5 s busy timeout covers the longest write transaction
(one assessment run's batch). #VERIFY with run-duration metrics in shadow mode;
a "database is locked" error means a second writer or a too-long transaction.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from pathlib import Path
    from sqlite3 import Connection as SQLiteConnection

    from sqlalchemy.engine import Engine
    from sqlalchemy.pool import ConnectionPoolEntry


def _set_pragmas(
    dbapi_connection: SQLiteConnection, _record: ConnectionPoolEntry
) -> None:
    """Enable WAL, foreign keys and a busy timeout on each new connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def create_sqlite_engine(path: Path) -> Engine:
    """Create an engine for the application database with WAL enabled.

    Args:
        path: Database file path; the parent directory must exist.

    Returns:
        A SQLAlchemy engine.
    """
    engine = create_engine(f"sqlite:///{path}", future=True)
    event.listen(engine, "connect", _set_pragmas)
    return engine


def create_readonly_engine(path: Path) -> Engine:
    """Open a database file read-only, without changing its journal mode.

    Used to inspect files such as restored backups without modifying them.

    Args:
        path: Database file path.

    Returns:
        A SQLAlchemy engine whose connections cannot write.
    """
    return create_engine(
        f"sqlite:///file:{path.resolve()}?mode=ro&uri=true", future=True
    )


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
    except SQLAlchemyError:
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
    except SQLAlchemyError:
        return False
    return result == "ok"
