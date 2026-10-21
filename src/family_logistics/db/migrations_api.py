"""Run and inspect schema migrations without an alembic.ini.

The application owns its schema: ``serve`` refuses to start on a database
that is not at the head revision unless started with ``--upgrade-db``, and
``family-logistics migrate`` upgrades (or downgrades) explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

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


def is_at_head(engine: Engine) -> bool:
    """Report whether the database schema matches this version of the app.

    Args:
        engine: The application engine.

    Returns:
        True when the recorded revision is the packaged head.
    """
    return current_revision(engine) == head_revision()


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Upgrade the schema.

    Args:
        engine: The application engine.
        revision: Target revision (default: head).
    """
    config = _config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade(engine: Engine, revision: str) -> None:
    """Downgrade the schema (operator action; may destroy data).

    Args:
        engine: The application engine.
        revision: Target revision, for example ``base``.
    """
    config = _config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)
