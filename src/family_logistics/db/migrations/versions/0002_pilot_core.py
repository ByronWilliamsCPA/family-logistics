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

# The DDL type stays DateTime(timezone=True). Offset-preserving behaviour is
# the ORM-layer ``UtcDateTime`` type in ``db.schema``; migrations must not
# import app code.
# #ASSUME: Data integrity: SQLite stores naive text and enforces no offset or
# UTC rule, so raw SQL writers are NOT protected by ``UtcDateTime``.
# #VERIFY: only ORM or Core code writes these columns; tests assert the aware
# UTC round trip through ``db.schema``.
_TZ = sa.DateTime(timezone=True)

# Referenced by several tables; the names are kept local so the migration does
# not depend on the mutable ``db.schema`` module.
_PERSON_ID_FK = "person.person_id"


# ``alias.target_id`` is polymorphic (person, passenger or location ids) and has
# no foreign key, so deleting a target used to leave orphan aliases. These
# AFTER DELETE triggers remove them. They are migration-only: ``db.schema``
# does not declare triggers (the audit_log triggers in 0001 are the precedent).
_ALIAS_TRIGGERS = {
    "trg_external_passenger_delete_alias": (
        "CREATE TRIGGER trg_external_passenger_delete_alias "
        "AFTER DELETE ON external_passenger BEGIN "
        "DELETE FROM alias WHERE kind = 'passenger' "
        "AND target_id = OLD.passenger_id; END"
    ),
    "trg_person_delete_alias": (
        "CREATE TRIGGER trg_person_delete_alias "
        "AFTER DELETE ON person BEGIN "
        "DELETE FROM alias WHERE kind = 'person' "
        "AND target_id = OLD.person_id; END"
    ),
    "trg_location_delete_alias": (
        "CREATE TRIGGER trg_location_delete_alias "
        "AFTER DELETE ON location BEGIN "
        "DELETE FROM alias WHERE kind = 'location' "
        "AND target_id = OLD.location_id; END"
    ),
}


def _fernet_check(column: str) -> str:
    """Return the CHECK text that keeps a ciphertext column Fernet-shaped.

    Kept identical to the helper in ``db.schema``; the parity tests compare the
    two texts.

    Args:
        column: Name of the nullable ciphertext column.

    Returns:
        SQL accepting NULL or a blob of at least 100 bytes (the size of the
        smallest Fernet token) whose first six bytes are ``gAAAAA``.
    """
    return (
        f"{column} IS NULL OR (typeof({column}) = 'blob' "
        f"AND length({column}) >= 100 "
        f"AND substr({column}, 1, 6) = X'674141414141')"
    )


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
        # A Fernet token starts with version byte 0x80 and a small timestamp,
        # which is "gAAAAA" in base64 (hex 674141414141); plaintext and
        # text-typed values fail.
        sa.CheckConstraint(
            _fernet_check("address_ciphertext"),
            name="ck_location_address_ciphertext_fernet",
        ),
        sa.CheckConstraint(
            _fernet_check("latlon_ciphertext"),
            name="ck_location_latlon_ciphertext_fernet",
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
        sa.CheckConstraint(
            "role IN ('parent', 'child', 'contact')", name="ck_person_role"
        ),
        sa.CheckConstraint(
            (
                "monitoring_start IS NULL OR monitoring_end IS NULL "
                "OR monitoring_end >= monitoring_start"
            ),
            name="ck_person_monitoring_range",
        ),
    )
    op.create_table(
        "calendar_source",
        sa.Column("calendar_id", sa.String(128), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "person_id",
            sa.String(64),
            sa.ForeignKey(_PERSON_ID_FK),
            nullable=True,
        ),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_success", _TZ, nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.CheckConstraint(
            "kind IN ('transport', 'person', 'school_district', 'school_bell')",
            name="ck_calendar_source_kind",
        ),
        # Owner rule from events.CalendarSource: person_id is the owner of an
        # individual calendar and None otherwise.
        sa.CheckConstraint(
            (
                "(kind = 'person' AND person_id IS NOT NULL) "
                "OR (kind <> 'person' AND person_id IS NULL)"
            ),
            name="ck_calendar_source_person_owner",
        ),
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
        sa.CheckConstraint(
            "status IN ('confirmed', 'tentative', 'cancelled')",
            name="ck_source_event_status",
        ),
        sa.CheckConstraint(
            "julianday(end_utc) >= julianday(start_utc)",
            name="ck_source_event_end_after_start",
        ),
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
            sa.ForeignKey(_PERSON_ID_FK),
            nullable=True,
        ),
        _location_fk("default_pickup_location_id"),
        _location_fk("default_dropoff_location_id"),
        sa.Column("alternate_destination_ids", sa.JSON, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("inactive_since", sa.Date, nullable=True),
        # The privacy spec requires a first name or nickname but defines no
        # stricter shape, so only non-empty (after trim) and the declared
        # maximum length are enforced.
        sa.CheckConstraint(
            "length(trim(display_first_name)) >= 1 AND length(display_first_name) <= 60",
            name="ck_external_passenger_first_name",
        ),
        # A passenger is either active with no end date or inactive with one.
        sa.CheckConstraint(
            (
                "(active = 1 AND inactive_since IS NULL) "
                "OR (active = 0 AND inactive_since IS NOT NULL)"
            ),
            name="ck_external_passenger_active_inactive_since",
        ),
    )
    op.create_table(
        "alias",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("alias", sa.String(120), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "kind IN ('person', 'passenger', 'location')", name="ck_alias_kind"
        ),
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
            sa.ForeignKey(_PERSON_ID_FK),
            nullable=False,
        ),
        sa.Column("weekdays", sa.JSON, nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column(
            "driver_id",
            sa.String(64),
            sa.ForeignKey(_PERSON_ID_FK),
            nullable=True,
        ),
        sa.Column("effective_from", sa.Date, nullable=True),
        sa.Column("effective_to", sa.Date, nullable=True),
        _location_fk("origin_location_id", nullable=False),
        _location_fk("destination_location_id", nullable=False),
        sa.CheckConstraint(
            "direction IN ('to_school', 'from_school')",
            name="ck_transport_default_direction",
        ),
        sa.CheckConstraint(
            "mode IN ('bike', 'bus', 'walk', 'parent', 'carpool')",
            name="ck_transport_default_mode",
        ),
        sa.CheckConstraint(
            (
                "effective_from IS NULL OR effective_to IS NULL "
                "OR effective_to >= effective_from"
            ),
            name="ck_transport_default_effective_range",
        ),
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
        sa.CheckConstraint(
            "status IN ('clear', 'attention_required', 'incomplete', 'failed')",
            name="ck_analysis_run_status",
        ),
        sa.CheckConstraint(
            "julianday(window_end) >= julianday(window_start)",
            name="ck_analysis_run_window",
        ),
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
        sa.CheckConstraint(
            "severity IN ('critical', 'warning', 'incomplete')",
            name="ck_finding_severity",
        ),
        sa.CheckConstraint(
            "lifecycle IN ('new', 'ongoing', 'resolved', 'regressed')",
            name="ck_finding_lifecycle",
        ),
    )
    op.create_index("ix_finding_lifecycle", "finding", ["lifecycle"])
    op.create_index("ix_finding_rule", "finding", ["rule_id"])
    for ddl in _ALIAS_TRIGGERS.values():
        op.execute(ddl)


def downgrade() -> None:
    """Drop the pilot-core tables in reverse dependency order.

    This destroys collected events, registry data and findings; it is an
    operator action for rollback only. The alias-cleanup triggers are dropped
    first, before the tables they sit on.
    """
    for name in _ALIAS_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index("ix_finding_rule", table_name="finding")
    op.drop_index("ix_finding_lifecycle", table_name="finding")
    op.drop_table("finding")
    op.drop_table("analysis_run")
    op.drop_table("transport_default")
    op.drop_index("ux_alias_kind_alias", table_name="alias")
    op.drop_table("alias")
    op.drop_table("external_passenger")
    op.drop_table("sync_state")
    op.drop_index("ix_source_event_deleted", table_name="source_event")
    op.drop_index("ix_source_event_start", table_name="source_event")
    op.drop_table("source_event")
    op.drop_table("calendar_source")
    op.drop_table("person")
    op.drop_table("household")
    op.drop_table("vehicle")
    op.drop_table("location")
