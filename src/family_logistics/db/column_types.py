"""Column types shared by the database schema."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC timestamp column.

    SQLite has no timestamp type and keeps no offset: ``DateTime(timezone=True)``
    stores the wall-clock text and returns a naive ``datetime``, so a value
    written as 22:00 in a non-UTC zone would read back as 22:00 UTC. This type
    makes the round trip lossless at the ORM and Core layer:

    - on write, a naive datetime raises ``ValueError`` and an aware one is
      converted to UTC before it is stored;
    - on read, a naive value from the driver is tagged as UTC (every writer of
      this type stores UTC, matching the ``domain.events`` contract and how
      ``db.audit`` reads ``audit_log.recorded_at``).

    # #ASSUME: Data integrity: SQLite stores naive text, so a raw SQL writer
    # (a bare ``sqlite3`` connection or a hand-written INSERT) bypasses this
    # type and is NOT protected; the database does not enforce UTC or offsets.
    # #VERIFY: tests insert through the ORM or Core only and assert the aware
    # UTC round trip; any non-UTC wall-clock text written by raw SQL reads back
    # as UTC and is wrong by its offset.
    """

    impl = DateTime
    cache_ok = True

    def __init__(self) -> None:
        """Wrap ``DateTime(timezone=True)``."""
        super().__init__(timezone=True)

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Reject naive datetimes and convert aware ones to UTC.

        Args:
            value: The datetime being stored, or None.
            dialect: The active dialect (unused).

        Returns:
            The value in UTC, or None.

        Raises:
            ValueError: If ``value`` is a naive datetime.
        """
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "naive datetime rejected: store timezone-aware datetimes only"
            raise ValueError(msg)
        return value.astimezone(UTC)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Attach UTC to a naive value returned by the driver.

        Args:
            value: The stored datetime, or None.
            dialect: The active dialect (unused).

        Returns:
            An aware UTC datetime, or None.
        """
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
