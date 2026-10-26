"""Route relay: the Home Assistant route cache and the pending request queue.

Both tables are keyed by location id (never address text) and reference
``location`` with ``ON DELETE CASCADE``. A BEFORE UPDATE trigger on ``location``
drops a location's cache entries and pending requests, as origin or
destination, whenever its zone, address, coordinates or verification state
changes, so a corrected address can never be answered from the old address's
travel time (ADR 0006). The trigger is migration-only, like the alias triggers
in 0002.

Revision ID: 0005_route_relay
Revises: 0004_school_snapshot
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_route_relay"
down_revision = "0004_school_snapshot"
branch_labels = None
depends_on = None

# Same rule as 0002: the DDL type is DateTime(timezone=True) and the aware-UTC
# behaviour lives in the ORM-layer ``UtcDateTime`` type in ``db.schema``.
_TZ = sa.DateTime(timezone=True)

_LOCATION_ID_FK = "location.location_id"

# Repeated from ``db.schema.MAX_ROUTE_DURATION_SECONDS`` on purpose: a migration
# must not depend on the mutable schema module.
_MAX_DURATION = 12 * 60 * 60

# BEFORE UPDATE so the children are gone before SQLite checks the foreign keys
# of a changed ``location_id``; the whole statement is one atomic unit, so a
# failing UPDATE rolls the invalidation back with it. ``IS NOT`` compares NULLs.
_CREATE_TRIGGER = (
    "CREATE TRIGGER trg_location_route_relay_invalidate BEFORE UPDATE ON location "
    "WHEN OLD.location_id IS NOT NEW.location_id "
    "OR OLD.ha_zone_entity_id IS NOT NEW.ha_zone_entity_id "
    "OR OLD.address_ciphertext IS NOT NEW.address_ciphertext "
    "OR OLD.latlon_ciphertext IS NOT NEW.latlon_ciphertext "
    "OR OLD.verification_state IS NOT NEW.verification_state "
    "BEGIN "
    "DELETE FROM route_cache WHERE origin_location_id = OLD.location_id "
    "OR destination_location_id = OLD.location_id; "
    "DELETE FROM route_request WHERE origin_location_id = OLD.location_id "
    "OR destination_location_id = OLD.location_id; "
    "END"
)


def _location_fk(name: str, *, primary_key: bool) -> sa.Column[str]:
    """Build a column referencing ``location.location_id`` with a cascade.

    Args:
        name: Column name.
        primary_key: Whether the column is part of the primary key.

    Returns:
        The column.
    """
    return sa.Column(
        name,
        sa.String(64),
        sa.ForeignKey(_LOCATION_ID_FK, ondelete="CASCADE"),
        primary_key=primary_key,
        nullable=False,
    )


def upgrade() -> None:
    """Create ``route_cache``, ``route_request`` and the invalidation trigger."""
    op.create_table(
        "route_cache",
        _location_fk("origin_location_id", primary_key=True),
        _location_fk("destination_location_id", primary_key=True),
        sa.Column("weekday", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("bucket", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("duration_seconds", sa.Integer, nullable=False),
        sa.Column("observed_at", _TZ, nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_route_cache_weekday"),
        sa.CheckConstraint("bucket BETWEEN 0 AND 95", name="ck_route_cache_bucket"),
        sa.CheckConstraint(
            f"duration_seconds BETWEEN 1 AND {_MAX_DURATION}",
            name="ck_route_cache_duration",
        ),
        sa.CheckConstraint(
            "origin_location_id <> destination_location_id",
            name="ck_route_cache_distinct_endpoints",
        ),
    )
    op.create_table(
        "route_request",
        sa.Column("request_id", sa.String(36), primary_key=True),
        _location_fk("origin_location_id", primary_key=False),
        _location_fk("destination_location_id", primary_key=False),
        sa.Column("weekday", sa.Integer, nullable=False),
        sa.Column("bucket", sa.Integer, nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("expires_at", _TZ, nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("answered_duration_seconds", sa.Integer, nullable=True),
        sa.Column("answered_at", _TZ, nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'answered')", name="ck_route_request_status"
        ),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_route_request_weekday"),
        sa.CheckConstraint("bucket BETWEEN 0 AND 95", name="ck_route_request_bucket"),
        sa.CheckConstraint(
            "origin_location_id <> destination_location_id",
            name="ck_route_request_distinct_endpoints",
        ),
        sa.CheckConstraint(
            "julianday(expires_at) > julianday(created_at)",
            name="ck_route_request_expiry_after_creation",
        ),
        sa.CheckConstraint(
            (
                "(status = 'pending' AND answered_duration_seconds IS NULL"
                " AND answered_at IS NULL)"
                " OR (status = 'answered' AND answered_at IS NOT NULL"
                f" AND answered_duration_seconds BETWEEN 1 AND {_MAX_DURATION})"
            ),
            name="ck_route_request_answer",
        ),
    )
    op.create_index(
        "uq_route_request_pending",
        "route_request",
        [
            "origin_location_id",
            "destination_location_id",
            "weekday",
            "bucket",
        ],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.execute(_CREATE_TRIGGER)


def downgrade() -> None:
    """Drop the trigger and both tables; the next estimate is unknown again."""
    op.execute("DROP TRIGGER IF EXISTS trg_location_route_relay_invalidate")
    op.drop_index("uq_route_request_pending", table_name="route_request")
    op.drop_table("route_request")
    op.drop_table("route_cache")
