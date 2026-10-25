"""Column types shared by the database schema."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, LargeBinary
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC timestamp column, wrapping ``DateTime(timezone=True)``.

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


# A Fernet token is url-safe base64 of: version byte 0x80, an 8-byte big-endian
# timestamp, a 16-byte IV, the ciphertext (a multiple of 16 bytes) and a 32-byte
# HMAC. The first six characters encode 0x80 followed by the four high timestamp
# bytes, which stay zero until the year 2106, so every token starts "gAAAAA".
# The smallest token (empty plaintext) is 73 bytes, which is 100 characters of
# padded base64.
_FERNET_SHAPE = re.compile(r"gAAAAA[A-Za-z0-9_-]*={0,2}")
_FERNET_MIN_LENGTH = 100


def _ascii_text(value: object) -> str | None:
    """Return ``value`` as ASCII text, or None if it is not ``str`` or ASCII bytes.

    Args:
        value: The candidate value.

    Returns:
        The text, or None when the type or the encoding is wrong.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("ascii")
        except UnicodeDecodeError:
            return None
    return None


class FernetToken(TypeDecorator[str]):
    """A protected value column that only accepts Fernet-shaped token text.

    The Python side is ``str`` so it round-trips with ``AddressCipher.encrypt``
    (which returns token text) and ``AddressCipher.decrypt`` (which takes it).
    The stored form is the token's ASCII bytes in a ``LargeBinary`` column. On
    write, ``None`` passes through, ``str`` (ASCII) and ``bytes`` are accepted,
    and anything else, or any value that is not shaped like a Fernet token
    (``gAAAAA`` prefix, url-safe base64 alphabet with optional ``=`` padding, at
    least 100 characters), raises ``ValueError``. The error text never contains
    the offending value, so a plaintext address passed by mistake does not reach
    logs or API responses.

    # #CRITICAL: Security: this is a shape check, not authentication. Checking
    # the HMAC needs the key, which a column type must not hold, so a forged
    # value that merely looks like a token passes. It stops the accidental
    # plaintext write, which is the failure that exposes an address.
    # #VERIFY: only ``core.crypto.AddressCipher`` produces values for these
    # columns; a raw SQL writer is limited by the migration CHECK constraints,
    # which test the blob type and the ``gAAAAA`` prefix but not the HMAC.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: object, dialect: Dialect) -> bytes | None:
        """Reject anything that is not Fernet-shaped and store its ASCII bytes.

        Args:
            value: Token text or bytes, or None.
            dialect: The active dialect (unused).

        Returns:
            The token as ASCII bytes, or None.

        Raises:
            ValueError: If ``value`` is not None, ``str`` or ASCII ``bytes``, or
                is not shaped like a Fernet token. The message never contains
                the value.
        """
        del dialect
        if value is None:
            return None
        text = _ascii_text(value)
        if (
            text is None
            or len(text) < _FERNET_MIN_LENGTH
            or _FERNET_SHAPE.fullmatch(text) is None
        ):
            msg = "protected value rejected: not a Fernet token"
            raise ValueError(msg)
        return text.encode("ascii")

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> str | None:
        """Return the stored token bytes as text.

        Args:
            value: The stored bytes, or None.
            dialect: The active dialect (unused).

        Returns:
            The token as ``str``, or None.
        """
        del dialect
        if value is None:
            return None
        return bytes(value).decode("ascii")
