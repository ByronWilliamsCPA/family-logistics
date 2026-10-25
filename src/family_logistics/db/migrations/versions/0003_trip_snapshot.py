"""Trip snapshot: the previous run's accepted rides, for change impact.

Also indexes ``analysis_run.started_at`` (newest-first run listing) and
``finding.run_id`` (findings of a run); both tables come from revision 0002.

Revision ID: 0003_trip_snapshot
Revises: 0002_pilot_core
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_trip_snapshot"
down_revision = "0002_pilot_core"
branch_labels = None
depends_on = None

# Same rule as 0002: the DDL type is DateTime(timezone=True) and the aware-UTC
# behaviour lives in the ORM-layer ``UtcDateTime`` type in ``db.schema``.
_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create ``trip_snapshot`` and the run and finding indexes."""
    op.create_table(
        "trip_snapshot",
        sa.Column("trip_key", sa.String(512), primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("analysis_run.run_id"),
            nullable=True,
        ),
        sa.Column("updated_at", _TZ, nullable=False),
    )
    op.create_index("ix_analysis_run_started_at", "analysis_run", ["started_at"])
    op.create_index("ix_finding_run_id", "finding", ["run_id"])


def downgrade() -> None:
    """Drop the indexes and ``trip_snapshot``; the next run has no previous rides."""
    op.drop_index("ix_finding_run_id", table_name="finding")
    op.drop_index("ix_analysis_run_started_at", table_name="analysis_run")
    op.drop_table("trip_snapshot")
