"""SQLite engine factory (WAL, single writer).

#CRITICAL: Timing: SQLite is safe here only because one container is the only
writer. Never mount the database file into a second writer container, and
keep it on the host's local disk (never NFS), where SQLite locking is reliable.
"""

from __future__ import annotations

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
