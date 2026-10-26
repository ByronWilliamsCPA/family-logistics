"""Notification state: claims, de-duplication rows and the dispatch watermark.

The repository stores what the notification service last delivered per
``(subject_key, mode)`` and applies the pure decision of
``domain.notification_policy.decide`` (ADR 0008, D4, D5, D9 and D11). It
supplies each row as a ``StateView`` and writes what the decision implies; the
policy owns every judgement, the service owns every send. Neither table holds
personal data: subject keys are hashes, run ids and fixed names.

#CRITICAL: Concurrency: the watermark check, every decision, the row upserts
and the closes of one dispatch happen in ONE ``BEGIN IMMEDIATE`` transaction
(``core.database.immediate_transaction``), so two dispatches of the same run
cannot both claim a subject and an older outcome cannot interleave with a newer
one. No lock is held while Apprise is called: a send happens between ``claim``
and ``complete``, protected by the claim token.
#VERIFY: tests/unit/test_notification_state_repo.py::TestConcurrency and
tests/unit/test_immediate_transaction.py.

#CRITICAL: Timing: an outcome older than the watermark, or equal to it from a
different run, is ``STALE`` as a whole and writes nothing, so an older run can
never send "clear" after a newer failed run nor push an issue a newer run
already resolved. A status or report older than one scan interval is stale on
its own.
#VERIFY: tests/unit/test_notification_state_repo.py::TestWatermark and
TestStatusFreshness.

#CRITICAL: Data integrity: a claim never changes the kind or severity of a
delivered row. Only ``complete`` does, once the push really went out, so a
failed escalation still looks like the lower delivered push and is owed again.
#VERIFY: tests/unit/test_notification_state_repo.py::TestEscalationAndRetry.

#ASSUME: Data integrity: a push held for quiet hours writes nothing, except one
case: an open row whose episode a new run reopens (a regression after a missed
or failed dispatch). The next scan sees that finding as ``ongoing``, so unless
the reopen is recorded the held push would read as already delivered and be
lost. A closed row (cooldown) and a missing row re-derive on their own.
#VERIFY: tests/unit/test_notification_state_repo.py::TestHeldPushes.

#EDGE: Concurrency: a crash between a delivered send and ``complete`` leaves
the claim to expire after ``CLAIM_TTL``; the next dispatch sends again
(at-least-once). A close that lands while a send is in flight leaves that
claim in place, so the late ``complete`` marks a closed row delivered; the
next episode resets it.
#VERIFY: tests/unit/test_notification_state_repo.py::TestClose and TestClaim.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, cast

from sqlalchemy import and_, func, insert, literal, or_, select, update

from family_logistics.core.database import (
    BUSY_TIMEOUT_MS,
    immediate_transaction,
    is_lock_contention,
)
from family_logistics.core.exceptions import DatabaseError, ValidationError
from family_logistics.db import schema
from family_logistics.db.column_types import UtcDateTime
from family_logistics.domain.models import Severity
from family_logistics.domain.notification_policy import (
    CLAIM_LOCK_RETRIES,
    Decision,
    PlannedPush,
    PushKind,
    StateView,
    decide,
)
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from datetime import datetime

    from sqlalchemy.engine import Connection, Engine, Row
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.sql.elements import ColumnElement

    from family_logistics.domain.notification_policy import (
        NotificationPolicy,
        Plan,
    )

logger = get_logger(__name__)

CLAIM_LOCK_BACKOFF_SECONDS: Final = 0.1
"""Base wait between claim attempts on a busy database; it grows per attempt."""

_RUN_LEVEL_KINDS: Final = frozenset({PushKind.STATUS, PushKind.REPORT})
_ISSUE_PREFIX: Final = "issue:"
_RUN_FAILED: Final = "run-failed"
_OPS_FAILED: Final = "ops:run_failed"
_OPS_INCOMPLETE: Final = "ops:run_incomplete"

_table = schema.notification_state
_watermark = schema.notification_watermark
_WATERMARK_ID: Final = 1


class NotificationStateBusyError(DatabaseError):
    """Another writer held the write lock past the lock timeout.

    Nothing was written. ``claim`` retries it up to ``CLAIM_LOCK_RETRIES``
    times before letting it propagate.
    """


@dataclass(frozen=True, slots=True)
class PushDecision:
    """One planned push and what the claim decided about it.

    Attributes:
        push: The push to render and send. For a retry it carries the kind and
            tag stored on the row, not the planned label.
        decision: The decision; only ``SEND`` pushes hold a claim.
    """

    push: PlannedPush
    decision: Decision


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """The outcome of one claim transaction.

    Attributes:
        decisions: One entry per planned push, in plan order.
        stale: The whole outcome was refused by the watermark and nothing was
            written.
    """

    decisions: tuple[PushDecision, ...]
    stale: bool = False

    @property
    def claimed(self) -> tuple[PlannedPush, ...]:
        """Return the pushes this dispatch now holds a claim on."""
        return tuple(d.push for d in self.decisions if d.decision is Decision.SEND)


@dataclass(frozen=True, slots=True)
class _ClaimContext:
    """The per-dispatch inputs every step of a claim needs."""

    mode: str
    token: str
    run_id: str
    evaluated_at: datetime
    now: datetime
    quiet: bool
    policy: NotificationPolicy


def _store_busy(exc: OperationalError) -> NotificationStateBusyError | None:
    """Translate lock contention into the busy error; ignore other faults.

    Args:
        exc: The driver error from ``BEGIN IMMEDIATE``.

    Returns:
        The busy error for SQLITE_BUSY or SQLITE_LOCKED, else None so that any
        other fault (read-only database, disk I/O error) is re-raised
        unchanged and never retried.
    """
    if not is_lock_contention(exc.orig):
        return None
    msg = "the database is locked by another writer; notification state not written"
    return NotificationStateBusyError(
        msg,
        operation="notification_state",
        table="notification_state",
        error_code="NOTIFICATION_STATE_BUSY",
    )


def _require_aware(name: str, value: datetime) -> None:
    """Refuse a naive timestamp without echoing it.

    Args:
        name: Field name for the message.
        value: The timestamp.

    Raises:
        ValidationError: If ``value`` is naive.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{name} must be timezone-aware"
        raise ValidationError(msg, field=name)


@dataclass(frozen=True, slots=True)
class _Stored:
    """The typed columns of one stored row that a claim reads."""

    subject_key: str
    kind: str
    tag: str
    severity: str | None
    is_open: bool
    delivered: bool
    bypass_used: bool
    episode_run_id: str
    last_sent_at: datetime | None
    claimed_at: datetime | None

    def view(self) -> StateView:
        """Return the policy's view of this row."""
        return StateView(
            is_open=self.is_open,
            delivered=self.delivered,
            kind=PushKind(self.kind),
            severity=None if self.severity is None else Severity(self.severity),
            bypass_used=self.bypass_used,
            episode_run_id=self.episode_run_id,
            last_sent_at=self.last_sent_at,
            claimed_at=self.claimed_at,
        )


def _stored(row: Row[tuple[object, ...]]) -> _Stored:
    """Type the columns of a ``notification_state`` row (UtcDateTime is aware)."""
    return _Stored(
        subject_key=cast("str", row.subject_key),
        kind=cast("str", row.kind),
        tag=cast("str", row.tag),
        severity=cast("str | None", row.severity),
        is_open=cast("bool", row.is_open),
        delivered=cast("bool", row.delivered),
        bypass_used=cast("bool", row.bypass_used),
        episode_run_id=cast("str", row.episode_run_id),
        last_sent_at=cast("datetime | None", row.last_sent_at),
        claimed_at=cast("datetime | None", row.claimed_at),
    )


def _starts_new_episode(push: PlannedPush, view: StateView | None, run_id: str) -> bool:
    """Say whether sending this push starts a new episode of its subject.

    This mirrors the second value of ``notification_policy._owed``: a retry
    never does; a missing or closed row does; a run-level push always does; an
    open row does when a ``new`` or ``regressed`` finding comes from another
    run (F4).

    Args:
        push: The effective planned push.
        view: The subject's stored row, if any.
        run_id: The run being dispatched.

    Returns:
        True when a send (or a held push) resets the episode.
    """
    # #ASSUME: Data integrity: this stays equal to the policy's rule; the
    # claim and the policy disagreeing would reset or keep the wrong fields.
    # #VERIFY: tests/unit/test_notification_state_repo.py::TestNewEpisodes.
    if push.retry_only:
        return False
    if view is None or push.kind in _RUN_LEVEL_KINDS or not view.is_open:
        return True
    return push.episode_start and view.episode_run_id != run_id


def _episode_reset(push: PlannedPush, run_id: str) -> dict[str, object]:
    """Return the column values that start a fresh episode (D9)."""
    return {
        "kind": push.kind.value,
        "tag": push.intended_tag,
        "rule_id": push.rule_id,
        "severity": None,
        "is_open": push.kind not in _RUN_LEVEL_KINDS,
        "delivered": False,
        "bypass_used": False,
        "send_count": 0,
        "episode_run_id": run_id,
    }


def _is_late_status(push: PlannedPush, ctx: _ClaimContext) -> bool:
    """Say whether a status or report is older than one scan interval (F2)."""
    return (
        push.kind in _RUN_LEVEL_KINDS
        and ctx.now - ctx.evaluated_at > ctx.policy.status_max_age
    )


def _key_mode(key: str, mode: str) -> ColumnElement[bool]:
    """Return the WHERE clause selecting one row by its primary key."""
    return and_(_table.c.subject_key == key, _table.c.mode == mode)


class NotificationStateRepository:
    """Claims and completes notification pushes against ``notification_state``.

    Args:
        engine: Database engine at the schema head.
        lock_timeout_ms: How long a write waits for the write lock before the
            attempt counts as busy.
        sleep: The wait between claim retries (replaceable in tests).
    """

    def __init__(
        self,
        engine: Engine,
        *,
        lock_timeout_ms: int = BUSY_TIMEOUT_MS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._lock_timeout_ms = lock_timeout_ms
        self._sleep = sleep

    def _transaction(self) -> AbstractContextManager[Connection]:
        """Open one write transaction that reports a busy database."""
        return immediate_transaction(
            self._engine,
            lock_timeout_ms=self._lock_timeout_ms,
            on_begin_error=_store_busy,
        )

    def claim(  # noqa: PLR0913 - the dispatch inputs are keyword-only (ADR 0008)
        self,
        plan: Plan,
        *,
        mode: str,
        token: str,
        run_id: str,
        evaluated_at: datetime,
        now: datetime,
        quiet: bool,
        policy: NotificationPolicy,
    ) -> ClaimResult:
        """Check the watermark, decide every push and claim the ones to send.

        One immediate transaction reads the watermark and the rows of every
        planned subject, applies ``decide`` and upserts a claim for each
        ``SEND``. A completed plan also closes the rows of every episode that
        has ended. A busy database is retried up to ``CLAIM_LOCK_RETRIES``
        times with a growing wait.

        Args:
            plan: What the outcome asks for.
            mode: ``shadow`` or ``production``; each has its own rows.
            token: This dispatch's claim token (unique per dispatch).
            run_id: The run being dispatched; a different run reopens an
                episode.
            evaluated_at: The outcome's ``evaluated_at`` (``now`` for a run
                that never produced an outcome), compared with the watermark.
            now: The dispatch clock (aware).
            quiet: Whether ``now`` is inside quiet hours.
            policy: The resolved policy.

        Returns:
            One decision per planned push. When the watermark refuses the
            outcome every decision is ``STALE``, ``stale`` is True and nothing
            was written.

        Raises:
            NotificationStateBusyError: The database stayed busy through every
                retry; nothing was written.

        Note:
            A naive ``now`` or ``evaluated_at`` raises ``ValidationError``.
        """
        _require_aware("now", now)
        _require_aware("evaluated_at", evaluated_at)
        ctx = _ClaimContext(mode, token, run_id, evaluated_at, now, quiet, policy)
        attempt = 0
        while True:
            try:
                return self._claim_once(plan, ctx)
            except NotificationStateBusyError:
                attempt += 1
                if attempt > CLAIM_LOCK_RETRIES:
                    raise
                logger.warning("notification.lock_retry", attempt=attempt)
                self._sleep(CLAIM_LOCK_BACKOFF_SECONDS * attempt)

    def _claim_once(self, plan: Plan, ctx: _ClaimContext) -> ClaimResult:
        """Run the claim transaction once."""
        with self._transaction() as connection:
            if not self._admit(connection, ctx):
                return ClaimResult(
                    tuple(PushDecision(p, Decision.STALE) for p in plan.pushes),
                    stale=True,
                )
            rows = self._load_rows(connection, plan, ctx.mode)
            decided = tuple(
                self._apply(connection, push, rows.get(push.subject_key), ctx)
                for push in plan.pushes
            )
            if plan.completed:
                self._close_ended(connection, plan, ctx)
        return ClaimResult(decided)

    def _admit(self, connection: Connection, ctx: _ClaimContext) -> bool:
        """Apply the watermark rule; advance the watermark when admitted.

        Older is refused. Equal from the same run is a re-dispatch and is
        admitted; equal from another run is refused. Newer is admitted and
        becomes the watermark in the same transaction (D9, F2).

        Args:
            connection: The claim transaction's connection.
            ctx: The dispatch inputs.

        Returns:
            False when the outcome is stale.
        """
        mark = connection.execute(
            select(_watermark.c.evaluated_at, _watermark.c.run_id).where(
                _watermark.c.id == _WATERMARK_ID
            )
        ).one_or_none()
        if mark is None:
            connection.execute(
                insert(_watermark).values(
                    id=_WATERMARK_ID,
                    evaluated_at=ctx.evaluated_at,
                    run_id=ctx.run_id,
                    updated_at=ctx.now,
                )
            )
            return True
        mark_at = cast("datetime", mark.evaluated_at)
        if ctx.evaluated_at < mark_at:
            return False
        if ctx.evaluated_at == mark_at:
            return ctx.run_id == cast("str", mark.run_id)
        connection.execute(
            update(_watermark)
            .where(_watermark.c.id == _WATERMARK_ID)
            .values(
                evaluated_at=ctx.evaluated_at, run_id=ctx.run_id, updated_at=ctx.now
            )
        )
        return True

    def _load_rows(
        self, connection: Connection, plan: Plan, mode: str
    ) -> dict[str, _Stored]:
        """Read the rows of every planned subject in one mode."""
        keys = [push.subject_key for push in plan.pushes]
        if not keys:
            return {}
        rows = connection.execute(
            select(_table).where(_table.c.mode == mode, _table.c.subject_key.in_(keys))
        )
        stored = (_stored(row) for row in rows)
        return {item.subject_key: item for item in stored}

    def _apply(
        self,
        connection: Connection,
        push: PlannedPush,
        row: _Stored | None,
        ctx: _ClaimContext,
    ) -> PushDecision:
        """Decide one push and write what the decision implies."""
        effective = push
        if push.retry_only and row is not None:
            # The row's own kind decides a retry (an urgent push that failed
            # earlier stays urgent even if this run labels it a notice).
            effective = replace(push, kind=PushKind(row.kind), intended_tag=row.tag)
        if _is_late_status(effective, ctx):
            return PushDecision(effective, Decision.STALE)
        view = None if row is None else row.view()
        decision = decide(
            effective,
            view,
            run_id=ctx.run_id,
            now=ctx.now,
            quiet=ctx.quiet,
            policy=ctx.policy,
        )
        new_episode = _starts_new_episode(effective, view, ctx.run_id)
        if decision is Decision.SEND:
            self._write_claim(connection, effective, row, ctx)
        elif (
            decision is Decision.QUIET_HOURS
            and new_episode
            and view is not None
            and view.is_open
        ):
            self._record_reopen(connection, effective, ctx)
        return PushDecision(effective, decision)

    def _write_claim(
        self,
        connection: Connection,
        push: PlannedPush,
        row: _Stored | None,
        ctx: _ClaimContext,
    ) -> None:
        """Insert or update the row of a push this dispatch will send."""
        claim = {
            "claim_token": ctx.token,
            "claimed_at": ctx.now,
            "run_id": ctx.run_id,
            "evaluated_at": ctx.evaluated_at,
            "updated_at": ctx.now,
        }
        if row is None:
            connection.execute(
                insert(_table).values(
                    subject_key=push.subject_key,
                    mode=ctx.mode,
                    failed_attempts=0,
                    created_at=ctx.now,
                    **_episode_reset(push, ctx.run_id),
                    **claim,
                )
            )
            return
        values = dict(claim)
        if _starts_new_episode(push, row.view(), ctx.run_id):
            values |= _episode_reset(push, ctx.run_id)
        elif not row.delivered and not push.retry_only:
            # Undelivered: the kind is only "planned". A delivered row keeps
            # its delivered kind until ``complete`` (see the module notes).
            values |= {"kind": push.kind.value, "tag": push.intended_tag}
        connection.execute(
            update(_table).where(_key_mode(push.subject_key, ctx.mode)).values(**values)
        )

    def _record_reopen(
        self, connection: Connection, push: PlannedPush, ctx: _ClaimContext
    ) -> None:
        """Record a held new episode on an open row, without claiming it."""
        connection.execute(
            update(_table)
            .where(_key_mode(push.subject_key, ctx.mode))
            .values(
                **_episode_reset(push, ctx.run_id),
                run_id=ctx.run_id,
                evaluated_at=ctx.evaluated_at,
                updated_at=ctx.now,
            )
        )

    def _close_ended(
        self, connection: Connection, plan: Plan, ctx: _ClaimContext
    ) -> None:
        """Close the open rows whose episode this completed run ended (D9).

        Closes every open ``issue:*`` row whose issue is not open in the
        result, ``run-failed`` and ``ops:run_failed``, and
        ``ops:run_incomplete`` unless this run is incomplete. ``last_sent_at``
        is kept for the notice cooldown.
        """
        is_issue = func.substr(_table.c.subject_key, 1, len(_ISSUE_PREFIX)) == (
            _ISSUE_PREFIX
        )
        ended = [
            and_(is_issue, _table.c.subject_key.not_in(sorted(plan.open_issue_keys))),
            _table.c.subject_key.in_([_RUN_FAILED, _OPS_FAILED]),
        ]
        if not plan.incomplete:
            ended.append(_table.c.subject_key == _OPS_INCOMPLETE)
        connection.execute(
            update(_table)
            .where(_table.c.mode == ctx.mode, _table.c.is_open.is_(True), or_(*ended))
            .values(
                is_open=False,
                delivered=False,
                severity=None,
                bypass_used=False,
                run_id=ctx.run_id,
                evaluated_at=ctx.evaluated_at,
                updated_at=ctx.now,
            )
        )

    def complete(  # noqa: PLR0913 - the outcome inputs are keyword-only (ADR 0008)
        self,
        pushes: Sequence[PlannedPush],
        *,
        mode: str,
        token: str,
        delivered: bool,
        now: datetime,
        quiet: bool,
    ) -> None:
        """Record the result of sending a claimed push or chunk.

        One immediate transaction. A delivery marks the row delivered with the
        pushed kind, tag and severity, counts the send and clears the failure
        count; a failure counts the attempt. Both clear the claim, and only on
        rows whose ``claim_token`` is still this dispatch's token: a claim that
        expired and was taken over is left to its new owner.

        Args:
            pushes: The claimed pushes (a chunk's items), as ``claim`` returned
                them.
            mode: ``shadow`` or ``production``.
            token: This dispatch's claim token.
            delivered: Whether Apprise delivered the push.
            now: The completion clock (aware).
            quiet: Whether the push was sent inside quiet hours (it then spent
                the episode's bypass).

        Note:
            A write lock that stays held raises ``NotificationStateBusyError``
            and a naive ``now`` raises ``ValidationError``; nothing is written
            in either case.
        """
        _require_aware("now", now)
        with self._transaction() as connection:
            for push in pushes:
                values = (
                    self._delivered_values(push, now, quiet=quiet)
                    if delivered
                    else {
                        "failed_attempts": _table.c.failed_attempts + 1,
                        "last_attempt_at": now,
                    }
                )
                connection.execute(
                    update(_table)
                    .where(
                        _key_mode(push.subject_key, mode),
                        _table.c.claim_token == token,
                    )
                    .values(claim_token=None, claimed_at=None, updated_at=now, **values)
                )

    @staticmethod
    def _delivered_values(
        push: PlannedPush, now: datetime, *, quiet: bool
    ) -> dict[str, object]:
        """Return the column values of a delivered push."""
        values: dict[str, object] = {
            "delivered": True,
            "kind": push.kind.value,
            "tag": push.intended_tag,
            "severity": None if push.severity is None else push.severity.value,
            "last_sent_at": now,
            "last_attempt_at": now,
            "first_sent_at": func.coalesce(
                _table.c.first_sent_at, literal(now, UtcDateTime())
            ),
            "send_count": _table.c.send_count + 1,
            "failed_attempts": 0,
        }
        if quiet:
            values["bypass_used"] = True
        return values

    def release(self, pushes: Sequence[PlannedPush], *, mode: str, token: str) -> None:
        """Release claims that were never sent (the send deadline passed).

        Clears the claim without counting a failure, so the next dispatch can
        claim the push at once. Only rows whose ``claim_token`` is still this
        dispatch's token are touched.

        Args:
            pushes: The claimed pushes that were not sent.
            mode: ``shadow`` or ``production``.
            token: This dispatch's claim token.

        Note:
            A write lock that stays held raises ``NotificationStateBusyError``
            and nothing is released.
        """
        with self._transaction() as connection:
            for push in pushes:
                connection.execute(
                    update(_table)
                    .where(
                        _key_mode(push.subject_key, mode),
                        _table.c.claim_token == token,
                    )
                    .values(claim_token=None, claimed_at=None)
                )


__all__ = [
    "CLAIM_LOCK_BACKOFF_SECONDS",
    "ClaimResult",
    "NotificationStateBusyError",
    "NotificationStateRepository",
    "PushDecision",
]
