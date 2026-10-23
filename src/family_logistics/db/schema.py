"""SQLAlchemy table definitions.

The schema itself is owned by the Alembic migrations in
``family_logistics/db/migrations``. These definitions mirror the head
revision's columns for query building; triggers, CHECK constraints and indexes
exist only in the migrations, so ``metadata.create_all`` does not build a
production schema. Always create databases with ``migrations_api.upgrade``.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table

metadata = MetaData()

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("actor", String(64), nullable=False),
    Column("object_ref", String(128), nullable=False),
    Column("action", String(32), nullable=False),
    Column("prior_hash", String(64), nullable=True),
    Column("new_hash", String(64), nullable=True),
    Column("correlation_id", String(64), nullable=True),
)
"""Append-only audit trail (enforced by triggers in migration 0001). Rows carry
SHA-256 hashes of the prior and new state, never the state itself; the actor is
a username, so rows are pseudonymous, not anonymous. See
``family_logistics.db.audit`` for what the hashes do and do not protect."""
