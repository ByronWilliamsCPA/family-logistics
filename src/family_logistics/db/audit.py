"""Append-only audit repository.

Every registry change and confirmation-state change is recorded as one row:
when, who (an actor identifier such as an Authentik username or ``system``),
which object (an internal reference such as ``trip:<id>``), what action,
HMAC-SHA256 digests of the state before and after, and the version of the key
that produced them. The state itself is never stored here; only the digests are.

#CRITICAL: Security: the digests are HMAC-SHA256 under ``FL_AUDIT_KEY`` (32
random bytes, never stored in the database), so holding the table alone does
not let anyone confirm a guess of a low-entropy state (a name, a street
address) by hashing it. There is no unkeyed fallback: ``state_hash`` requires
the key and ``AuditRepository.record`` raises ``ConfigurationError`` without
one. The rows stay pseudonymous: whoever also holds the key can still confirm a
guess, and actor, object_ref, action and correlation_id are stored as given, so
they must be internal identifiers, never names; the code checks their length,
not their meaning. ``hash_key_version`` is stored per row so a future rotation
can tell which key produced a digest. Treat the table as Confidential audit
evidence (privacy spec section 2), not as anonymised data, and keep the key in
the secret store, separate from ``FL_DATA_KEY``.
#VERIFY: tests/unit/test_audit.py::TestStateHash (same key and input give the
same digest, another key does not, it differs from plain SHA-256, the key is
checked) and TestRecord (key required, version persisted); callers put protected
values into a state only as a keyed digest (``AddressCipher``) and never as
``SensitiveStr``, which ``state_hash`` refuses.

#CRITICAL: Data integrity: the table is append-only. The repository exposes no
update or delete, and migration 0001's triggers abort, on every connection,
any UPDATE, any INSERT that would overwrite an existing row (``INSERT OR
REPLACE``, ``REPLACE``, upsert) and any DELETE of a row younger than 365 days
(the privacy spec's audit retention; older rows may be purged). Schema changes
(DROP TRIGGER, DROP TABLE, the 0001 downgrade) are not blocked.
#VERIFY: tests/unit/test_audit.py covers each statement on SQLAlchemy and on a
bare sqlite3 connection; downgrades are an explicit, confirmed operator action.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Self

from sqlalchemy import insert, select

from family_logistics.core.config import decode_audit_key
from family_logistics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    ValidationError,
)
from family_logistics.db.schema import audit_log

if TYPE_CHECKING:
    from pydantic import SecretStr
    from sqlalchemy.engine import Connection, Engine

    from family_logistics.core.config import Settings

MAX_ACTOR_LENGTH = 64
MAX_REF_LENGTH = 128
MAX_ACTION_LENGTH = 32
MAX_CORRELATION_ID_LENGTH = 64
"""Field limits; they equal the ``audit_log`` column widths and CHECKs."""

_HASH = re.compile(r"[0-9a-f]{64}")


def _canonical(value: object) -> str:
    """Encode a JSON-incompatible value canonically, or refuse it.

    Args:
        value: A value ``json`` cannot encode by itself.

    Returns:
        The ISO 8601 text of a date, time or datetime.

    Raises:
        TypeError: For any other type (sets, arbitrary objects, ``SensitiveStr``),
            whose text form is unstable across processes or masks the value.
    """
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    msg = f"state_hash cannot canonically encode {type(value).__name__}"
    raise TypeError(msg)


def _key_bytes(key: SecretStr) -> bytes:
    """Return the raw HMAC key bytes of an audit key.

    Args:
        key: ``Settings.audit_key``.

    Returns:
        The 32 key bytes.

    Raises:
        ConfigurationError: If the key is not 32 bytes of url-safe base64. The
            message never echoes the key.
    """
    try:
        return decode_audit_key(key.get_secret_value())
    except ValueError:
        msg = "FL_AUDIT_KEY must be 32 random bytes as url-safe base64 (44 characters)"
        # Raised outside the handler so the key-bearing error is not chained.
        raise ConfigurationError(msg) from None


def state_hash(state: object | None, *, key: SecretStr) -> str | None:
    """Digest a JSON-serialisable state canonically, keyed with HMAC-SHA256.

    Keys are sorted, separators are compact, non-ASCII is escaped, and dates,
    times and datetimes are encoded as ISO 8601. Anything else is refused, so
    the same state under the same key always has the same digest. There is no
    unkeyed mode.

    Args:
        state: State to hash, or None when there is no state (create/delete).
        key: ``Settings.audit_key``; checked even when ``state`` is None.

    Returns:
        64 lowercase hex characters of HMAC-SHA256 over the canonical JSON, or
        None for no state.

    Raises:
        ValidationError: For a value with no canonical encoding, dictionary
            keys of mixed types, or NaN or infinity. The message names types,
            never values.

    Note:
        A ``key`` that is not 32 bytes of url-safe base64 raises
        ConfigurationError, even when ``state`` is None; the message never
        echoes the key.
    """
    key_bytes = _key_bytes(key)
    if state is None:
        return None
    try:
        canonical = json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_canonical,
        )
    except (TypeError, ValueError) as exc:
        msg = f"state cannot be hashed canonically: {exc}"
        raise ValidationError(msg, field="state") from exc
    return hmac.new(key_bytes, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One recorded audit row.

    Attributes:
        recorded_at: UTC time of the change (timezone-aware).
        actor: Who made the change.
        object_ref: Internal reference of the changed object.
        action: What happened (for example ``create``, ``update``).
        prior_hash: Keyed digest of the state before, if any.
        new_hash: Keyed digest of the state after, if any.
        correlation_id: Request or run correlation ID, if any.
        hash_key_version: Version of ``FL_AUDIT_KEY`` that produced the digests.
    """

    recorded_at: datetime
    actor: str
    object_ref: str
    action: str
    prior_hash: str | None
    new_hash: str | None
    correlation_id: str | None
    hash_key_version: int

    def __post_init__(self) -> None:
        """Reject a naive timestamp, a malformed hash or a key version below 1.

        Raises:
            ValidationError: If an invariant does not hold.
        """
        if self.recorded_at.utcoffset() is None:
            msg = "audit recorded_at must be timezone-aware"
            raise ValidationError(msg, field="recorded_at")
        for name, value in (
            ("prior_hash", self.prior_hash),
            ("new_hash", self.new_hash),
        ):
            if value is not None and not _HASH.fullmatch(value):
                msg = f"audit {name} must be 64 lowercase hex characters"
                raise ValidationError(msg, field=name)
        if self.hash_key_version < 1:
            msg = "audit hash_key_version must be at least 1"
            raise ValidationError(msg, field="hash_key_version")


def _check_fields(
    actor: str, object_ref: str, action: str, correlation_id: str | None
) -> None:
    """Check reference fields against the column widths.

    Args:
        actor: Who made the change.
        object_ref: Internal reference of the changed object.
        action: What happened.
        correlation_id: Request or run correlation ID, if any.

    Raises:
        ValidationError: If a field is empty or longer than its column.
    """
    fields = (
        ("actor", actor, MAX_ACTOR_LENGTH),
        ("object_ref", object_ref, MAX_REF_LENGTH),
        ("action", action, MAX_ACTION_LENGTH),
        ("correlation_id", correlation_id, MAX_CORRELATION_ID_LENGTH),
    )
    for name, value, limit in fields:
        if value is None and name == "correlation_id":
            continue
        if not value or len(value) > limit:
            msg = f"audit {name} must be 1-{limit} characters"
            raise ValidationError(msg, field=name)


def record_on(  # noqa: PLR0913 - keyword-only audit fields
    connection: Connection,
    *,
    key: SecretStr | None,
    key_version: int,
    actor: str,
    object_ref: str,
    action: str,
    prior_state: object | None = None,
    new_state: object | None = None,
    correlation_id: str | None = None,
) -> AuditEntry:
    """Append one audit row on an open connection (the caller's transaction).

    Use this when the audited change and its audit row must commit or roll
    back together.

    Args:
        connection: Connection inside the caller's transaction.
        key: ``Settings.audit_key``; None raises ``ConfigurationError``.
        key_version: ``Settings.audit_key_version``, stored with the row.
        actor: Who made the change.
        object_ref: Internal reference; must not contain personal data.
        action: What happened.
        prior_state: State before the change (hashed, not stored).
        new_state: State after the change (hashed, not stored).
        correlation_id: Request or run correlation ID.

    Returns:
        The recorded entry.

    Raises:
        ConfigurationError: If ``key`` is None.

    Note:
        A reference field that is empty or longer than its column, a state
        ``state_hash`` cannot encode, or a ``key_version`` below 1 raises
        ValidationError; a malformed audit key also raises ConfigurationError.
        Nothing is written in any of these cases.
    """
    if key is None:
        msg = "FL_AUDIT_KEY is required before recording audit entries"
        raise ConfigurationError(msg)
    _check_fields(actor, object_ref, action, correlation_id)
    entry = AuditEntry(
        recorded_at=datetime.now(UTC),
        actor=actor,
        object_ref=object_ref,
        action=action,
        prior_hash=state_hash(prior_state, key=key),
        new_hash=state_hash(new_state, key=key),
        correlation_id=correlation_id,
        hash_key_version=key_version,
    )
    connection.execute(
        insert(audit_log).values(
            recorded_at=entry.recorded_at,
            actor=entry.actor,
            object_ref=entry.object_ref,
            action=entry.action,
            prior_hash=entry.prior_hash,
            new_hash=entry.new_hash,
            correlation_id=entry.correlation_id,
            hash_key_version=entry.hash_key_version,
        )
    )
    return entry


class AuditRepository:
    """Writes and reads the append-only audit log.

    Args:
        engine: The application engine (schema at head).
        key: ``Settings.audit_key``. May be None for read-only use; ``record``
            then raises ``ConfigurationError`` instead of writing anything.
        key_version: ``Settings.audit_key_version``, stored with every row.

    Raises:
        ConfigurationError: If ``key_version`` is below 1.
    """

    def __init__(
        self, engine: Engine, *, key: SecretStr | None, key_version: int = 1
    ) -> None:
        if key_version < 1:
            msg = "audit key version must be at least 1"
            raise ConfigurationError(msg)
        self._engine = engine
        self._key = key
        self._key_version = key_version

    @classmethod
    def from_settings(cls, engine: Engine, settings: Settings) -> Self:
        """Build a repository keyed from the application settings.

        Args:
            engine: The application engine (schema at head).
            settings: Application settings (``audit_key``, ``audit_key_version``).

        Returns:
            A repository that keys and versions every digest it records.
        """
        return cls(
            engine, key=settings.audit_key, key_version=settings.audit_key_version
        )

    def record(  # noqa: PLR0913 - keyword-only audit fields
        self,
        *,
        actor: str,
        object_ref: str,
        action: str,
        prior_state: object | None = None,
        new_state: object | None = None,
        correlation_id: str | None = None,
    ) -> AuditEntry:
        """Append one audit row with keyed digests of the prior and new state.

        Args:
            actor: Who made the change.
            object_ref: Internal reference; must not contain personal data.
            action: What happened.
            prior_state: State before the change (hashed, not stored).
            new_state: State after the change (hashed, not stored).
            correlation_id: Request or run correlation ID.

        Returns:
            The recorded entry.

        Note:
            Runs ``record_on`` in its own transaction, so the row commits
            separately from the change it describes. Without an audit key it
            raises ConfigurationError, and an empty or over-long reference
            field, a state ``state_hash`` cannot encode, or a malformed key
            raises ValidationError or ConfigurationError from there. Nothing is
            written in any of these cases.
        """
        with self._engine.begin() as connection:
            return record_on(
                connection,
                key=self._key,
                key_version=self._key_version,
                actor=actor,
                object_ref=object_ref,
                action=action,
                prior_state=prior_state,
                new_state=new_state,
                correlation_id=correlation_id,
            )

    def entries_for(self, object_ref: str) -> list[AuditEntry]:
        """Return the audit trail of one object, oldest first.

        Args:
            object_ref: Internal reference of the object.

        Returns:
            Its audit entries.
        """
        query = (
            select(
                audit_log.c.recorded_at,
                audit_log.c.actor,
                audit_log.c.object_ref,
                audit_log.c.action,
                audit_log.c.prior_hash,
                audit_log.c.new_hash,
                audit_log.c.correlation_id,
                audit_log.c.hash_key_version,
            )
            .where(audit_log.c.object_ref == object_ref)
            .order_by(audit_log.c.id)
        )
        with self._engine.connect() as connection:
            rows: list[dict[str, object]] = [
                dict(mapping) for mapping in connection.execute(query).mappings()
            ]
        return [_entry_from_row(row) for row in rows]


def _optional_text(value: object) -> str | None:
    """Return a column value as text, keeping NULL as None."""
    return None if value is None else str(value)


def _key_version(value: object) -> int:
    """Return the stored key version, refusing anything but an integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = "audit_log.hash_key_version is not an integer"
        raise DatabaseError(msg, operation="read", table="audit_log")
    return value


def _entry_from_row(row: dict[str, object]) -> AuditEntry:
    """Convert one database row into an AuditEntry."""
    recorded_at = row["recorded_at"]
    if not isinstance(recorded_at, datetime):
        msg = "audit_log.recorded_at is not a timestamp"
        raise DatabaseError(msg, operation="read", table="audit_log")
    # SQLite keeps no offset; rows are written in UTC (see ``record``).
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)
    return AuditEntry(
        recorded_at=recorded_at,
        actor=str(row["actor"]),
        object_ref=str(row["object_ref"]),
        action=str(row["action"]),
        prior_hash=_optional_text(row["prior_hash"]),
        new_hash=_optional_text(row["new_hash"]),
        correlation_id=_optional_text(row["correlation_id"]),
        hash_key_version=_key_version(row["hash_key_version"]),
    )
