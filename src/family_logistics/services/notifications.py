"""Notification service: one stored outcome becomes templated pushes (ADR 0008).

``dispatch`` plans the pushes with the pure policy, claims what is owed through
the state repository (no lock held while sending), renders and sends each push
with ``AppriseClient`` and records the result. ``dispatch_unavailable`` does the
same for a run that never produced an outcome. Nothing calls either yet: the
scheduler wires them (``services/scheduler.py``, the M3 gate).

A delivery failure never raises: the push stays owed and a later dispatch
retries it (an urgent push once more in the same dispatch). Only
``NotificationTagError`` and ``DatabaseError`` escape; the run is already
stored, so neither changes it.

#CRITICAL: Security: children's data. Nothing here reads a finding's message,
suggestion, entity keys, notes, addresses, UIDs, calendar IDs or names; pushes
come from ``domain.notification_text`` and logs and audit rows carry only
counts, IDs, kinds, tags, decisions, reason codes and error class names.
#VERIFY: tests/unit/test_notification_service_canary.py.
#CRITICAL: Timing: a failed or incomplete run is never reported as clear, and an
older outcome never sends after a newer one (the claim's watermark).
#VERIFY: tests/unit/test_notification_service.py (T-svc-1, 2, 21, 24).
#CRITICAL: External resources: Apprise may be down. A False from ``notify``
records nothing as sent; the push is retried by the next dispatch.
#VERIFY: tests/unit/test_notification_service.py (T-svc-7, 7b, 17).
#CRITICAL: Concurrency: a push is sent only after its claim commits. A crash
between a delivered send and its completion leaves the claim to expire after
``CLAIM_TTL`` and the next dispatch sends again (at-least-once).
#VERIFY: tests/unit/test_notification_service.py (T-svc-6, 16).
#ASSUME: Timing: the dispatch ``now`` decides quiet hours for every push of the
dispatch and the clock is steady enough for ``SEND_DEADLINE``.
#VERIFY: tests/unit/test_notification_service.py (send deadline).
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, Self

from sqlalchemy.exc import SQLAlchemyError

from family_logistics.adapters.apprise.client import AppriseClient
from family_logistics.core.exceptions import DatabaseError, ProjectBaseError
from family_logistics.db.audit import AuditRepository
from family_logistics.db.notifications import (
    NotificationStateBusyError,
    NotificationStateRepository,
    load_name_rows,
)
from family_logistics.domain.notification_policy import (
    RUN_LEVEL_KINDS,
    SEND_DEADLINE,
    URGENT_RETRY_DELAY,
    Decision,
    NotificationPolicy,
    PushKind,
    delivery_tag,
    in_quiet_hours,
    plan,
    plan_unavailable,
)
from family_logistics.domain.notification_text import (
    NameDirectory,
    chunk_pushes,
    render,
    status_counts,
)
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

    import httpx
    from sqlalchemy.engine import Engine

    from family_logistics.core.config import Settings
    from family_logistics.db.notifications import PushDecision
    from family_logistics.domain.notification_policy import Plan, PlannedPush
    from family_logistics.services.assessments import RunOutcome

logger = get_logger(__name__)

_SUPPRESSED: Final = {
    Decision.QUIET_HOURS: "quiet_hours",
    Decision.BYPASS_USED: "bypass_used",
    Decision.COOLDOWN: "cooldown",
}
_SKIPPED: Final = {
    Decision.DUPLICATE: "duplicates",
    Decision.IN_FLIGHT: "in_flight",
    Decision.STALE: "dropped",
}
_COUNTED: Final = (
    "sent",
    "failed",
    "urgent_failed",
    "held",
    "dropped",
    "duplicates",
    "in_flight",
    "released",
)
_RUN_ID_LENGTH: Final = 36
"""``notification_state.run_id`` is a 36 character column."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """What one dispatch did, for the scheduler and the logs.

    Counts are Apprise calls for ``sent`` and ``failed`` (a chunk of notices is
    one call, an unrenderable chunk counts as failed) and pushes elsewhere.
    ``held`` is owed but not sent now (quiet hours, a used bypass, the
    cooldown); ``dropped`` is a status or report dropped in quiet hours;
    ``released`` is claimed but unsent (send deadline or tag error), still owed;
    ``stale`` means the watermark refused the whole outcome; ``unconfigured``
    means ``FL_APPRISE_URL`` is unset (nothing written, everything stays owed);
    ``urgent_failed`` lets the scheduler run an extra dispatch in about five
    minutes; ``status_stale`` means the status or report was older than one scan
    interval and was dropped.
    """

    run_id: str | None
    mode: Literal["shadow", "production"]
    sent: int = 0
    failed: int = 0
    urgent_failed: int = 0
    held: int = 0
    dropped: int = 0
    duplicates: int = 0
    in_flight: int = 0
    stale: bool = False
    unresolved: int = 0
    unconfigured: bool = False
    released: int = 0
    status_stale: bool = False


@dataclass(slots=True)
class _Dispatch:
    """The inputs fixed when a dispatch starts, and its running counters."""

    run_id: str
    evaluated_at: datetime
    plan: Plan
    now: datetime
    policy: NotificationPolicy
    counts: Mapping[str, int] | None
    trigger: str = ""
    run_status: str = ""
    quiet: bool = False
    token: str = field(default_factory=lambda: str(uuid.uuid4()))
    tally: Counter[str] = field(default_factory=Counter)
    state_error: DatabaseError | None = None


@contextmanager
def _writable(operation: str) -> Generator[None]:
    """Translate a state-write failure into ``DatabaseError``.

    Args:
        operation: ``claim``, ``complete`` or ``release``.

    Yields:
        None: the guarded state write runs in the caller's block.

    Raises:
        DatabaseError: The repository stayed busy or the database raised; the
            message is fixed and the driver error is only chained.
    """
    try:
        yield
    except (NotificationStateBusyError, SQLAlchemyError) as exc:
        msg = f"notification state could not be written ({operation})"
        raise DatabaseError(
            msg,
            operation=operation,
            table="notification_state",
            error_code="NOTIFICATION_STATE_UNWRITABLE",
        ) from exc


class NotificationService:
    """Sends the pushes one assessment outcome owes (ADR 0008).

    Args:
        engine: Database engine at the schema head.
        settings: Application settings (mode, tags, quiet hours, audit key).
        client: The Apprise client, or None when ``FL_APPRISE_URL`` is unset
            (a dispatch then plans, logs and writes nothing).
        audit: The audit repository; built from the settings when ``None`` and
            ``FL_AUDIT_KEY`` is set, else no audit rows are written.
        clock: The current aware time (replaceable in tests).
        sleep: The wait before an urgent retry and between lock retries.
    """

    def __init__(  # noqa: PLR0913 - the collaborators are keyword-only (ADR 0008)
        self,
        engine: Engine,
        settings: Settings,
        client: AppriseClient | None,
        *,
        audit: AuditRepository | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._client = client
        if audit is None and settings.audit_key is not None:
            audit = AuditRepository.from_settings(engine, settings)
        self._audit = audit
        self._clock = clock
        self._sleep = sleep
        self._repo = NotificationStateRepository(engine, sleep=sleep)

    @classmethod
    def from_settings(
        cls,
        engine: Engine,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> Self:
        """Build the service and its Apprise client from settings.

        Args:
            engine: Database engine at the schema head.
            settings: Application settings.
            transport: Optional HTTP transport for the client (tests).

        Returns:
            The service; its client is None when ``FL_APPRISE_URL`` is unset
            (an unusable URL, key or tag set raises ``ConfigurationError``).
        """
        return cls(
            engine, settings, AppriseClient.from_settings(settings, transport=transport)
        )

    def close(self) -> None:
        """Close the Apprise client, if there is one."""
        if self._client is not None:
            self._client.close()

    def dispatch(self, outcome: RunOutcome) -> DispatchReport:
        """Send the pushes a stored run owes.

        Plans the pushes, claims the ones owed, renders and sends them outside
        any transaction and records each result. Held pushes (quiet hours, a
        used bypass, the cooldown) stay owed for a later dispatch.

        Args:
            outcome: The stored run (trigger, ride index and result).

        Returns:
            What was sent, held, dropped or skipped.

        Note:
            Raises ``NotificationTagError`` for a tag the client does not own
            (unsent claims are released first) and ``DatabaseError`` when the
            state is not writable (nothing sent if the claim failed; a failed
            completion leaves its claim to expire and raises after the other
            sends were attempted). A delivery failure never raises.
        """
        now = self._clock()
        policy = NotificationPolicy.from_settings(self._settings)
        result = outcome.result
        planned = plan(
            run_id=outcome.run_id,
            trigger=outcome.trigger,
            result=result,
            rides=outcome.rides,
            now=now,
            policy=policy,
        )
        counts = status_counts(result)
        return self._run(
            _Dispatch(
                outcome.run_id,
                result.evaluated_at,
                planned,
                now,
                policy,
                counts,
                outcome.trigger,
                result.run_status.value,
            )
        )

    def dispatch_unavailable(self, trigger: str) -> DispatchReport:
        """Tell the family a run could not be verified at all (D4).

        Plans ``run-failed`` with an unknown ride set (urgent, once per local
        day) and ``ops:run_failed``.

        Args:
            trigger: What the missing run was for (logged only).

        Returns:
            What was sent, held or skipped; raises like ``dispatch``.
        """
        now = self._clock()
        policy = NotificationPolicy.from_settings(self._settings)
        run_id = f"unavailable-{uuid.uuid4()}"[:_RUN_ID_LENGTH]
        planned = plan_unavailable(now=now, policy=policy)
        return self._run(
            _Dispatch(run_id, now, planned, now, policy, None, trigger, "unavailable")
        )

    def _run(self, d: _Dispatch) -> DispatchReport:
        """Claim, send and record what the plan owes, then report."""
        logger.info(
            "notification.dispatch_started",
            run_id=d.run_id,
            trigger=d.trigger,
            run_status=d.run_status,
            mode=d.policy.mode,
            open_findings=len(d.plan.open_issue_keys),
        )
        d.quiet = in_quiet_hours(
            d.now, d.policy.quiet_start, d.policy.quiet_end, d.policy.zone
        )
        for rule_id in d.plan.unresolved_rule_ids:
            logger.error("notification.subject_unresolved", rule_id=rule_id)
        if d.plan.status_stale:
            logger.info(
                "notification.suppressed",
                subject_key=f"run:{d.run_id}",
                reason="status_stale",
            )
        if self._client is None:
            d.tally["unconfigured"] += 1
            logger.warning(
                "notification.not_configured",
                run_id=d.run_id,
                owed=len(d.plan.pushes),
            )
        else:
            self._claim_and_send(d, self._client)
        return self._finish(d)

    def _claim_and_send(self, d: _Dispatch, client: AppriseClient) -> None:
        """Claim the owed pushes, then send the ones this dispatch holds."""
        with _writable("claim"):
            claim = self._repo.claim(
                d.plan,
                mode=d.policy.mode,
                token=d.token,
                run_id=d.run_id,
                evaluated_at=d.evaluated_at,
                now=d.now,
                quiet=d.quiet,
                policy=d.policy,
            )
        if claim.stale:
            d.tally["stale"] += 1
            logger.warning(
                "notification.stale_outcome",
                run_id=d.run_id,
                evaluated_at=d.evaluated_at.isoformat(),
                watermark_run_id=self._watermark_run_id(),
            )
        claimed = tuple(
            i.push for i in claim.decisions if _account(i, d.tally, claim.stale)
        )
        if claimed:
            self._send_all(d, client, claimed)

    def _watermark_run_id(self) -> str | None:
        """Return the watermark's run ID for a log line; None if unreadable."""
        try:
            return self._repo.watermark_run_id()
        except SQLAlchemyError:
            return None

    def _names(self) -> NameDirectory:
        """Load the masked-name directory once; an unreadable one is empty (D2)."""
        try:
            people, passengers = load_name_rows(self._engine)
        except SQLAlchemyError as exc:
            logger.warning("notification.names_unavailable", error=type(exc).__name__)
            return NameDirectory.empty()
        return NameDirectory.from_rows(people, passengers)

    def _send_all(
        self, d: _Dispatch, client: AppriseClient, claimed: Sequence[PlannedPush]
    ) -> None:
        """Send each claimed chunk; release whatever was not sent."""
        names = self._names()
        chunks = chunk_pushes(claimed)
        done = 0
        try:
            for chunk in chunks:
                if self._clock() - d.now >= SEND_DEADLINE:
                    break
                self._send_chunk(d, client, chunk, names)
                done += 1
        finally:
            unsent = tuple(pushed for chunk in chunks[done:] for pushed in chunk)
            if unsent:
                d.tally["released"] += len(unsent)
                self._release(d, unsent)

    def _send_chunk(
        self,
        d: _Dispatch,
        client: AppriseClient,
        chunk: tuple[PlannedPush, ...],
        names: NameDirectory,
    ) -> None:
        """Render, send and record one Apprise call (outside any transaction)."""
        first = chunk[0]
        rendered = None
        refusal = ""
        try:
            rendered = render(chunk, names=names, policy=d.policy, counts=d.counts)
        except ValueError as exc:
            # #EDGE: Data integrity: the renderer refuses an empty or mixed
            # chunk. It is never sent (so never worded as clear) and its claim
            # is released.
            # #VERIFY: tests/unit/test_notification_service.py (render failure).
            refusal = type(exc).__name__
        if rendered is None:
            # Only the class name is logged: a traceback or message may carry text.
            logger.error(
                "notification.render_failed",
                subject_key=first.subject_key,
                kind=first.kind.value,
                items=len(chunk),
                error=refusal,
            )
            self._count_failure(d, chunk)
            self._release(d, chunk)
            return
        tag = delivery_tag(first.kind, d.policy)
        delivered = client.notify(tag, rendered.title, rendered.body)
        attempts = 1
        if not delivered and first.kind is PushKind.URGENT and self._can_retry(d):
            self._sleep(URGENT_RETRY_DELAY.total_seconds())
            delivered = client.notify(tag, rendered.title, rendered.body)
            attempts = 2
        written = self._complete(d, chunk, delivered=delivered)
        if not delivered:
            self._count_failure(d, chunk)
            logger.warning(
                "notification.delivery_failed",
                subject_key=first.subject_key,
                kind=first.kind.value,
                delivery_tag=tag,
                attempts=attempts,
                retried=attempts > 1,
            )
            return
        d.tally["sent"] += 1
        logger.info(
            "notification.sent",
            subject_key=first.subject_key,
            kind=first.kind.value,
            intended_tag=first.intended_tag,
            delivery_tag=tag,
            mode=d.policy.mode,
            items=len(chunk),
        )
        if written:
            self._audit_sent(d, chunk, tag)

    def _can_retry(self, d: _Dispatch) -> bool:
        """Say whether an urgent retry would still start before the deadline."""
        return self._clock() - d.now + URGENT_RETRY_DELAY < SEND_DEADLINE

    @staticmethod
    def _count_failure(d: _Dispatch, chunk: Sequence[PlannedPush]) -> None:
        d.tally["failed"] += 1
        if chunk[0].kind is PushKind.URGENT:
            d.tally["urgent_failed"] += len(chunk)

    def _complete(
        self, d: _Dispatch, chunk: Sequence[PlannedPush], *, delivered: bool
    ) -> bool:
        """Record a send's result; return False when it could not be written."""
        try:
            with _writable("complete"):
                self._repo.complete(
                    chunk,
                    mode=d.policy.mode,
                    token=d.token,
                    delivered=delivered,
                    now=self._clock(),
                    quiet=d.quiet,
                )
        except DatabaseError as exc:
            # #EDGE: Concurrency: the result could not be written; the claim
            # expires and a later dispatch sends again (at-least-once).
            # #VERIFY: tests/unit/test_notification_service.py (T-svc-16).
            self._note_state_error(d, exc)
            return False
        return True

    def _release(self, d: _Dispatch, pushes: Sequence[PlannedPush]) -> None:
        """Release claims that were not sent; a failure leaves them to expire."""
        try:
            with _writable("release"):
                self._repo.release(pushes, mode=d.policy.mode, token=d.token)
        except DatabaseError as exc:
            self._note_state_error(d, exc)

    @staticmethod
    def _note_state_error(d: _Dispatch, exc: DatabaseError) -> None:
        logger.error(
            "notification.state_write_failed", operation=exc.details.get("operation")
        )
        d.state_error = d.state_error or exc

    def _audit_sent(self, d: _Dispatch, chunk: Sequence[PlannedPush], tag: str) -> None:
        """Write one audit row per delivered push, after its row is committed."""
        if self._audit is None:
            return
        for pushed in chunk:
            severity = None if pushed.severity is None else pushed.severity.value
            failure = ""
            try:
                self._audit.record(
                    actor="notifier",
                    object_ref=pushed.subject_key,
                    action="notification_sent",
                    new_state={
                        "kind": pushed.kind.value,
                        "tag": pushed.intended_tag,
                        "delivery_tag": tag,
                        "mode": d.policy.mode,
                        "rule_id": pushed.rule_id,
                        "severity": severity,
                        "run_id": d.run_id,
                        "chunk_size": len(chunk),
                    },
                    correlation_id=d.run_id,
                )
            except (ProjectBaseError, SQLAlchemyError) as exc:
                # An audit failure never undoes or repeats the send (D10).
                failure = type(exc).__name__
            if failure:
                logger.error(
                    "notification.audit_failed",
                    subject_key=pushed.subject_key,
                    error=failure,
                )

    def _finish(self, d: _Dispatch) -> DispatchReport:
        """Log the dispatch and build its report.

        Args:
            d: The finished dispatch.

        Returns:
            The report.

        Raises:
            DatabaseError: A completion or release could not be written after
                the other sends were attempted (the claim expires and a later
                dispatch sends again).
        """
        n = d.tally
        if self._audit is None and n["sent"]:
            logger.debug("notification.audit_skipped", run_id=d.run_id)
        report = DispatchReport(
            run_id=d.run_id,
            mode=d.policy.mode,
            sent=n["sent"],
            failed=n["failed"],
            urgent_failed=n["urgent_failed"],
            held=n["held"],
            dropped=n["dropped"],
            duplicates=n["duplicates"],
            in_flight=n["in_flight"],
            stale=bool(n["stale"]),
            unresolved=d.plan.unresolved,
            unconfigured=bool(n["unconfigured"]),
            released=n["released"],
            status_stale=d.plan.status_stale,
        )
        elapsed = self._clock() - d.now
        logger.info(
            "notification.dispatch_finished",
            run_id=d.run_id,
            duration_ms=int(elapsed.total_seconds() * 1000),
            **{name: n[name] for name in _COUNTED},
            stale=report.stale,
            unconfigured=report.unconfigured,
            status_stale=report.status_stale,
            unresolved=report.unresolved,
        )
        if d.state_error is not None:
            msg = "notification state could not be written (completion)"
            raise DatabaseError(
                msg,
                operation="complete",
                table="notification_state",
                error_code="NOTIFICATION_STATE_UNWRITABLE",
            ) from d.state_error
        return report


def _account(item: PushDecision, tally: Counter[str], stale: bool) -> bool:
    """Count and log one push's decision.

    Args:
        item: The push and what the claim decided.
        tally: The dispatch counters.
        stale: The whole outcome was refused (its pushes are not counted).

    Returns:
        True when the push is claimed and must be sent.
    """
    pushed, decision = item.push, item.decision
    severity = None if pushed.severity is None else pushed.severity.value
    logger.debug(
        "notification.decided",
        subject_key=pushed.subject_key,
        kind=pushed.kind.value,
        rule_id=pushed.rule_id,
        severity=severity,
        intended_tag=pushed.intended_tag,
        decision=decision.value,
    )
    if decision is Decision.SEND:
        return True
    reason = _SUPPRESSED.get(decision)
    if reason is None:
        # DUPLICATE, IN_FLIGHT and STALE (a late status the plan did not drop).
        if not stale:
            tally[_SKIPPED[decision]] += 1
        logger.debug(
            "notification.claim_skipped",
            subject_key=pushed.subject_key,
            kind=pushed.kind.value,
            decision=decision.value,
        )
        return False
    if decision is Decision.QUIET_HOURS and pushed.kind in RUN_LEVEL_KINDS:
        tally["dropped"] += 1
        reason = "quiet_hours_dropped"
    else:
        tally["held"] += 1
    logger.info(
        "notification.suppressed",
        subject_key=pushed.subject_key,
        kind=pushed.kind.value,
        reason=reason,
    )
    return False


__all__ = ["DispatchReport", "NotificationService"]
