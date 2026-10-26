"""SQLAlchemy table definitions.

The schema itself is owned by the Alembic migrations in
``family_logistics/db/migrations``. These definitions mirror the head
revision's columns for query building. The pilot-core tables (revision 0002)
also declare their indexes, unique constraints and named CHECK constraints
here, with the same names and SQL text as the migration, so metadata and
migration stay in parity. ``audit_log`` is the exception: its CHECK constraints
and its append-only triggers exist only in migration 0001. The triggers that
remove ``alias`` rows when a person, external passenger or location is deleted
(migration 0002) are likewise migration-only. So ``metadata.create_all`` does
not build a production schema. Always create databases with
``migrations_api.upgrade``.

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
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)

from family_logistics.db.column_types import FernetToken, UtcDateTime

metadata = MetaData()

_PERSON_ID_FK = "person.person_id"
_LOCATION_ID_FK = "location.location_id"


def _fernet_check(column: str) -> str:
    """Return the CHECK text that keeps a ciphertext column Fernet-shaped.

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
# Registry tables hold household data. Registry addresses and coordinates
# (``location.address_ciphertext`` and ``location.latlon_ciphertext``) live only
# as Fernet ciphertext (``core.crypto``); ``FernetToken`` and a CHECK constraint
# reject anything else. This does not cover ``source_event.location``, which is
# the raw calendar text: Restricted, and nulled 30 days after the event ends or
# is deleted by ``db.retention.purge_raw_source_text``. Names of external
# children are first-name labels keyed by an internal passenger ID. Aliases for
# people, passengers and locations live in the single ``alias`` table.

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

Rows missing from a *complete* collection window get ``deleted_at``; the rows
are never removed, so change impact can compare against the prior commitment.
``location`` and ``description`` are raw calendar text (Restricted): they are
nulled 30 days after the event ends or is deleted by
``db.retention.purge_raw_source_text``, while ``summary`` and the normalized
fields persist."""

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
    Column("address_ciphertext", FernetToken(), nullable=True),
    Column("latlon_ciphertext", FernetToken(), nullable=True),
    Column("verification_state", String(16), nullable=False, default="unverified"),
    # The Fernet token starts with version byte 0x80 and a small timestamp, which
    # is "gAAAAA" in base64 (hex 674141414141). The column stores the token's
    # ASCII bytes, so the blob type and that prefix keep plaintext out.
    CheckConstraint(
        _fernet_check("address_ciphertext"),
        name="ck_location_address_ciphertext_fernet",
    ),
    CheckConstraint(
        _fernet_check("latlon_ciphertext"),
        name="ck_location_latlon_ciphertext_fernet",
    ),
)
"""Places. Address and ``lat,lon`` are Fernet ciphertext only, enforced by the
``FernetToken`` column type and by CHECK constraints on the stored blob."""

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
    # A passenger is either active with no end date or inactive with one.
    CheckConstraint(
        (
            "(active = 1 AND inactive_since IS NULL) "
            "OR (active = 0 AND inactive_since IS NOT NULL)"
        ),
        name="ck_external_passenger_active_inactive_since",
    ),
)
"""Children of other families. ``inactive_since`` drives removal 180 days after
the carpool ends (180, not 90, so a summer break does not trigger removal). The
removal job arrives with the scheduler in M3; until then rows are kept."""

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
uniqueness is case-insensitive per kind. ``target_id`` is polymorphic and has no
foreign key, so migration-only triggers delete a target's aliases when the
person, external passenger or location row is deleted."""

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
    # Added by revision 0003_trip_snapshot (the table itself is from 0002).
    Index("ix_analysis_run_started_at", "started_at"),
)
"""One assessment run; ``trigger`` is weekly, daily, incremental or on_demand
(``services.assessments.TRIGGERS``; the database has no CHECK on it)."""

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
    # Added by revision 0003_trip_snapshot (the table itself is from 0002).
    Index("ix_finding_run_id", "run_id"),
)
"""Current state of every finding by stable ``issue_id``; ``lifecycle`` is
new, ongoing, resolved or regressed."""

# ---------------------------------------------------------------------------
# Trip snapshot (revision 0003_trip_snapshot)
# ---------------------------------------------------------------------------
# The rides of the latest run that did not fail, for change impact. Revision 0003 also
# adds ``ix_analysis_run_started_at`` and ``ix_finding_run_id``, declared above
# on the 0002 tables they index.

trip_snapshot = Table(
    "trip_snapshot",
    metadata,
    Column("trip_key", String(512), primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("run_id", String(36), ForeignKey("analysis_run.run_id"), nullable=True),
    Column("updated_at", UtcDateTime(), nullable=False),
)
"""Rides accepted by the latest completed run, keyed by the source occurrence.

The next run compares its rides to these to find a passenger removed or a
destination changed (change impact). ``complete_run`` replaces the whole table
with the rides of the latest run that did not fail; a failed run leaves it
untouched."""

# ---------------------------------------------------------------------------
# School snapshot (revision 0004_school_snapshot)
# ---------------------------------------------------------------------------
# The parsed school days of the latest Home Assistant snapshot (ADR 0007) and
# its freshness. The raw ``calendar.get_events`` payload is never stored: no
# summary, description, location or zone column exists here (children's data,
# privacy class C). ``db.school_snapshots`` replaces both tables in one
# transaction.

school_snapshot = Table(
    "school_snapshot",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("fetched_at", UtcDateTime(), nullable=False),
    Column("received_at", UtcDateTime(), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("day_count", Integer, nullable=False),
    CheckConstraint("id = 1", name="ck_school_snapshot_singleton"),
    CheckConstraint(
        "julianday(fetched_at) <= julianday(received_at)",
        name="ck_school_snapshot_fetched_not_after_received",
    ),
    CheckConstraint(
        "length(content_hash) = 64", name="ck_school_snapshot_content_hash"
    ),
    CheckConstraint("day_count >= 0", name="ck_school_snapshot_day_count"),
)
"""Freshness of the stored snapshot: a single row (``id = 1``). ``fetched_at``
is the earlier of Home Assistant's ``fetched_at`` and ``received_at`` (server
time), so data sent with a clock ahead of the server never reads fresher than
it is. ``content_hash`` (SHA-256 of the days and the fetch time as sent) makes
an identical re-post a no-op."""

school_day = Table(
    "school_day",
    metadata,
    Column("person_id", String(64), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("location_id", String(64), nullable=False),
    Column("start_utc", UtcDateTime(), nullable=False),
    Column("end_utc", UtcDateTime(), nullable=False),
    Column("kind", String(16), nullable=False),
    CheckConstraint(
        "kind IN ('normal', 'early_release', 'late_start', 'no_school', 'unknown')",
        name="ck_school_day_kind",
    ),
    CheckConstraint(
        "julianday(end_utc) >= julianday(start_utc)",
        name="ck_school_day_order",
    ),
)
"""One person's parsed school day (``domain.events.SchoolDay``). No foreign key
to ``person`` or ``location``: the rows are derived data, replaced wholesale on
every ingest and checked against the registry when they are built, so a
registry edit is never blocked by a stale snapshot row."""

# ---------------------------------------------------------------------------
# Route relay (revision 0005_route_relay)
# ---------------------------------------------------------------------------
# The ``ha_relay`` route provider (ADR 0006): Home Assistant prices legs and
# posts the results back, and the app caches them. Both tables are keyed by
# location id, never by address text, so a location edit invalidates exactly
# the right rows. No address, zone or endpoint text is stored: the endpoint
# sent to Home Assistant is resolved from the registry when a request is
# served. The ``trg_location_route_relay_invalidate`` trigger and the
# ``ON DELETE CASCADE`` keys (migration only) drop a location's rows when it
# changes or is deleted.

MAX_ROUTE_DURATION_SECONDS = 12 * 60 * 60
"""Largest travel time a cache row or an answered request may hold (12 hours).
The migration repeats the number in its CHECK constraints."""

route_cache = Table(
    "route_cache",
    metadata,
    Column(
        "origin_location_id",
        String(64),
        ForeignKey(_LOCATION_ID_FK, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "destination_location_id",
        String(64),
        ForeignKey(_LOCATION_ID_FK, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("weekday", Integer, primary_key=True),
    Column("bucket", Integer, primary_key=True),
    Column("duration_seconds", Integer, nullable=False),
    Column("observed_at", UtcDateTime(), nullable=False),
    CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_route_cache_weekday"),
    CheckConstraint("bucket BETWEEN 0 AND 95", name="ck_route_cache_bucket"),
    CheckConstraint(
        f"duration_seconds BETWEEN 1 AND {MAX_ROUTE_DURATION_SECONDS}",
        name="ck_route_cache_duration",
    ),
    CheckConstraint(
        "origin_location_id <> destination_location_id",
        name="ck_route_cache_distinct_endpoints",
    ),
)
"""One observed driving time per origin, destination, weekday (Monday is 0) and
15-minute bucket of local time of day (0 to 95). ``observed_at`` is the server's
receipt time, so the age rule never trusts the sender's clock."""

route_request = Table(
    "route_request",
    metadata,
    Column("request_id", String(36), primary_key=True),
    Column(
        "origin_location_id",
        String(64),
        ForeignKey(_LOCATION_ID_FK, ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "destination_location_id",
        String(64),
        ForeignKey(_LOCATION_ID_FK, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("weekday", Integer, nullable=False),
    Column("bucket", Integer, nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    Column("expires_at", UtcDateTime(), nullable=False),
    Column("status", String(8), nullable=False),
    Column("answered_duration_seconds", Integer, nullable=True),
    Column("answered_at", UtcDateTime(), nullable=True),
    CheckConstraint(
        "status IN ('pending', 'answered')", name="ck_route_request_status"
    ),
    CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_route_request_weekday"),
    CheckConstraint("bucket BETWEEN 0 AND 95", name="ck_route_request_bucket"),
    CheckConstraint(
        "origin_location_id <> destination_location_id",
        name="ck_route_request_distinct_endpoints",
    ),
    CheckConstraint(
        "julianday(expires_at) > julianday(created_at)",
        name="ck_route_request_expiry_after_creation",
    ),
    CheckConstraint(
        (
            "(status = 'pending' AND answered_duration_seconds IS NULL"
            " AND answered_at IS NULL)"
            " OR (status = 'answered' AND answered_at IS NOT NULL"
            f" AND answered_duration_seconds BETWEEN 1 AND {MAX_ROUTE_DURATION_SECONDS})"
        ),
        name="ck_route_request_answer",
    ),
    Index(
        "uq_route_request_pending",
        "origin_location_id",
        "destination_location_id",
        "weekday",
        "bucket",
        unique=True,
        sqlite_where=text("status = 'pending'"),
    ),
)
"""A leg the app needs priced. The app, not Home Assistant, decides what an
answer means: the cache key comes from this row, never from the sender. At most
one pending row exists per leg, weekday and bucket; answered rows stay until
they expire so an identical re-post is a no-op."""

# ---------------------------------------------------------------------------
# Notification state (revision 0006_notification_state)
# ---------------------------------------------------------------------------
# De-duplication, claims and the dispatch watermark of the notification service
# (ADR 0008, D9). Neither table holds personal data: subject keys are hashes,
# run ids and fixed names. ``run_id`` and ``episode_run_id`` have no foreign
# key because runs are purged on their own schedule. The CHECK text repeats the
# migration's, colons escaped for SQLAlchemy's bind-parameter syntax.

_NOTIFICATION_SUBJECT_CHECK = (
    "(length(subject_key) = 22 AND subject_key GLOB 'issue\\:[0-9a-f]*'"
    " AND substr(subject_key, 7) NOT GLOB '*[^0-9a-f]*'"
    " AND kind IN ('notice', 'urgent'))"
    " OR (subject_key = 'run-failed' AND kind IN ('notice', 'urgent'))"
    " OR (subject_key GLOB 'run\\:?*' AND length(subject_key) <= 40"
    " AND kind IN ('status', 'report'))"
    " OR (subject_key IN ('ops\\:run_failed', 'ops\\:run_incomplete')"
    " AND kind = 'ops')"
)

notification_state = Table(
    "notification_state",
    metadata,
    Column("subject_key", String(48), primary_key=True),
    Column("mode", String(16), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("tag", String(64), nullable=False),
    Column("rule_id", String(16), nullable=True),
    Column("severity", String(16), nullable=True),
    Column("is_open", Boolean, nullable=False),
    Column("delivered", Boolean, nullable=False, server_default=text("0")),
    Column("bypass_used", Boolean, nullable=False, server_default=text("0")),
    Column("episode_run_id", String(36), nullable=False),
    Column("send_count", Integer, nullable=False, server_default=text("0")),
    Column("failed_attempts", Integer, nullable=False, server_default=text("0")),
    Column("first_sent_at", UtcDateTime(), nullable=True),
    Column("last_sent_at", UtcDateTime(), nullable=True),
    Column("last_attempt_at", UtcDateTime(), nullable=True),
    Column("claim_token", String(36), nullable=True),
    Column("claimed_at", UtcDateTime(), nullable=True),
    Column("run_id", String(36), nullable=False),
    Column("evaluated_at", UtcDateTime(), nullable=False),
    Column("created_at", UtcDateTime(), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    CheckConstraint(
        "kind IN ('notice', 'urgent', 'status', 'report', 'ops')",
        name="ck_notification_state_kind",
    ),
    CheckConstraint(
        "mode IN ('shadow', 'production')", name="ck_notification_state_mode"
    ),
    CheckConstraint("tag GLOB 'fl-[a-z0-9]*'", name="ck_notification_state_tag"),
    CheckConstraint(
        "severity IS NULL OR severity IN ('critical', 'warning', 'incomplete')",
        name="ck_notification_state_severity",
    ),
    CheckConstraint(
        "send_count >= 0 AND failed_attempts >= 0",
        name="ck_notification_state_counts",
    ),
    CheckConstraint(
        "(claim_token IS NULL) = (claimed_at IS NULL)",
        name="ck_notification_state_claim",
    ),
    CheckConstraint(
        "delivered = 0 OR (first_sent_at IS NOT NULL AND last_sent_at IS NOT NULL)",
        name="ck_notification_state_delivered",
    ),
    CheckConstraint(_NOTIFICATION_SUBJECT_CHECK, name="ck_notification_state_subject"),
    Index("ix_notification_state_open", "is_open"),
    Index("ix_notification_state_updated_at", "updated_at"),
)
"""One row per ``(subject_key, mode)``: what was last delivered for the subject's
episode, whether it is open, and the claim of a push in flight. ``kind`` is the
kind last delivered, or the planned kind while undelivered; ``severity`` is the
severity last delivered (NULL until delivered)."""

notification_watermark = Table(
    "notification_watermark",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("evaluated_at", UtcDateTime(), nullable=False),
    Column("run_id", String(36), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    CheckConstraint("id = 1", name="ck_notification_watermark_singleton"),
)
"""The single row holding the newest outcome ``evaluated_at`` dispatched so far,
so an older outcome is refused as stale."""
