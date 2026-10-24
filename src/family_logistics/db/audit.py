"""Append-only audit repository.

Every registry change and confirmation-state change is recorded as one row:
when, who (an actor identifier such as an Authentik username or ``system``),
which object (an internal reference such as ``trip:<id>``), what action, and
SHA-256 hashes of the state before and after. The state itself is never stored
here; only the hashes are.

#CRITICAL: Security: the hashes are unkeyed SHA-256, so they are pseudonymous,
not anonymous. Anyone holding the table can confirm a guess of a low-entropy
state (a name, a street address) by hashing the guess. Actor, object_ref,
action and correlation_id are stored as given, so they must be internal
identifiers, never names; the code checks their length, not their meaning.
Treat the table as Confidential audit evidence (privacy spec section 2), not as
anonymised data.
#VERIFY: callers put protected values into a state only as a keyed digest
(``AddressCipher``) and never as ``SensitiveStr``, which ``state_hash``
refuses; whether to key the audit hash itself is an open owner decision.

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
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import insert, select

from family_logistics.core.exceptions import DatabaseError, ValidationError
from family_logistics.db.schema import audit_log

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

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


def state_hash(state: object | None) -> str | None:
    """Hash a JSON-serialisable state canonically.

    Keys are sorted, separators are compact, non-ASCII is escaped, and dates,
    times and datetimes are encoded as ISO 8601. Anything else is refused, so
    the same state always has the same hash.

    Args:
        state: State to hash, or None when there is no state (create/delete).

    Returns:
        Hex SHA-256 of the canonical JSON, or None for no state.

    Raises:
        ValidationError: For a value with no canonical encoding, dictionary
            keys of mixed types, or NaN or infinity. The message names types,
            never values.
    """
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
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One recorded audit row.

    Attributes:
        recorded_at: UTC time of the change (timezone-aware).
        actor: Who made the change.
        object_ref: Internal reference of the changed object.
        action: What happened (for example ``create``, ``update``).
        prior_hash: Hash of the state before, if any.
        new_hash: Hash of the state after, if any.
        correlation_id: Request or run correlation ID, if any.
    """

    recorded_at: datetime
    actor: str
    object_ref: str
    action: str
    prior_hash: str | None
    new_hash: str | None
    correlation_id: str | None

    def __post_init__(self) -> None:
        """Reject a naive timestamp or a hash that is not 64 lowercase hex.

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


class AuditRepository:
    """Writes and reads the append-only audit log.

    Args:
        engine: The application engine (schema at head).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

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
        """Append one audit row with hashes of the prior and new state.

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
            The row commits in its own transaction, separately from the change
            it describes. A reference field that is empty or longer than its
            column, or a state ``state_hash`` cannot encode, raises
            ValidationError. Nothing is written in either case.
        """
        _check_fields(actor, object_ref, action, correlation_id)
        entry = AuditEntry(
            recorded_at=datetime.now(UTC),
            actor=actor,
            object_ref=object_ref,
            action=action,
            prior_hash=state_hash(prior_state),
            new_hash=state_hash(new_state),
            correlation_id=correlation_id,
        )
        with self._engine.begin() as connection:
            connection.execute(
                insert(audit_log).values(
                    recorded_at=entry.recorded_at,
                    actor=entry.actor,
                    object_ref=entry.object_ref,
                    action=entry.action,
                    prior_hash=entry.prior_hash,
                    new_hash=entry.new_hash,
                    correlation_id=entry.correlation_id,
                )
            )
        return entry

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
    )
