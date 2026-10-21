"""SQLAlchemy table definitions.

The schema itself is owned by the Alembic migrations in
``family_logistics/db/migrations``. These definitions mirror the head
revision's columns for query building; triggers, CHECK constraints and indexes
exist only in the migrations, so ``metadata.create_all`` does not build a
production schema. Always create databases with ``migrations_api.upgrade``.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
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
    Column("person_id", String(64), ForeignKey("person.person_id"), nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("last_success", DateTime(timezone=True), nullable=True),
    Column("last_error", String(500), nullable=True),
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
    Column("start_utc", DateTime(timezone=True), nullable=False),
    Column("end_utc", DateTime(timezone=True), nullable=False),
    Column("all_day", Boolean, nullable=False, default=False),
    Column("summary", Text, nullable=False, default=""),
    Column("location", Text, nullable=True),
    Column("description", Text, nullable=True),
    Column("status", String(16), nullable=False),
    Column("etag", String(255), nullable=True),
    Column("raw_hash", String(64), nullable=False),
    Column("is_override", Boolean, nullable=False, default=False),
    Column("series_uid_has_rrule", Boolean, nullable=False, default=False),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
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
    Column("last_full_sync", DateTime(timezone=True), nullable=True),
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
        ForeignKey("location.location_id"),
        nullable=True,
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
    Column("contact_id", String(64), ForeignKey("person.person_id"), nullable=True),
    Column(
        "default_pickup_location_id",
        String(64),
        ForeignKey("location.location_id"),
        nullable=True,
    ),
    Column(
        "default_dropoff_location_id",
        String(64),
        ForeignKey("location.location_id"),
        nullable=True,
    ),
    Column("alternate_destination_ids", JSON, nullable=False, default=list),
    Column("active", Boolean, nullable=False, default=True),
    Column("inactive_since", Date, nullable=True),
)
"""Children of other families; ``inactive_since`` drives 90-day removal."""

alias = Table(
    "alias",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(16), nullable=False),
    Column("alias", String(120), nullable=False),
    Column("target_id", String(64), nullable=False),
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
    Column("person_id", String(64), ForeignKey("person.person_id"), nullable=False),
    Column("weekdays", JSON, nullable=False),
    Column("direction", String(16), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("driver_id", String(64), ForeignKey("person.person_id"), nullable=True),
    Column("effective_from", Date, nullable=True),
    Column("effective_to", Date, nullable=True),
    Column(
        "origin_location_id",
        String(64),
        ForeignKey("location.location_id"),
        nullable=False,
    ),
    Column(
        "destination_location_id",
        String(64),
        ForeignKey("location.location_id"),
        nullable=False,
    ),
)
"""Effective-dated school transport defaults; ``weekdays`` is a list of
integers (0 = Monday)."""

analysis_run = Table(
    "analysis_run",
    metadata,
    Column("run_id", String(36), primary_key=True),
    Column("trigger", String(16), nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("status", String(24), nullable=False),
    Column("counts", JSON, nullable=False, default=dict),
    Column("rules_version", String(32), nullable=False),
    Column("error", String(500), nullable=True),
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
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("run_id", String(36), ForeignKey("analysis_run.run_id"), nullable=True),
    Index("ix_finding_lifecycle", "lifecycle"),
    Index("ix_finding_rule", "rule_id"),
)
"""Current state of every finding by stable ``issue_id``; ``lifecycle`` is
new, ongoing, resolved or regressed."""
