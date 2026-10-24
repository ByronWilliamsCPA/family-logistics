"""Baseline schema: the audit log.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-21

The triggers below make ``audit_log`` append-only against any SQL statement on
any connection (SQLAlchemy or a bare ``sqlite3`` one), because triggers live in
the database file rather than in per-connection settings:

- ``audit_log_no_update`` aborts every UPDATE.
- ``audit_log_no_replace`` aborts an INSERT whose ``id`` already exists. SQLite's
  REPLACE conflict resolution (``INSERT OR REPLACE``, ``REPLACE INTO``) deletes
  the old row without firing DELETE triggers unless ``recursive_triggers`` is
  on, which is a per-connection pragma and cannot be relied on.
- ``audit_log_no_delete`` aborts a DELETE of a row younger than 365 days, the
  audit retention in the privacy spec; older rows may be purged.

``prior_hash`` and ``new_hash`` are HMAC-SHA256 digests under ``FL_AUDIT_KEY``;
``hash_key_version`` records which key version produced them. The column was
added to this revision in place, before any production database existed.

#CRITICAL: Data integrity: the triggers do not stop schema changes. Anyone who
can write the database file can DROP TRIGGER, DROP TABLE or run this
revision's downgrade, which deletes the audit history.
#VERIFY: only the application container mounts the data volume; downgrades
need ``migrate --downgrade`` plus a confirmation; tests/unit/test_audit.py and
tests/unit/test_migrations_safety.py assert the three triggers exist at head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

APPEND_ONLY = "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
HASH_CHECK = "{0} IS NULL OR (length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"
TRIGGERS = {
    "audit_log_no_update": "BEFORE UPDATE ON audit_log",
    "audit_log_no_replace": (
        "BEFORE INSERT ON audit_log "
        "WHEN EXISTS (SELECT 1 FROM audit_log WHERE id = NEW.id)"
    ),
    # An unparseable timestamp makes julianday() NULL; COALESCE keeps that row.
    "audit_log_no_delete": (
        "BEFORE DELETE ON audit_log WHEN COALESCE("
        "julianday(OLD.recorded_at) > julianday('now', '-365 days'), 1)"
    ),
}


def upgrade() -> None:
    """Create the audit log, its lookup index and its append-only triggers."""
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("object_ref", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("prior_hash", sa.String(64), nullable=True),
        sa.Column("new_hash", sa.String(64), nullable=True),
        # Which FL_AUDIT_KEY version keyed the hashes, so a rotation can be
        # told apart from a tampered row.
        sa.Column("hash_key_version", sa.Integer, nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        # A positive id keeps NEW.id (-1 for an automatic id in a BEFORE
        # INSERT trigger) from ever matching a stored row.
        sa.CheckConstraint("id > 0", name="ck_audit_log_id_positive"),
        sa.CheckConstraint(
            "julianday(recorded_at) IS NOT NULL", name="ck_audit_log_recorded_at"
        ),
        sa.CheckConstraint("length(actor) BETWEEN 1 AND 64", name="ck_audit_actor"),
        sa.CheckConstraint(
            "length(object_ref) BETWEEN 1 AND 128", name="ck_audit_object_ref"
        ),
        sa.CheckConstraint("length(action) BETWEEN 1 AND 32", name="ck_audit_action"),
        sa.CheckConstraint(
            "correlation_id IS NULL OR length(correlation_id) BETWEEN 1 AND 64",
            name="ck_audit_correlation_id",
        ),
        sa.CheckConstraint(HASH_CHECK.format("prior_hash"), name="ck_audit_prior_hash"),
        sa.CheckConstraint(HASH_CHECK.format("new_hash"), name="ck_audit_new_hash"),
        sa.CheckConstraint("hash_key_version >= 1", name="ck_audit_hash_key_version"),
    )
    op.create_index("ix_audit_log_object_ref", "audit_log", ["object_ref", "id"])
    for name, timing in TRIGGERS.items():
        op.execute(f"CREATE TRIGGER {name} {timing} {APPEND_ONLY}")


def downgrade() -> None:
    """Drop the audit log (destroys audit history; operator action only)."""
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("audit_log")
