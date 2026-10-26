"""Run and inspect schema migrations without an alembic.ini.

The application owns its schema: ``serve`` refuses to start on a database
that is not at the head revision unless started with ``--upgrade-db``, and
``family-logistics migrate`` upgrades (or downgrades) explicitly.

Every upgrade or downgrade runs in one SQLite transaction opened with
``BEGIN IMMEDIATE``:

- it is atomic: SQLite DDL is transactional once a transaction is open, so a
  failed or interrupted migration rolls back completely (the sqlite3 module's
  legacy mode would otherwise autocommit each CREATE or DROP);
- it is serialized: ``BEGIN IMMEDIATE`` takes the database's write lock, so a
  second migrator (another container on the same volume) waits, then finds the
  schema already at head and does nothing;
- it refuses a schema it does not know (written by a newer release), so an
  older image never runs against it.

#CRITICAL: Data integrity: the default container command runs
``serve --upgrade-db``, so every start may migrate the household database.
#VERIFY: tests/unit/test_migrations_safety.py forces a mid-migration failure
(nothing persists), runs concurrent upgraders (one migrates, the rest no-op)
and refuses an unknown newer revision; take a backup before deploying an
image with a new revision.
#ASSUME: Concurrency: SQLite's file lock is honoured by every migrator, which
holds for processes on one host and a local volume, not for NFS.
#VERIFY: the core.database #CRITICAL keeps the file on local disk.
"""

from __future__ import annotations

import enum
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from family_logistics.core.database import immediate_transaction
from family_logistics.core.exceptions import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_LOCK_TIMEOUT_MS = 120_000
"""How long a migrator waits for another one to finish before giving up."""


class MigrationError(DatabaseError):
    """A migration was refused or failed; the schema is unchanged."""


class MigrationLockError(MigrationError):
    """Another process held the database's write lock for too long."""


class SchemaAheadError(MigrationError):
    """The database records a revision this release does not ship."""


class MigrationDirectionError(MigrationError):
    """An upgrade target is below, or a downgrade target not below, current."""


class SchemaState(enum.Enum):
    """Where a database's schema stands relative to this release."""

    AT_HEAD = "at head"
    BEHIND = "behind head"
    AHEAD = "unknown revision, newer than this release"


def _config() -> Config:
    """Build an Alembic config pointing at the packaged migrations."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def _script() -> ScriptDirectory:
    """Return the packaged migration scripts."""
    return ScriptDirectory.from_config(_config())


def head_revision() -> str:
    """Return the newest revision shipped with this version of the app.

    Returns:
        The head revision identifier.

    Raises:
        MigrationError: If the packaged migrations have no head. Several heads
            raise alembic's ``CommandError`` instead.
    """
    head = _script().get_current_head()
    if head is None:
        msg = "packaged migrations have no single head revision"
        raise MigrationError(msg, operation="migrate")
    return head


def _revision_of(connection: Connection) -> str | None:
    """Return the revision recorded on an open connection."""
    return MigrationContext.configure(connection).get_current_revision()


def current_revision(engine: Engine) -> str | None:
    """Return the revision recorded in the database.

    Args:
        engine: The application engine.

    Returns:
        The current revision, or None for an empty or unversioned database.
    """
    with engine.connect() as connection:
        return _revision_of(connection)


def _position(script: ScriptDirectory, revision: str | None) -> int | None:
    """Return a revision's distance from base, or None if it is unknown."""
    if revision is None or revision == "base":
        return 0
    order = [item.revision for item in script.walk_revisions()]
    order.reverse()
    if revision in {"head", "heads"}:
        return len(order)
    try:
        found = script.get_revision(revision)
    except CommandError:
        return None
    return order.index(found.revision) + 1


def _state(script: ScriptDirectory, current: str | None) -> SchemaState:
    """Classify a recorded revision against the packaged scripts."""
    if _position(script, current) is None:
        return SchemaState.AHEAD
    if current == script.get_current_head():
        return SchemaState.AT_HEAD
    return SchemaState.BEHIND


def schema_state(engine: Engine) -> SchemaState:
    """Report where the database's schema stands relative to this release.

    A revision that the packaged migrations do not contain is reported as
    ``AHEAD``: it was most likely written by a newer release.

    Args:
        engine: The application engine (a read-only engine works).

    Returns:
        The schema state.
    """
    return _state(_script(), current_revision(engine))


def is_at_head(engine: Engine) -> bool:
    """Report whether the database schema matches this version of the app.

    Args:
        engine: The application engine.

    Returns:
        True when the recorded revision is the packaged head.
    """
    return current_revision(engine) == head_revision()


def ahead_message(current: str | None) -> str:
    """Explain a refused schema written by a newer release.

    Args:
        current: The revision recorded in the database.

    Returns:
        A one-line message for the operator.
    """
    return (
        f"database schema revision {current} is newer than this release "
        f"(head {head_revision()}); run the newer image, or restore a backup "
        "taken for this release"
    )


def _lock_refused(_exc: OperationalError) -> MigrationLockError:
    """Describe a refused migration lock for the operator.

    Args:
        _exc: The driver error from ``BEGIN IMMEDIATE``, not needed.

    Returns:
        The error to raise in its place.
    """
    msg = (
        "database is locked by another process (another instance "
        "may be migrating); schema unchanged"
    )
    return MigrationLockError(msg, operation="migrate")


@contextmanager
def _locked_transaction(engine: Engine) -> Generator[Connection]:
    """Yield a connection inside ``BEGIN IMMEDIATE``; commit on success.

    Any exception from the caller rolls the whole transaction back.

    Args:
        engine: The application engine.

    Yields:
        Connection: A connection holding the database's write lock.

    Note:
        A write lock that is not granted in time raises ``MigrationLockError``.
    """
    with immediate_transaction(
        engine,
        lock_timeout_ms=MIGRATION_LOCK_TIMEOUT_MS,
        on_begin_error=_lock_refused,
    ) as connection:
        yield connection


def _migrate(engine: Engine, revision: str, *, downgrade: bool) -> None:
    """Move the schema to ``revision`` atomically, under the write lock.

    Args:
        engine: The application engine.
        revision: Target revision id, ``head`` or ``base``.
        downgrade: True to move down, False to move up.

    Raises:
        SchemaAheadError: If the database records an unknown revision.
        MigrationDirectionError: If the target is on the wrong side of current.
        MigrationError: If the target is unknown or the migration failed.
    """
    script = _script()
    target = _position(script, revision)
    if target is None:
        msg = f"unknown revision {revision!r}; schema unchanged"
        raise MigrationError(msg, operation="migrate")
    try:
        with _locked_transaction(engine) as connection:
            current = _revision_of(connection)
            position = _position(script, current)
            if position is None:
                raise SchemaAheadError(ahead_message(current), operation="migrate")
            if not downgrade and target < position:
                msg = (
                    f"revision {revision} is below the current revision "
                    f"{current}; use an explicit downgrade"
                )
                raise MigrationDirectionError(msg, operation="migrate")
            if downgrade and target >= position:
                msg = (
                    f"a downgrade target must be below the current revision "
                    f"{current or 'base'}; got {revision}"
                )
                raise MigrationDirectionError(msg, operation="migrate")
            config = _config()
            config.attributes["connection"] = connection
            if downgrade:
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)
    except (CommandError, SQLAlchemyError) as exc:
        detail = str(exc).splitlines()[0]
        msg = f"migration failed and was rolled back; schema unchanged: {detail}"
        raise MigrationError(msg, operation="migrate") from exc


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Upgrade the schema atomically; a concurrent migrator waits its turn.

    Args:
        engine: The application engine.
        revision: Target revision id, ``head`` (default) or ``base``. It must
            not be below the current revision.
    """
    _migrate(engine, revision, downgrade=False)


def downgrade(engine: Engine, revision: str) -> None:
    """Downgrade the schema atomically (operator action; may destroy data).

    Args:
        engine: The application engine.
        revision: Target revision, for example ``base``; it must be below the
            current revision.
    """
    _migrate(engine, revision, downgrade=True)
