"""Alembic environment.

Migrations always run against a connection supplied by
``family_logistics.db.migrations_api`` through ``config.attributes``; there is
no alembic.ini and no URL in configuration files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from alembic import context

from family_logistics.db.schema import metadata

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

connection = cast("Connection", context.config.attributes["connection"])
context.configure(
    connection=connection,
    target_metadata=metadata,
    render_as_batch=True,
)
with context.begin_transaction():
    context.run_migrations()
