"""SQLAlchemy table definitions.

The schema itself is owned by the Alembic migrations in
``family_logistics/db/migrations``. These definitions mirror the head
revision's columns for query building. The pilot-core tables (revision 0002)
also declare their indexes, unique constraints and named CHECK constraints
here, with the same names and SQL text as the migration, so metadata and
migration stay in parity. ``audit_log`` is the exception: its CHECK constraints
and its append-only triggers exist only in migration 0001, so
``metadata.create_all`` does not build a production schema. Always create
databases with ``migrations_api.upgrade``.

Pilot-core timestamp columns use ``UtcDateTime`` (ORM and Core layer only; the
DDL type is still ``DateTime(timezone=True)``). SQLite stores naive text, so a
raw SQL writer is not protected by it.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

from family_logistics.db.column_types import UtcDateTime

metadata = MetaData()

_PERSON_ID_FK = "person.person_id"
_LOCATION_ID_FK = "location.location_id"

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
    Column("hash_key_version", Integer, nullable=False),
    Column("correlation_id", String(64), nullable=True),
)
"""Append-only audit trail (enforced by triggers in migration 0001). Rows carry
HMAC-SHA256 digests of the prior and new state (never the state itself) and the
version of the key that produced them (``hash_key_version``); the actor is a
username, so rows are pseudonymous, not anonymous. See
``family_logistics.db.audit`` for what the hashes do and do not protect."""

# ---------------------------------------------------------------------------
# Pilot core (revision 0002_pilot_core)
# ---------------------------------------------------------------------------
# Registry tables hold household data. Addresses and coordinates are stored
# only as Fernet ciphertext (``core.crypto``); names of external children are
# first-name labels keyed by an internal passenger ID. Aliases for people,
# passengers and locations live in the single ``alias`` table.

calendar_source = Table(
    "calendar_source",
    metadata,
    Column("calendar_id", String(128), primary_key=True),
    Column("display_name", String(200), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("person_id", String(64), ForeignKey(_PERSON_ID_FK), nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("last_success", UtcDateTime(), nullable=True),
    Column("last_error", String(500), nullable=True),
    CheckConstraint(
        "kind IN ('transport', 'person', 'school_district', 'school_bell')",
        name="ck_calendar_source_kind",
    ),
    # Owner rule from events.CalendarSource: person_id is the owner of an
    # individual calendar and None otherwise.
    CheckConstraint(
        (
            "(kind = 'person' AND person_id IS NOT NULL) "
            "OR (kind <> 'person' AND person_id IS NULL)"
        ),
        name="ck_calendar_source_person_owner",
    ),
)
"""Calendars the collector reads; ``calendar_id`` is an href hash."""

source_event = Table(
    "source_event",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "calendar_id",
        String(128),
        ForeignKey("calendar_source.calendar_id"),
        nullable=False,
    ),
    Column("uid", String(255), nullable=False),
    Column("recurrence_id", String(64), nullable=False, default=""),
    Column("start_utc", UtcDateTime(), nullable=False),
    Column("end_utc", UtcDateTime(), nullable=False),
    Column("all_day", Boolean, nullable=False, default=False),
    Column("summary", Text, nullable=False, default=""),
    Column("location", Text, nullable=True),
    Column("description", Text, nullable=True),
    Column("status", String(16), nullable=False),
    Column("etag", String(255), nullable=True),
    Column("raw_hash", String(64), nullable=False),
    Column("is_override", Boolean, nullable=False, default=False),
    Column("series_uid_has_rrule", Boolean, nullable=False, default=False),
    Column("first_seen", UtcDateTime(), nullable=False),
    Column("last_seen", UtcDateTime(), nullable=False),
    Column("deleted_at", UtcDateTime(), nullable=True),
    CheckConstraint(
        "status IN ('confirmed', 'tentative', 'cancelled')",
        name="ck_source_event_status",
    ),
    CheckConstraint(
        "julianday(end_utc) >= julianday(start_utc)",
        name="ck_source_event_end_after_start",
    ),
    UniqueConstraint(
        "calendar_id", "uid", "recurrence_id", name="uq_source_event_occurrence"
    ),
    Index("ix_source_event_start", "start_utc"),
    Index("ix_source_event_deleted", "deleted_at"),
)
"""Normalized occurrences, one row per (calendar, UID, RECURRENCE-ID).

Rows missing from a *complete* collection window get ``deleted_at``; they are
never removed, so change impact can compare against the prior commitment."""

sync_state = Table(
    "sync_state",
    metadata,
    Column(
        "calendar_id",
        String(128),
        ForeignKey("calendar_source.calendar_id"),
        primary_key=True,
    ),
    Column("sync_token", Text, nullable=True),
    Column("ctag", String(255), nullable=True),
    Column("last_full_sync", UtcDateTime(), nullable=True),
)
"""Per-calendar CalDAV sync token and collection tag."""

location = Table(
    "location",
    metadata,
    Column("location_id", String(64), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("ha_zone_entity_id", String(128), nullable=True),
    Column("address_ciphertext", LargeBinary, nullable=True),
    Column("latlon_ciphertext", LargeBinary, nullable=True),
    Column("verification_state", String(16), nullable=False, default="unverified"),
)
"""Places. Address and ``lat,lon`` are Fernet ciphertext only."""

vehicle = Table(
    "vehicle",
    metadata,
    Column("vehicle_id", String(64), primary_key=True),
    Column("display_name", String(120), nullable=False),
    Column("seat_capacity", Integer, nullable=True),
)
"""Vehicles; ``seat_capacity`` counts the driver and is None until verified."""

household = Table(
    "household",
    metadata,
    Column("household_id", String(64), primary_key=True),
    Column("label", String(120), nullable=False),
    Column("privacy_scope", String(32), nullable=False, default="external"),
)
"""Families of external passengers (labels only; contacts are people)."""

person = Table(
    "person",
    metadata,
    Column("person_id", String(64), primary_key=True),
    Column("display_name", String(120), nullable=False),
    Column("role", String(16), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("can_drive", Boolean, nullable=False, default=False),
    Column(
        "default_vehicle_id",
        String(64),
        ForeignKey("vehicle.vehicle_id"),
        nullable=True,
    ),
    Column("monitoring_enabled", Boolean, nullable=False, default=True),
    Column("monitoring_start", Date, nullable=True),
    Column("monitoring_end", Date, nullable=True),
    Column(
        "home_location_id",
        String(64),
        ForeignKey(_LOCATION_ID_FK),
        nullable=True,
    ),
    CheckConstraint("role IN ('parent', 'child', 'contact')", name="ck_person_role"),
    CheckConstraint(
        (
            "monitoring_start IS NULL OR monitoring_end IS NULL "
            "OR monitoring_end >= monitoring_start"
        ),
        name="ck_person_monitoring_range",
    ),
)
"""Family members and registered contacts (external drivers)."""

external_passenger = Table(
    "external_passenger",
    metadata,
    Column("passenger_id", String(64), primary_key=True),
    Column("display_first_name", String(60), nullable=False),
    Column(
        "household_id",
        String(64),
        ForeignKey("household.household_id"),
        nullable=False,
    ),
    Column("contact_id", String(64), ForeignKey(_PERSON_ID_FK), nullable=True),
    Column(
        "default_pickup_location_id",
        String(64),
        ForeignKey(_LOCATION_ID_FK),
        nullable=True,
    ),
    Column(
        "default_dropoff_location_id",
        String(64),
        ForeignKey(_LOCATION_ID_FK),
        nullable=True,
    ),
    Column("alternate_destination_ids", JSON, nullable=False, default=list),
    Column("active", Boolean, nullable=False, default=True),
    Column("inactive_since", Date, nullable=True),
    # The privacy spec requires a first name or nickname but defines no stricter
    # shape, so only non-empty (after trim) and the declared maximum length are
    # enforced; surnames cannot be detected by a CHECK.
    CheckConstraint(
        "length(trim(display_first_name)) >= 1 AND length(display_first_name) <= 60",
        name="ck_external_passenger_first_name",
    ),
)
"""Children of other families; ``inactive_since`` drives 90-day removal."""

alias = Table(
    "alias",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(16), nullable=False),
    Column("alias", String(120), nullable=False),
    Column("target_id", String(64), nullable=False),
    CheckConstraint(
        "kind IN ('person', 'passenger', 'location')", name="ck_alias_kind"
    ),
)
"""Alternative names. ``kind`` is ``person``, ``passenger`` or ``location``;
uniqueness is case-insensitive per kind."""

Index(
    "ux_alias_kind_alias",
    alias.c.kind,
    func.lower(alias.c.alias),
    unique=True,
)

transport_default = Table(
    "transport_default",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("person_id", String(64), ForeignKey(_PERSON_ID_FK), nullable=False),
    Column("weekdays", JSON, nullable=False),
    Column("direction", String(16), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("driver_id", String(64), ForeignKey(_PERSON_ID_FK), nullable=True),
    Column("effective_from", Date, nullable=True),
    Column("effective_to", Date, nullable=True),
    Column(
        "origin_location_id",
        String(64),
        ForeignKey(_LOCATION_ID_FK),
        nullable=False,
    ),
    Column(
        "destination_location_id",
        String(64),
        ForeignKey(_LOCATION_ID_FK),
        nullable=False,
    ),
    CheckConstraint(
        "direction IN ('to_school', 'from_school')",
        name="ck_transport_default_direction",
    ),
    CheckConstraint(
        "mode IN ('bike', 'bus', 'walk', 'parent', 'carpool')",
        name="ck_transport_default_mode",
    ),
    CheckConstraint(
        (
            "effective_from IS NULL OR effective_to IS NULL "
            "OR effective_to >= effective_from"
        ),
        name="ck_transport_default_effective_range",
    ),
)
"""Effective-dated school transport defaults; ``weekdays`` is a list of
integers (0 = Monday)."""

analysis_run = Table(
    "analysis_run",
    metadata,
    Column("run_id", String(36), primary_key=True),
    Column("trigger", String(16), nullable=False),
    Column("window_start", UtcDateTime(), nullable=False),
    Column("window_end", UtcDateTime(), nullable=False),
    Column("started_at", UtcDateTime(), nullable=False),
    Column("finished_at", UtcDateTime(), nullable=True),
    Column("status", String(24), nullable=False),
    Column("counts", JSON, nullable=False, default=dict),
    Column("rules_version", String(32), nullable=False),
    Column("error", String(500), nullable=True),
    CheckConstraint(
        "status IN ('clear', 'attention_required', 'incomplete', 'failed')",
        name="ck_analysis_run_status",
    ),
    CheckConstraint(
        "julianday(window_end) >= julianday(window_start)",
        name="ck_analysis_run_window",
    ),
)
"""One assessment run (hourly, daily, weekly or on demand)."""

finding = Table(
    "finding",
    metadata,
    Column("issue_id", String(16), primary_key=True),
    Column("rule_id", String(16), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("message", Text, nullable=False),
    Column("entity_keys", JSON, nullable=False, default=list),
    Column("suggestion", Text, nullable=True),
    Column("lifecycle", String(16), nullable=False),
    Column("first_seen", UtcDateTime(), nullable=False),
    Column("last_seen", UtcDateTime(), nullable=False),
    Column("resolved_at", UtcDateTime(), nullable=True),
    Column("run_id", String(36), ForeignKey("analysis_run.run_id"), nullable=True),
    CheckConstraint(
        "severity IN ('critical', 'warning', 'incomplete')",
        name="ck_finding_severity",
    ),
    CheckConstraint(
        "lifecycle IN ('new', 'ongoing', 'resolved', 'regressed')",
        name="ck_finding_lifecycle",
    ),
    Index("ix_finding_lifecycle", "lifecycle"),
    Index("ix_finding_rule", "rule_id"),
)
"""Current state of every finding by stable ``issue_id``; ``lifecycle`` is
new, ongoing, resolved or regressed."""
