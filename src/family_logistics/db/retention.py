"""Retention purges for calendar-derived data.

``source_event.location`` and ``source_event.description`` hold the calendar
entry's free text exactly as it arrived. That text is Restricted: it can carry
street addresses, names and notes about children. The normalized columns the
engine reasons over persist, but the raw text is removed 30 days after the event
ends or is deleted from its calendar.

``school_day`` holds the parsed school days of the Home Assistant snapshot
(Confidential). Every ingest replaces them, but if Home Assistant stops posting
the last snapshot would stay forever, so days that ended more than
``SCHOOL_DAY_RETENTION_DAYS`` ago are deleted.

``route_cache`` holds the ``ha_relay`` travel times (Confidential, keyed by
location id) and ``route_request`` the queue behind them. Entries older than
``ROUTE_CACHE_RETENTION_DAYS`` and requests past their expiry are deleted
(privacy spec, section 7).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, literal, or_, select, update

from family_logistics.core.exceptions import ValidationError
from family_logistics.db import schema
from family_logistics.db.column_types import UtcDateTime
from family_logistics.db.route_relay import CACHE_VALIDITY
from family_logistics.db.school_snapshots import PURGED_CONTENT_HASH

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

RAW_TEXT_RETENTION_DAYS = 30

# #ASSUME: Security: proposed retention for parsed school days (defaults
# register, pending owner approval); the privacy spec lists no period for the
# HA snapshot yet. Past school days serve no assessment (the window starts
# today), so the period only bounds how long a stale snapshot lingers.
# #VERIFY: owner approves the period; the scheduler (M3) must call
# ``purge_expired_school_days`` with ``purge_raw_source_text``.
SCHOOL_DAY_RETENTION_DAYS = 30

ROUTE_CACHE_RETENTION_DAYS = CACHE_VALIDITY.days
"""Route cache entries are kept for the 28 days they can still be served."""


def purge_raw_source_text(connection: Connection, *, now: datetime) -> int:
    """Null the raw ``location`` and ``description`` of events past retention.

    A row is purged when ``COALESCE(deleted_at, end_utc)`` is more than
    ``RAW_TEXT_RETENTION_DAYS`` days before ``now`` and at least one of the two
    columns still holds text. Only those two columns change: ``summary`` and
    every normalized column are kept and no row is deleted. A second call with
    the same ``now`` changes nothing.

    The caller owns the transaction; this function neither commits nor rolls
    back.

    Args:
        connection: An open connection, normally inside ``engine.begin()``.
        now: The reference instant. It must be timezone-aware.

    Returns:
        The number of rows whose text was cleared.

    Raises:
        ValidationError: If ``now`` is a naive datetime. The message does not
            contain the value.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "now must be a timezone-aware datetime"
        raise ValidationError(msg, field="now")
    # #CRITICAL: security: raw calendar notes may hold addresses and children's
    # data and must not outlive the retention period.
    # #VERIFY: the scheduler (M3) must call this on every run; tests pin the
    # 30-day boundary and that only location and description are cleared.
    # #EDGE: data integrity: julianday returns NULL for text it cannot parse and
    # a NULL comparison never matches, so a row with an unparseable deleted_at
    # or end_utc is not purged. UtcDateTime writes always parse; only a raw SQL
    # writer could store such a value.
    # #VERIFY: write these columns through Core or the ORM only.
    cutoff = now - timedelta(days=RAW_TEXT_RETENTION_DAYS)
    table = schema.source_event
    # SQLite keeps no timestamp type: UtcDateTime stores UTC wall-clock text.
    # The cutoff is wrapped in ``literal(..., UtcDateTime())`` so the type's bind
    # processing (aware to UTC, then the DateTime storage format) is applied to
    # it explicitly instead of relying on type inference through COALESCE.
    # Both sides then go through ``julianday``, as the CHECK constraints do, so
    # the comparison is numeric and does not depend on text ordering.
    expired = func.julianday(func.coalesce(table.c.deleted_at, table.c.end_utc)) < (
        func.julianday(literal(cutoff, UtcDateTime()))
    )
    result = connection.execute(
        update(table)
        .where(expired)
        .where(or_(table.c.location.is_not(None), table.c.description.is_not(None)))
        .values(location=None, description=None)
    )
    return result.rowcount


def purge_expired_school_days(connection: Connection, *, now: datetime) -> int:
    """Delete parsed school days that ended more than the retention period ago.

    The ``school_snapshot`` freshness row is kept, so the snapshot still ages
    out through the staleness check (DATA-011) rather than disappearing. When
    any day is deleted, the same transaction updates the row's ``day_count``
    and replaces its ``content_hash`` with ``PURGED_CONTENT_HASH``, so the row
    never describes days that are gone and an identical re-post is not
    reported as unchanged. The caller owns the transaction.

    Args:
        connection: An open connection, normally inside ``engine.begin()``.
        now: The reference instant. It must be timezone-aware.

    Returns:
        The number of rows deleted.

    Raises:
        ValidationError: If ``now`` is a naive datetime. The message does not
            contain the value.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "now must be a timezone-aware datetime"
        raise ValidationError(msg, field="now")
    cutoff = now - timedelta(days=SCHOOL_DAY_RETENTION_DAYS)
    table = schema.school_day
    expired = func.julianday(table.c.end_utc) < func.julianday(
        literal(cutoff, UtcDateTime())
    )
    purged = connection.execute(delete(table).where(expired)).rowcount
    if purged:
        # #CRITICAL: Data integrity: the snapshot row must describe exactly the
        # rows left, or an identical re-post would read as "unchanged" and
        # leave the purged days missing.
        # #VERIFY: tests/unit/test_school_snapshot_repo.py::TestRetention.
        remaining = select(func.count()).select_from(table).scalar_subquery()
        connection.execute(
            update(schema.school_snapshot).values(
                day_count=remaining, content_hash=PURGED_CONTENT_HASH
            )
        )
    return purged


def purge_expired_route_cache(connection: Connection, *, now: datetime) -> int:
    """Delete route cache entries observed more than 28 days before ``now``.

    An entry exactly 28 days old is kept: the provider still serves it. The
    caller owns the transaction. Not scheduled yet; the scheduler (M3) must call
    it with the other purges.

    Args:
        connection: An open connection, normally inside ``engine.begin()``.
        now: The reference instant. It must be timezone-aware.

    Returns:
        The number of entries deleted.

    Raises:
        ValidationError: If ``now`` is a naive datetime. The message does not
            contain the value.
    """
    # #ASSUME: Security: cached travel times between named places are
    # Confidential (privacy spec); the retention period equals the validity
    # window, so nothing is kept that could no longer be served.
    # #VERIFY: the scheduler (M3) must call this on every run; tests pin the
    # 28-day boundary.
    _require_aware(now)
    cutoff = now - timedelta(days=ROUTE_CACHE_RETENTION_DAYS)
    table = schema.route_cache
    expired = func.julianday(table.c.observed_at) < func.julianday(
        literal(cutoff, UtcDateTime())
    )
    return connection.execute(delete(table).where(expired)).rowcount


def purge_expired_route_requests(connection: Connection, *, now: datetime) -> int:
    """Delete route requests whose expiry has passed, answered or not.

    Answered rows only serve to recognise an identical re-post, which is moot
    once the request has expired. The caller owns the transaction. Not
    scheduled yet.

    Args:
        connection: An open connection, normally inside ``engine.begin()``.
        now: The reference instant. It must be timezone-aware.

    Returns:
        The number of requests deleted.

    Raises:
        ValidationError: If ``now`` is a naive datetime. The message does not
            contain the value.
    """
    _require_aware(now)
    table = schema.route_request
    expired = func.julianday(table.c.expires_at) < func.julianday(
        literal(now, UtcDateTime())
    )
    return connection.execute(delete(table).where(expired)).rowcount


def _require_aware(now: datetime) -> None:
    """Refuse a naive reference instant without echoing it.

    Args:
        now: The reference instant.

    Raises:
        ValidationError: If ``now`` is naive.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "now must be a timezone-aware datetime"
        raise ValidationError(msg, field="now")
