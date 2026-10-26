"""Shared builders for the notification state repository tests (ADR 0008, D9).

Everything here is fictional: subject keys are hashes and fixed names, run ids
are labels, and no push text exists at this layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fl_service import make_settings
from sqlalchemy import insert, select

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema
from family_logistics.domain.models import Severity
from family_logistics.domain.notification_policy import (
    NotificationPolicy,
    Plan,
    PlannedPush,
    PushKind,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

T0 = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
"""07:00 local (PDT): outside quiet hours."""
NIGHT = datetime(2026, 10, 27, 6, 30, tzinfo=UTC)
"""23:30 local on 26 October: inside quiet hours."""

ISSUE = "issue:" + "a" * 16
ISSUE_B = "issue:" + "b" * 16
TAGS = {
    PushKind.NOTICE: "fl-warning",
    PushKind.URGENT: "fl-urgent",
    PushKind.STATUS: "fl-status",
    PushKind.REPORT: "fl-report",
    PushKind.OPS: "fl-ops",
}


def make_engine(tmp_path: Path) -> Engine:
    """Create a file-backed engine migrated to head (WAL, single writer)."""
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    return engine


def policy(mode: str = "shadow", **overrides: object) -> NotificationPolicy:
    """Build the policy from default settings plus ``overrides``."""
    return NotificationPolicy.from_settings(
        make_settings(notification_mode=mode, **overrides)
    )


def push(
    key: str = ISSUE,
    kind: PushKind = PushKind.NOTICE,
    *,
    severity: Severity | None = Severity.WARNING,
    episode_start: bool = False,
    retry_only: bool = False,
    run_wide: bool = False,
    rule_id: str | None = "DEST-004",
) -> PlannedPush:
    """Build one planned push; the defaults describe an issue notice."""
    is_issue = key.startswith("issue:")
    return PlannedPush(
        subject_key=key,
        kind=kind,
        intended_tag=TAGS[kind],
        episode_start=episode_start,
        retry_only=retry_only,
        run_wide=run_wide,
        rule_id=rule_id if is_issue else None,
        severity=severity if is_issue else None,
    )


def make_plan(
    *pushes: PlannedPush,
    open_keys: frozenset[str] | None = None,
    completed: bool = True,
    incomplete: bool = False,
) -> Plan:
    """Build a plan; the open issue keys default to the planned issue keys."""
    if open_keys is None:
        open_keys = frozenset(
            p.subject_key for p in pushes if p.subject_key[:6] == "issue:"
        )
    return Plan(pushes, open_keys, completed=completed, incomplete=incomplete)


def state_row(**overrides: object) -> dict[str, object]:
    """Return a full ``notification_state`` row, an open undelivered issue notice."""
    values: dict[str, object] = {
        "subject_key": ISSUE,
        "mode": "shadow",
        "kind": "notice",
        "tag": "fl-warning",
        "rule_id": "DEST-004",
        "severity": None,
        "is_open": True,
        "delivered": False,
        "bypass_used": False,
        "episode_run_id": "run-1",
        "send_count": 0,
        "failed_attempts": 0,
        "first_sent_at": None,
        "last_sent_at": None,
        "last_attempt_at": None,
        "claim_token": None,
        "claimed_at": None,
        "run_id": "run-1",
        "evaluated_at": T0,
        "created_at": T0,
        "updated_at": T0,
    }
    return values | overrides


def delivered_row(sent_at: datetime = T0, **overrides: object) -> dict[str, object]:
    """Return a row for an open episode delivered as a warning notice."""
    values: dict[str, object] = {
        "severity": "warning",
        "delivered": True,
        "send_count": 1,
        "first_sent_at": sent_at,
        "last_sent_at": sent_at,
        "last_attempt_at": sent_at,
    }
    return state_row(**(values | overrides))


def put_row(engine: Engine, **overrides: object) -> None:
    """Insert one ``notification_state`` row."""
    with engine.begin() as connection:
        connection.execute(
            insert(schema.notification_state).values(state_row(**overrides))
        )


def get_row(engine: Engine, key: str = ISSUE, mode: str = "shadow"):
    """Return the stored row, or None."""
    table = schema.notification_state
    with engine.connect() as connection:
        return connection.execute(
            select(table).where(table.c.subject_key == key, table.c.mode == mode)
        ).one_or_none()


def all_rows(engine: Engine):
    """Return every stored row, ordered by key and mode."""
    table = schema.notification_state
    with engine.connect() as connection:
        return connection.execute(
            select(table).order_by(table.c.subject_key, table.c.mode)
        ).all()


def get_watermark(engine: Engine):
    """Return the watermark row, or None."""
    with engine.connect() as connection:
        return connection.execute(select(schema.notification_watermark)).one_or_none()


def minutes(count: float) -> timedelta:
    """Return a timedelta of ``count`` minutes."""
    return timedelta(minutes=count)
