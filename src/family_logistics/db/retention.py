"""Retention purge for the raw calendar text kept on ``source_event``.

``source_event.location`` and ``source_event.description`` hold the calendar
entry's free text exactly as it arrived. That text is Restricted: it can carry
street addresses, names and notes about children. The normalized columns the
engine reasons over persist, but the raw text is removed 30 days after the event
ends or is deleted from its calendar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, literal, or_, update

from family_logistics.core.exceptions import ValidationError
from family_logistics.db import schema
from family_logistics.db.column_types import UtcDateTime

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

RAW_TEXT_RETENTION_DAYS = 30


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
