"""School snapshot: parsed school days and freshness from Home Assistant.

Stores only the parsed ``SchoolDay`` rows and the snapshot's freshness
(ADR 0007). The raw ``calendar.get_events`` payload, its event text and the
zone list are never stored.

Revision ID: 0004_school_snapshot
Revises: 0003_trip_snapshot
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_school_snapshot"
down_revision = "0003_trip_snapshot"
branch_labels = None
depends_on = None

# Same rule as 0002: the DDL type is DateTime(timezone=True) and the aware-UTC
# behaviour lives in the ORM-layer ``UtcDateTime`` type in ``db.schema``.
_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``school_snapshot`` and ``school_day``."""
    op.create_table(
        "school_snapshot",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("fetched_at", _TZ, nullable=False),
        sa.Column("received_at", _TZ, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("day_count", sa.Integer, nullable=False),
        sa.CheckConstraint("id = 1", name="ck_school_snapshot_singleton"),
        sa.CheckConstraint(
            "julianday(fetched_at) <= julianday(received_at)",
            name="ck_school_snapshot_fetched_not_after_received",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_school_snapshot_content_hash"
        ),
        sa.CheckConstraint("day_count >= 0", name="ck_school_snapshot_day_count"),
    )
    op.create_table(
        "school_day",
        sa.Column("person_id", sa.String(64), primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("location_id", sa.String(64), nullable=False),
        sa.Column("start_utc", _TZ, nullable=False),
        sa.Column("end_utc", _TZ, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "kind IN ('normal', 'early_release', 'late_start', 'no_school', 'unknown')",
            name="ck_school_day_kind",
        ),
        sa.CheckConstraint(
            "julianday(end_utc) >= julianday(start_utc)",
            name="ck_school_day_order",
        ),
    )


def downgrade() -> None:
    """Drop both tables; the next run has no school snapshot until HA posts."""
    op.drop_table("school_day")
    op.drop_table("school_snapshot")
