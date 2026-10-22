"""Alembic environment.

Migrations run only through ``family_logistics.db.migrations_api``, which
supplies a connection through ``config.attributes`` inside one ``BEGIN
IMMEDIATE`` transaction. There is no alembic.ini, no URL in configuration
files and no offline (``--sql``) mode; a bare ``alembic`` invocation fails
with a clear error.

``render_as_batch`` lets future revisions ALTER tables on SQLite, which does
so by copying the table. Copying ``audit_log`` drops its triggers, so a
revision that batch-alters it must recreate them;
tests/unit/test_migrations_safety.py asserts they exist at head.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from alembic import context

from family_logistics.db.schema import metadata

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

supplied = context.config.attributes.get("connection")
if supplied is None:
    msg = (
        "run migrations through family_logistics.db.migrations_api "
        "(`family-logistics migrate`), not the alembic command line"
    )
    raise RuntimeError(msg)
connection = cast("Connection", supplied)
context.configure(
    connection=connection,
    target_metadata=metadata,
    render_as_batch=True,
    # The caller's transaction makes SQLite DDL transactional; say so, so
    # Alembic does not assume (and log) non-transactional DDL.
    transactional_ddl=True,
)
with context.begin_transaction():
    context.run_migrations()
