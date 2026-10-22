"""Baseline schema: append-only audit log.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the audit log and the triggers that make it append-only."""
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("object_ref", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("prior_hash", sa.String(64), nullable=True),
        sa.Column("new_hash", sa.String(64), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
    )
    for event in ("UPDATE", "DELETE"):
        op.execute(
            " ".join(
                (
                    f"CREATE TRIGGER audit_log_no_{event.lower()}",
                    f"BEFORE {event} ON audit_log",
                    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
                )
            )
        )
    # INSERT OR REPLACE (and REPLACE INTO) deletes the conflicting row without
    # firing DELETE triggers (recursive_triggers is off by default), so an
    # insert that reuses an existing id is refused before it can replace.
    op.execute(
        " ".join(
            (
                "CREATE TRIGGER audit_log_no_replace",
                "BEFORE INSERT ON audit_log",
                "WHEN NEW.id IS NOT NULL",
                "AND EXISTS (SELECT 1 FROM audit_log WHERE id = NEW.id)",
                "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
            )
        )
    )


def downgrade() -> None:
    """Drop the audit log (destroys audit history; operator action only)."""
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_replace")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
    op.drop_table("audit_log")
