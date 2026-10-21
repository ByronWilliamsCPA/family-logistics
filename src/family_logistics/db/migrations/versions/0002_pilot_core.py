"""Pilot core: calendar sources, source events, registry, runs and findings.

Revision ID: 0002_pilot_core
Revises: 0001_baseline
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_pilot_core"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

_TZ = sa.DateTime(timezone=True)


def _location_fk(name: str, *, nullable: bool = True) -> sa.Column[str]:
    """Build a column referencing ``location.location_id``.

    Args:
        name: Column name.
        nullable: Whether the column may be NULL.

    Returns:
        The column.
    """
    return sa.Column(
        name, sa.String(64), sa.ForeignKey("location.location_id"), nullable=nullable
    )


def upgrade() -> None:
    """Create the pilot-core tables in dependency order."""
    op.create_table(
        "calendar_source",
        sa.Column("calendar_id", sa.String(128), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("person_id", sa.String(64), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_success", _TZ, nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
    )
    op.create_table(
        "source_event",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "calendar_id",
            sa.String(128),
            sa.ForeignKey("calendar_source.calendar_id"),
            nullable=False,
        ),
        sa.Column("uid", sa.String(255), nullable=False),
        sa.Column("recurrence_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("start_utc", _TZ, nullable=False),
        sa.Column("end_utc", _TZ, nullable=False),
        sa.Column("all_day", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("location", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("etag", sa.String(255), nullable=True),
        sa.Column("raw_hash", sa.String(64), nullable=False),
        sa.Column("is_override", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "series_uid_has_rrule",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("first_seen", _TZ, nullable=False),
        sa.Column("last_seen", _TZ, nullable=False),
        sa.Column("deleted_at", _TZ, nullable=True),
        sa.UniqueConstraint(
            "calendar_id", "uid", "recurrence_id", name="uq_source_event_occurrence"
        ),
    )
    op.create_index("ix_source_event_start", "source_event", ["start_utc"])
    op.create_index("ix_source_event_deleted", "source_event", ["deleted_at"])
    op.create_table(
        "sync_state",
        sa.Column(
            "calendar_id",
            sa.String(128),
            sa.ForeignKey("calendar_source.calendar_id"),
            primary_key=True,
        ),
        sa.Column("sync_token", sa.Text, nullable=True),
        sa.Column("ctag", sa.String(255), nullable=True),
        sa.Column("last_full_sync", _TZ, nullable=True),
    )
    op.create_table(
        "location",
        sa.Column("location_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("ha_zone_entity_id", sa.String(128), nullable=True),
        sa.Column("address_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("latlon_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column(
            "verification_state",
            sa.String(16),
            nullable=False,
            server_default="unverified",
        ),
    )
    op.create_table(
        "vehicle",
        sa.Column("vehicle_id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("seat_capacity", sa.Integer, nullable=True),
    )
    op.create_table(
        "household",
        sa.Column("household_id", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column(
            "privacy_scope", sa.String(32), nullable=False, server_default="external"
        ),
    )
    op.create_table(
        "person",
        sa.Column("person_id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("can_drive", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "default_vehicle_id",
            sa.String(64),
            sa.ForeignKey("vehicle.vehicle_id"),
            nullable=True,
        ),
        sa.Column(
            "monitoring_enabled", sa.Boolean, nullable=False, server_default=sa.true()
        ),
        sa.Column("monitoring_start", sa.Date, nullable=True),
        sa.Column("monitoring_end", sa.Date, nullable=True),
        _location_fk("home_location_id"),
    )
    op.create_table(
        "external_passenger",
        sa.Column("passenger_id", sa.String(64), primary_key=True),
        sa.Column("display_first_name", sa.String(60), nullable=False),
        sa.Column(
            "household_id",
            sa.String(64),
            sa.ForeignKey("household.household_id"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            sa.String(64),
            sa.ForeignKey("person.person_id"),
            nullable=True,
        ),
        _location_fk("default_pickup_location_id"),
        _location_fk("default_dropoff_location_id"),
        sa.Column("alternate_destination_ids", sa.JSON, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("inactive_since", sa.Date, nullable=True),
    )
    op.create_table(
        "alias",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("alias", sa.String(120), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
    )
    op.create_index(
        "ux_alias_kind_alias",
        "alias",
        ["kind", sa.text("lower(alias)")],
        unique=True,
    )
    op.create_table(
        "transport_default",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "person_id",
            sa.String(64),
            sa.ForeignKey("person.person_id"),
            nullable=False,
        ),
        sa.Column("weekdays", sa.JSON, nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column(
            "driver_id",
            sa.String(64),
            sa.ForeignKey("person.person_id"),
            nullable=True,
        ),
        sa.Column("effective_from", sa.Date, nullable=True),
        sa.Column("effective_to", sa.Date, nullable=True),
        _location_fk("origin_location_id", nullable=False),
        _location_fk("destination_location_id", nullable=False),
    )
    op.create_table(
        "analysis_run",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("window_start", _TZ, nullable=False),
        sa.Column("window_end", _TZ, nullable=False),
        sa.Column("started_at", _TZ, nullable=False),
        sa.Column("finished_at", _TZ, nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("counts", sa.JSON, nullable=False),
        sa.Column("rules_version", sa.String(32), nullable=False),
        sa.Column("error", sa.String(500), nullable=True),
    )
    op.create_table(
        "finding",
        sa.Column("issue_id", sa.String(16), primary_key=True),
        sa.Column("rule_id", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("entity_keys", sa.JSON, nullable=False),
        sa.Column("suggestion", sa.Text, nullable=True),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("first_seen", _TZ, nullable=False),
        sa.Column("last_seen", _TZ, nullable=False),
        sa.Column("resolved_at", _TZ, nullable=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("analysis_run.run_id"),
            nullable=True,
        ),
    )
    op.create_index("ix_finding_lifecycle", "finding", ["lifecycle"])
    op.create_index("ix_finding_rule", "finding", ["rule_id"])


def downgrade() -> None:
    """Drop the pilot-core tables in reverse dependency order.

    This destroys collected events, registry data and findings; it is an
    operator action for rollback only.
    """
    op.drop_index("ix_finding_rule", table_name="finding")
    op.drop_index("ix_finding_lifecycle", table_name="finding")
    op.drop_table("finding")
    op.drop_table("analysis_run")
    op.drop_table("transport_default")
    op.drop_index("ux_alias_kind_alias", table_name="alias")
    op.drop_table("alias")
    op.drop_table("external_passenger")
    op.drop_table("person")
    op.drop_table("household")
    op.drop_table("vehicle")
    op.drop_table("location")
    op.drop_table("sync_state")
    op.drop_index("ix_source_event_deleted", table_name="source_event")
    op.drop_index("ix_source_event_start", table_name="source_event")
    op.drop_table("source_event")
    op.drop_table("calendar_source")
