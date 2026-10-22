"""Append-only audit repository.

Every registry change and confirmation-state change is recorded as one row:
when, who (an actor identifier such as an Authentik username or ``system``),
which object (an internal reference such as ``trip:<id>``), what action, and
SHA-256 hashes of the state before and after. The state itself is never
stored by this repository.

Scope of the privacy claim: the row holds no state content, but it is not
anonymous. ``actor`` is a username (pseudonymous personal data), and
``object_ref`` is caller-supplied text that callers must keep to internal
IDs; nothing in the schema stops a caller from passing a name. The hashes are
unsalted, so the hash of a low-entropy state (one address, one name) can be
confirmed by anyone who guesses the state. Treat the table as Confidential
audit evidence (privacy spec section 2), not as anonymised data.

#CRITICAL: Data integrity: the table is append-only. The repository exposes
no update or delete, and database triggers (migration 0001) abort any UPDATE,
any DELETE, and any INSERT that reuses an existing id (which is how
``INSERT OR REPLACE`` would otherwise delete a row without firing the DELETE
trigger).
#VERIFY: tests/unit/test_audit.py::TestAudit::test_rows_cannot_change runs
UPDATE, DELETE, INSERT OR REPLACE, REPLACE INTO and an UPSERT against a
migrated database and expects each to be refused with "append-only".
#ASSUME: Retention: the privacy spec's 365-day purge of audit hashes is not
implemented, and the triggers would refuse it. Reconciling the two is an open
owner decision; no purge job may be added that drops or bypasses a trigger.
#VERIFY: grep for "DELETE FROM audit_log" in src/ finds no purge path until
that decision is recorded in an ADR.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import insert, select

from family_logistics.db.schema import audit_log

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

MAX_REF_LENGTH = 128


def state_hash(state: object | None) -> str | None:
    """Hash a JSON-serialisable state canonically.

    Args:
        state: State to hash, or None when there is no state (create/delete).

    Returns:
        Hex SHA-256 of the canonical JSON, or None for no state.
    """
    if state is None:
        return None
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One recorded audit row.

    Attributes:
        recorded_at: UTC time of the change.
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

        Raises:
            ValueError: If a reference field is empty or too long.
        """
        for name, value in (
            ("actor", actor),
            ("object_ref", object_ref),
            ("action", action),
        ):
            if not value or len(value) > MAX_REF_LENGTH:
                msg = f"audit {name} must be 1-{MAX_REF_LENGTH} characters"
                raise ValueError(msg)
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
        raise TypeError(msg)
    return AuditEntry(
        recorded_at=recorded_at,
        actor=str(row["actor"]),
        object_ref=str(row["object_ref"]),
        action=str(row["action"]),
        prior_hash=_optional_text(row["prior_hash"]),
        new_hash=_optional_text(row["new_hash"]),
        correlation_id=_optional_text(row["correlation_id"]),
    )
