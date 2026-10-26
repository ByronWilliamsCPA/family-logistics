"""Notification state: per-subject de-duplication rows and the dispatch watermark.

``notification_state`` records, per ``(subject_key, mode)``, what the
notification service last delivered and whether a push is in flight, so a push
is owed once per episode, survives quiet hours and failed deliveries, and is
never sent twice (ADR 0008, D4, D5, D9 and D11). ``notification_watermark``
holds the single newest outcome time dispatched, so an older run can never
notify after a newer one (D9).

Neither table holds personal data: subject keys are hashes, run ids and fixed
names; tags are the configured ``fl-*`` values. ``run_id`` has no foreign key
because runs are purged on their own schedule.

Revision ID: 0006_notification_state
Revises: 0005_route_relay
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_notification_state"
down_revision = "0005_route_relay"
branch_labels = None
depends_on = None

# Same rule as 0002: the DDL type is DateTime(timezone=True) and the aware-UTC
# behaviour lives in the ORM-layer ``UtcDateTime`` type in ``db.schema``.
_TZ = sa.DateTime(timezone=True)

# SQLAlchemy reads ``:name`` in a CHECK string as a bind parameter, so the
# colons in the subject keys are escaped; the database stores them plain.
_SUBJECT_CHECK = (
    "(length(subject_key) = 22 AND subject_key GLOB 'issue\\:[0-9a-f]*'"
    " AND substr(subject_key, 7) NOT GLOB '*[^0-9a-f]*'"
    " AND kind IN ('notice', 'urgent'))"
    " OR (subject_key = 'run-failed' AND kind IN ('notice', 'urgent'))"
    " OR (subject_key GLOB 'run\\:?*' AND length(subject_key) <= 40"
    " AND kind IN ('status', 'report'))"
    " OR (subject_key IN ('ops\\:run_failed', 'ops\\:run_incomplete')"
    " AND kind = 'ops')"
)


def upgrade() -> None:
    """Create ``notification_state`` and ``notification_watermark``."""
    op.create_table(
        "notification_state",
        sa.Column("subject_key", sa.String(48), primary_key=True),
        sa.Column("mode", sa.String(16), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("tag", sa.String(64), nullable=False),
        sa.Column("rule_id", sa.String(16), nullable=True),
        sa.Column("severity", sa.String(16), nullable=True),
        sa.Column("is_open", sa.Boolean, nullable=False),
        sa.Column("delivered", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "bypass_used", sa.Boolean, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("episode_run_id", sa.String(36), nullable=False),
        sa.Column(
            "send_count", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "failed_attempts", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("first_sent_at", _TZ, nullable=True),
        sa.Column("last_sent_at", _TZ, nullable=True),
        sa.Column("last_attempt_at", _TZ, nullable=True),
        sa.Column("claim_token", sa.String(36), nullable=True),
        sa.Column("claimed_at", _TZ, nullable=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("evaluated_at", _TZ, nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.CheckConstraint(
            "kind IN ('notice', 'urgent', 'status', 'report', 'ops')",
            name="ck_notification_state_kind",
        ),
        sa.CheckConstraint(
            "mode IN ('shadow', 'production')", name="ck_notification_state_mode"
        ),
        sa.CheckConstraint("tag GLOB 'fl-[a-z0-9]*'", name="ck_notification_state_tag"),
        sa.CheckConstraint(
            "severity IS NULL OR severity IN ('critical', 'warning', 'incomplete')",
            name="ck_notification_state_severity",
        ),
        sa.CheckConstraint(
            "send_count >= 0 AND failed_attempts >= 0",
            name="ck_notification_state_counts",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL) = (claimed_at IS NULL)",
            name="ck_notification_state_claim",
        ),
        sa.CheckConstraint(
            "delivered = 0 OR (first_sent_at IS NOT NULL AND last_sent_at IS NOT NULL)",
            name="ck_notification_state_delivered",
        ),
        sa.CheckConstraint(_SUBJECT_CHECK, name="ck_notification_state_subject"),
    )
    op.create_index("ix_notification_state_open", "notification_state", ["is_open"])
    op.create_index(
        "ix_notification_state_updated_at", "notification_state", ["updated_at"]
    )
    op.create_table(
        "notification_watermark",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("evaluated_at", _TZ, nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.CheckConstraint("id = 1", name="ck_notification_watermark_singleton"),
    )


def downgrade() -> None:
    """Drop both tables; every episode is forgotten and owed pushes re-derive."""
    op.drop_table("notification_watermark")
    op.drop_index("ix_notification_state_updated_at", table_name="notification_state")
    op.drop_index("ix_notification_state_open", table_name="notification_state")
    op.drop_table("notification_state")
