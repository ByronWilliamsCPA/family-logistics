"""Run and inspect schema migrations without an alembic.ini.

The application owns its schema: ``serve`` refuses to start on a database
that is not at the head revision unless started with ``--upgrade-db``, and
``family-logistics migrate`` upgrades (or downgrades) explicitly.

#CRITICAL: Data integrity: a migration run is all-or-nothing. Python's
sqlite3 driver (legacy transaction control) runs DDL outside any
transaction, so Alembic's own transaction would not cover CREATE/DROP and a
failure halfway would leave a schema that matches no revision. Every run
therefore takes a connection whose driver-level transaction handling is off
and wraps the whole run, version-table update included, in one explicit
``BEGIN IMMEDIATE`` ... ``COMMIT`` (SQLite DDL is transactional).
#VERIFY: tests/unit/test_migrations_atomic.py makes a migration fail after
creating a table and asserts the table is gone and the recorded revision is
unchanged.
#EDGE: A database whose recorded revision is unknown to this build (written
by a newer image, or not ours) is never upgraded or downgraded: it is
reported as SchemaState.UNKNOWN so callers refuse with a clear message.
#VERIFY: tests/unit/test_cli_ops.py::TestNewerSchema.
"""

from __future__ import annotations

import enum
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator

    from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _config() -> Config:
    """Build an Alembic config pointing at the packaged migrations."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def head_revision() -> str:
    """Return the newest revision shipped with this version of the app.

    Returns:
        The head revision identifier.

    Raises:
        RuntimeError: If the packaged migrations do not have exactly one head.
    """
    head = ScriptDirectory.from_config(_config()).get_current_head()
    if head is None:
        msg = "packaged migrations have no single head revision"
        raise RuntimeError(msg)
    return head


def current_revision(engine: Engine) -> str | None:
    """Return the revision recorded in the database.

    Args:
        engine: The application engine.

    Returns:
        The current revision, or None for an empty or unversioned database.
    """
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


class SchemaState(enum.Enum):
    """How a database's recorded revision relates to this build."""

    AT_HEAD = "at_head"
    BEHIND = "behind"
    UNKNOWN = "unknown"


class MigrationRefusedError(RuntimeError):
    """A requested migration would do something other than what was asked."""


def _known_revisions() -> set[str]:
    """Return every revision identifier shipped with this build."""
    script = ScriptDirectory.from_config(_config())
    return {rev.revision for rev in script.walk_revisions()}


def schema_state(engine: Engine) -> SchemaState:
    """Classify the database schema relative to the packaged migrations.

    Args:
        engine: The application engine.

    Returns:
        AT_HEAD, BEHIND (empty, unversioned or an older known revision) or
        UNKNOWN (a revision this build does not ship, such as one written by
        a newer image).
    """
    current = current_revision(engine)
    if current == head_revision():
        return SchemaState.AT_HEAD
    if current is None or current in _known_revisions():
        return SchemaState.BEHIND
    return SchemaState.UNKNOWN


def unknown_revision_message(engine: Engine) -> str:
    """Explain an UNKNOWN schema state without guessing at a fix.

    Args:
        engine: The application engine.

    Returns:
        A message naming the recorded and packaged revisions.
    """
    return (
        f"database schema revision {current_revision(engine)!r} is not known to "
        f"this version (head {head_revision()!r}); it was written by a newer "
        "build or is not a Family Logistics database. Run the matching image "
        "or restore a backup taken with this version"
    )


def is_at_head(engine: Engine) -> bool:
    """Report whether the database schema matches this version of the app.

    Args:
        engine: The application engine.

    Returns:
        True when the recorded revision is the packaged head.
    """
    return current_revision(engine) == head_revision()


@contextmanager
def _atomic_connection(engine: Engine) -> Generator[Connection]:
    """Yield a connection whose whole use is one SQLite transaction.

    Args:
        engine: The application engine.

    Yields:
        A connection inside ``BEGIN IMMEDIATE``; committed on success and
        rolled back on any exception.
    """
    with engine.connect() as connection:
        if engine.dialect.name != "sqlite":  # pragma: no cover - SQLite only
            with connection.begin():
                yield connection
            return
        driver = cast("sqlite3.Connection", connection.connection.driver_connection)
        previous = driver.isolation_level
        driver.isolation_level = None
        try:
            _ = connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        finally:
            driver.isolation_level = previous


def _run(engine: Engine, revision: str, *, downgrade: bool) -> None:
    """Run an Alembic upgrade or downgrade atomically."""
    if schema_state(engine) is SchemaState.UNKNOWN:
        raise MigrationRefusedError(unknown_revision_message(engine))
    config = _config()
    with _atomic_connection(engine) as connection:
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


def is_below_current(engine: Engine, revision: str) -> bool:
    """Report whether ``revision`` is an ancestor of the current revision.

    Args:
        engine: The application engine.
        revision: A target revision identifier, ``base`` or ``head``.

    Returns:
        True when moving to ``revision`` would be a downgrade.
    """
    current = current_revision(engine)
    if current is None or revision in {"head", "heads", current}:
        return False
    if revision == "base":
        return True
    script = ScriptDirectory.from_config(_config())
    ancestors = {rev.revision for rev in script.iterate_revisions(current, "base")}
    return revision in ancestors


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Upgrade the schema in one transaction.

    Args:
        engine: The application engine.
        revision: Target revision (default: head).

    Raises:
        MigrationRefusedError: The recorded revision is unknown to this build,
            or ``revision`` is below the current one (use ``downgrade``).
    """
    if is_below_current(engine, revision):
        msg = (
            f"revision {revision!r} is below the current schema revision; "
            "pass --downgrade to move down (may destroy data)"
        )
        raise MigrationRefusedError(msg)
    _run(engine, revision, downgrade=False)


def downgrade(engine: Engine, revision: str) -> None:
    """Downgrade the schema in one transaction (operator action; may destroy data).

    Args:
        engine: The application engine.
        revision: Target revision, for example ``base``.
    """
    _run(engine, revision, downgrade=True)
