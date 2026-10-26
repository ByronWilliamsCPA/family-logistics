"""Notification policy: which findings become pushes and when (ADR 0008).

Everything here is pure: no I/O, no clock reads (``now`` is passed in), no
logging, and no push text. The policy decides the kind of each push, its
production tag, whether it is owed again, and whether quiet hours hold it. The
service (a later PR) claims, renders and sends. A ``PlannedPush`` carries the
tracked finding (the ADR interface), so the content boundary (D2) is enforced
at render time: the renderer reads only the structured subject and rule id,
never the finding's message, suggestion or raw entity keys.

Kinds (D3): ``urgent`` and ``notice`` for findings, ``status`` and ``report``
for completed runs, ``ops`` for service-health episodes. The kind alone
decides the production tag; shadow mode routes every kind to the shadow tag.

#CRITICAL: Timing: a failed or incomplete run is never reported as clear, and
an older outcome can never send after a newer one. This module never plans a
``status`` or ``report`` push for a failed or unavailable run, labels every
planned run-level push with the run's own status, and drops a status or report
that is older than one scan interval.
#VERIFY: tests/unit/test_notification_policy.py (never-clear properties).
#CRITICAL: Timing: a ride in progress is imminent until it ends; its critical
problem is ``urgent`` and ``decide`` never holds an urgent push for quiet
hours or a cooldown (at most one quiet-hours push per episode).
#VERIFY: tests/unit/test_notification_policy.py (in-progress properties).
#ASSUME: Timing: quiet hours (22:00 to 06:00), the 180-minute imminent window,
one quiet-hours push per episode and "CAP-014 is never urgent" suit the
household.
#VERIFY: owner approval before FL_NOTIFICATION_MODE=production.
#ASSUME: Timing: an unknown ride set counts as imminent, so a failed run with
no readable history sends one urgent "could not verify" per local day.
#VERIFY: owner approval before FL_NOTIFICATION_MODE=production;
tests/unit/test_notification_policy.py::test_unknown_ride_set_is_imminent_once_per_day.
#EDGE: Timing: DST. Quiet hours are evaluated per real instant (each instant
has exactly one local wall time), so a nonexistent boundary takes effect at
the next existing local time and a repeated one applies on both passes.
#VERIFY: tests/unit/test_notification_policy.py (spring forward, fall back).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal
from zoneinfo import ZoneInfo

from family_logistics.domain.context import EngineSettings
from family_logistics.domain.lifecycle import Lifecycle
from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.notification_subjects import (
    Resolution,
    is_imminent,
    resolve_subject,
    run_imminent,
)

if TYPE_CHECKING:
    from datetime import datetime, time

    from family_logistics.core.config import Settings
    from family_logistics.domain.engine import AssessmentResult
    from family_logistics.domain.lifecycle import TrackedFinding
    from family_logistics.domain.models import Finding
    from family_logistics.domain.notification_subjects import RideIndex, Subject
    from family_logistics.domain.timeline import DayTimeline


class PushKind(StrEnum):
    """What a push is for; the kind alone decides the production tag."""

    NOTICE = "notice"
    URGENT = "urgent"
    STATUS = "status"
    REPORT = "report"
    OPS = "ops"


class Decision(StrEnum):
    """What the service does with one planned push."""

    SEND = "send"
    DUPLICATE = "duplicate"
    QUIET_HOURS = "quiet_hours"
    BYPASS_USED = "bypass_used"
    COOLDOWN = "cooldown"
    IN_FLIGHT = "in_flight"
    STALE = "stale"


RENOTIFY_COOLDOWN: Final = timedelta(hours=4)
"""A new episode's notice is held while the last delivery is younger than this."""
CLAIM_TTL: Final = timedelta(minutes=15)
"""A claim older than this is abandoned (its dispatch crashed)."""
SEND_DEADLINE: Final = timedelta(minutes=10)
"""A dispatch stops starting new sends after this, below ``CLAIM_TTL``."""
URGENT_RETRY_DELAY: Final = timedelta(seconds=5)
"""Wait before the one in-dispatch retry of a failed urgent push."""
CLAIM_LOCK_RETRIES: Final = 3
"""Claim transaction retries while the database is busy."""
MAX_ITEMS_PER_PUSH: Final = 8
"""Notice items per Apprise call."""
CLEAR_PHRASES: Final = ("Clear", "No open findings", "sources fresh")
"""Wording only the clear status or report may use."""

_SEVERITY_RANK: Final = {
    Severity.CRITICAL: 3,
    Severity.INCOMPLETE: 2,
    Severity.WARNING: 1,
}
"""Same order as ``domain.lifecycle._RANK`` (a test pins them equal)."""

_NEVER_URGENT_RULES: Final = frozenset({"CAP-014"})


def severity_rank(severity: Severity) -> int:
    """Rank a severity for escalation: critical, then incomplete, then warning.

    Args:
        severity: A finding severity.

    Returns:
        A higher number for a more severe finding.
    """
    return _SEVERITY_RANK[severity]


@dataclass(frozen=True, slots=True)
class TagSet:
    """The six dedicated Apprise tags (recipients live in Apprise, not here)."""

    report: str
    status: str
    warning: str
    urgent: str
    ops: str
    shadow: str

    @classmethod
    def from_settings(cls, settings: Settings) -> TagSet:
        """Read the tags from settings.

        Args:
            settings: Application settings.

        Returns:
            The tag set.
        """
        return cls(
            report=settings.apprise_tag_report,
            status=settings.apprise_tag_status,
            warning=settings.apprise_tag_warning,
            urgent=settings.apprise_tag_urgent,
            ops=settings.apprise_tag_ops,
            shadow=settings.apprise_tag_shadow,
        )


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    """Settings the policy needs, resolved once per dispatch.

    Attributes:
        zone: ``FL_TIMEZONE``.
        quiet_start: ``FL_QUIET_HOURS_START`` (naive local time).
        quiet_end: ``FL_QUIET_HOURS_END`` (naive local time).
        imminent_window: ``FL_IMMINENT_WINDOW_MINUTES``.
        status_max_age: ``FL_CHANGE_SCAN_MINUTES``; an older status is stale.
        pickup_tolerance: The engine's pickup tolerance (its default).
        tags: The dedicated tags.
        mode: ``shadow`` (default) or ``production``.
    """

    zone: ZoneInfo
    quiet_start: time
    quiet_end: time
    imminent_window: timedelta
    status_max_age: timedelta
    pickup_tolerance: timedelta
    tags: TagSet
    mode: Literal["shadow", "production"]

    @classmethod
    def from_settings(cls, settings: Settings) -> NotificationPolicy:
        """Build the policy from settings.

        Args:
            settings: Application settings.

        Returns:
            The policy.
        """
        return cls(
            zone=ZoneInfo(settings.timezone),
            quiet_start=settings.quiet_hours_start,
            quiet_end=settings.quiet_hours_end,
            imminent_window=timedelta(minutes=settings.imminent_window_minutes),
            status_max_age=timedelta(minutes=settings.change_scan_minutes),
            pickup_tolerance=EngineSettings.from_config(settings).pickup_tolerance,
            tags=TagSet.from_settings(settings),
            mode=settings.notification_mode,
        )


@dataclass(frozen=True, slots=True)
class PlannedPush:
    """One push the policy wants considered, before any state is read.

    Attributes:
        subject_key: ``issue:<id>``, ``run-failed``, ``run:<run id>``,
            ``ops:run_failed`` or ``ops:run_incomplete``.
        kind: The planned kind. For ``retry_only`` pushes the service uses the
            kind stored on the row, which ``decide`` also does.
        intended_tag: The production tag of the kind.
        episode_start: The finding is ``new`` or ``regressed`` in this run.
        retry_only: Failed run: send only if the row is open and undelivered.
        run_wide: The daily re-arm applies (DATA-011, ``run-failed``).
        rule_id: The finding's rule, None for run, run-failed and ops subjects.
        severity: The finding's severity at emission.
        tracked: The finding with its lifecycle, for rendering.
        subject: The derived subject, for rendering.
        run_status: The run's status, for ``status`` and ``report`` pushes.
    """

    subject_key: str
    kind: PushKind
    intended_tag: str
    episode_start: bool = False
    retry_only: bool = False
    run_wide: bool = False
    rule_id: str | None = None
    severity: Severity | None = None
    tracked: TrackedFinding | None = None
    subject: Subject | None = None
    run_status: RunStatus | None = None


@dataclass(frozen=True, slots=True)
class StateView:
    """One ``notification_state`` row as the policy sees it.

    Attributes:
        is_open: The subject's episode is open (always False for ``run:*``).
        delivered: This episode was delivered.
        kind: Kind last delivered, or planned while undelivered.
        severity: Severity last delivered; None until delivered.
        bypass_used: A quiet-hours push was sent this episode.
        episode_run_id: The run that opened the episode.
        last_sent_at: Last delivery (aware UTC), kept across closes.
        claimed_at: When the current claim was taken, if any.
    """

    is_open: bool
    delivered: bool
    kind: PushKind
    severity: Severity | None
    bypass_used: bool
    episode_run_id: str
    last_sent_at: datetime | None
    claimed_at: datetime | None


@dataclass(frozen=True, slots=True)
class Plan:
    """What one outcome asks the service to consider.

    Attributes:
        pushes: Urgent first, then notice, status and report, then ops.
        open_issue_keys: ``issue:*`` subjects open in this result.
        completed: False for a failed or unavailable run: close nothing.
        incomplete: Keeps ``ops:run_incomplete`` open.
        unresolved_rule_ids: Rule IDs of findings whose subject could not be
            derived in a completed run (counted as imminent; the service logs
            the count and the IDs, never content).
        status_stale: A status or report was dropped for being older than one
            scan interval.
    """

    pushes: tuple[PlannedPush, ...]
    open_issue_keys: frozenset[str]
    completed: bool
    incomplete: bool
    unresolved_rule_ids: tuple[str, ...] = ()
    status_stale: bool = False

    @property
    def unresolved(self) -> int:
        """Return how many findings had no derivable subject."""
        return len(self.unresolved_rule_ids)


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{name} must be timezone-aware"
        raise ValueError(msg)
    return value


def in_quiet_hours(now: datetime, start: time, end: time, zone: ZoneInfo) -> bool:
    """Report whether an instant is inside the quiet-hours window.

    The instant is converted to local wall time; no local datetime is ever
    built from a boundary, so a nonexistent boundary (spring forward) takes
    effect at the first instant whose local time is at or after it, and a
    repeated one (fall back) applies on both passes.

    Args:
        now: The instant (aware).
        start: Window start, a naive local time.
        end: Window end, a naive local time.
        zone: The local zone.

    Returns:
        ``start <= t < end`` for a same-day window, else ``t >= start or
        t < end`` when the window wraps midnight, with ``t`` the local time.

    Raises:
        ValueError: ``now`` is naive, a boundary is time-zone aware (it would
            raise ``TypeError`` on comparison), or ``start == end``.
    """
    _aware(now, "now")
    if start.tzinfo is not None or end.tzinfo is not None:
        msg = "quiet-hours boundaries must be naive local times"
        raise ValueError(msg)
    if start == end:
        msg = "quiet-hours start and end must differ"
        raise ValueError(msg)
    t = now.astimezone(zone).time()
    if start < end:
        return start <= t < end
    return t >= start or t < end


def finding_kind(finding: Finding, *, imminent: bool) -> PushKind:
    """Choose the kind of a finding's push (the D3 table).

    Args:
        finding: The finding; only ``rule_id`` and ``severity`` are read.
        imminent: Whether the finding's ride or move is imminent or in progress.

    Returns:
        ``urgent`` for an imminent critical or incomplete finding, except
        CAP-014 which is always ``notice``; ``notice`` for everything else.
    """
    if finding.rule_id in _NEVER_URGENT_RULES or finding.severity is Severity.WARNING:
        return PushKind.NOTICE
    return PushKind.URGENT if imminent else PushKind.NOTICE


def production_tag(kind: PushKind, tags: TagSet) -> str:
    """Return the tag a kind uses in production.

    Args:
        kind: The push kind.
        tags: The dedicated tags.

    Returns:
        The tag value.
    """
    return {
        PushKind.URGENT: tags.urgent,
        PushKind.NOTICE: tags.warning,
        PushKind.STATUS: tags.status,
        PushKind.REPORT: tags.report,
        PushKind.OPS: tags.ops,
    }[kind]


def delivery_tag(kind: PushKind, policy: NotificationPolicy) -> str:
    """Return the tag a push is actually sent to.

    Args:
        kind: The push kind.
        policy: The policy (mode and tags).

    Returns:
        The shadow tag in shadow mode, else the kind's production tag.
    """
    if policy.mode == "shadow":
        return policy.tags.shadow
    return production_tag(kind, policy.tags)


_RUN_FAILED: Final = "run-failed"
_OPS_FAILED: Final = "ops:run_failed"
_OPS_INCOMPLETE: Final = "ops:run_incomplete"
_STATUS_TRIGGERS: Final = frozenset({"daily", "on_demand"})
_REPORT_TRIGGERS: Final = frozenset({"weekly"})
_KIND_ORDER: Final = {
    PushKind.URGENT: 0,
    PushKind.NOTICE: 1,
    PushKind.STATUS: 2,
    PushKind.REPORT: 2,
    PushKind.OPS: 3,
}
_RUN_LEVEL_KINDS: Final = frozenset({PushKind.STATUS, PushKind.REPORT})


@dataclass(frozen=True, slots=True)
class _Context:
    """What planning one outcome needs besides the findings."""

    now: datetime
    policy: NotificationPolicy
    rides: RideIndex | None
    timelines: tuple[DayTimeline, ...]
    run_wide_imminent: bool


def _finding_imminent(resolution: Resolution, subject: Subject, ctx: _Context) -> bool:
    """Decide imminence from a resolved subject (unresolved counts as imminent)."""
    if resolution is Resolution.RUN_WIDE:
        return ctx.run_wide_imminent
    if resolution is Resolution.DAY:
        return subject.day == ctx.now.astimezone(ctx.policy.zone).date()
    if subject.due_at is None or subject.due_end is None:
        return True
    return is_imminent(
        subject.due_at, subject.due_end, ctx.now, ctx.policy.imminent_window
    )


def _plan_finding(tracked: TrackedFinding, ctx: _Context) -> tuple[PlannedPush, bool]:
    """Plan one finding's push; also report whether its subject was unresolved."""
    finding = tracked.finding
    resolution, subject = resolve_subject(
        finding,
        rides=ctx.rides,
        timelines=ctx.timelines,
        pickup_tolerance=ctx.policy.pickup_tolerance,
    )
    kind = finding_kind(finding, imminent=_finding_imminent(resolution, subject, ctx))
    pushed = PlannedPush(
        subject_key=f"issue:{finding.issue_id}",
        kind=kind,
        intended_tag=production_tag(kind, ctx.policy.tags),
        episode_start=tracked.lifecycle in {Lifecycle.NEW, Lifecycle.REGRESSED},
        run_wide=resolution is Resolution.RUN_WIDE,
        rule_id=finding.rule_id,
        severity=finding.severity,
        tracked=tracked,
        subject=subject,
    )
    return pushed, resolution is Resolution.UNRESOLVED


def _run_wide_push(key: str, kind: PushKind, policy: NotificationPolicy) -> PlannedPush:
    return PlannedPush(
        subject_key=key,
        kind=kind,
        intended_tag=production_tag(kind, policy.tags),
        run_wide=key == _RUN_FAILED,
    )


def _failed_run_pushes(
    imminent: bool, policy: NotificationPolicy
) -> tuple[PlannedPush, PlannedPush]:
    """Return the ``run-failed`` and ``ops:run_failed`` pushes of a failed run."""
    kind = PushKind.URGENT if imminent else PushKind.NOTICE
    return (
        _run_wide_push(_RUN_FAILED, kind, policy),
        _run_wide_push(_OPS_FAILED, PushKind.OPS, policy),
    )


def _ordered(pushes: list[PlannedPush]) -> tuple[PlannedPush, ...]:
    return tuple(sorted(pushes, key=lambda p: _KIND_ORDER[p.kind]))


def _plan_failed(
    *,
    result: AssessmentResult,
    rides: RideIndex | None,
    now: datetime,
    policy: NotificationPolicy,
) -> Plan:
    """Plan a failed run: ``run-failed``, ``ops:run_failed`` and retries only."""
    imminent = run_imminent(
        rides,
        (),
        now=now,
        window=policy.imminent_window,
        pickup_tolerance=policy.pickup_tolerance,
    )
    pushes = list(_failed_run_pushes(imminent, policy))
    ctx = _Context(now, policy, rides, (), imminent)
    keys: set[str] = set()
    for tracked in result.findings:
        resolution, subject = resolve_subject(
            tracked.finding,
            rides=rides,
            timelines=(),
            pickup_tolerance=policy.pickup_tolerance,
        )
        # The row's own kind decides a retry; this one only labels the plan.
        kind = finding_kind(
            tracked.finding, imminent=_finding_imminent(resolution, subject, ctx)
        )
        key = f"issue:{tracked.finding.issue_id}"
        keys.add(key)
        pushes.append(
            PlannedPush(
                subject_key=key,
                kind=kind,
                intended_tag=production_tag(kind, policy.tags),
                retry_only=True,
                rule_id=tracked.finding.rule_id,
                severity=tracked.finding.severity,
                tracked=tracked,
                subject=subject,
            )
        )
    return Plan(_ordered(pushes), frozenset(keys), completed=False, incomplete=False)


def _run_level_pushes(
    run_id: str, trigger: str, result: AssessmentResult, ctx: _Context
) -> tuple[list[PlannedPush], bool]:
    """Plan the status or report and the ops push; flag a stale status."""
    pushes: list[PlannedPush] = []
    stale = False
    status_kind: PushKind | None = None
    if trigger in _STATUS_TRIGGERS:
        status_kind = PushKind.STATUS
    elif trigger in _REPORT_TRIGGERS:
        status_kind = PushKind.REPORT
    if status_kind is not None:
        if ctx.now - result.evaluated_at > ctx.policy.status_max_age:
            stale = True
        else:
            pushes.append(
                PlannedPush(
                    subject_key=f"run:{run_id}",
                    kind=status_kind,
                    intended_tag=production_tag(status_kind, ctx.policy.tags),
                    run_status=result.run_status,
                )
            )
    if result.run_status is RunStatus.INCOMPLETE:
        pushes.append(_run_wide_push(_OPS_INCOMPLETE, PushKind.OPS, ctx.policy))
    return pushes, stale


def plan(  # noqa: PLR0913 - everything but the outcome is keyword-only
    *,
    run_id: str,
    trigger: str,
    result: AssessmentResult,
    rides: RideIndex | None,
    now: datetime,
    policy: NotificationPolicy,
) -> Plan:
    """Plan the pushes one run outcome asks for (D3 and D4).

    Args:
        run_id: The run's ID (the status subject and the episode owner).
        trigger: ``daily``, ``weekly``, ``on_demand`` or ``incremental``; any
            other value plans no status or report.
        result: The run's result. A failed result plans retries only.
        rides: The run's ride index, or None when unknown (counts as imminent).
        now: The dispatch clock (aware).
        policy: The resolved policy.

    Returns:
        The plan: urgent pushes first, then notices, the status or report and
        ops pushes, with the unresolved findings counted by rule ID.

    """
    _aware(now, "now")
    if result.run_status is RunStatus.FAILED:
        return _plan_failed(result=result, rides=rides, now=now, policy=policy)
    run_wide_imminent = run_imminent(
        rides,
        result.timelines,
        now=now,
        window=policy.imminent_window,
        pickup_tolerance=policy.pickup_tolerance,
    )
    ctx = _Context(now, policy, rides, result.timelines, run_wide_imminent)
    pushes: list[PlannedPush] = []
    unresolved: list[str] = []
    for tracked in result.findings:
        pushed, was_unresolved = _plan_finding(tracked, ctx)
        pushes.append(pushed)
        if was_unresolved:
            unresolved.append(tracked.finding.rule_id)
    run_pushes, stale = _run_level_pushes(run_id, trigger, result, ctx)
    return Plan(
        _ordered([*pushes, *run_pushes]),
        frozenset(p.subject_key for p in pushes),
        completed=True,
        incomplete=result.run_status is RunStatus.INCOMPLETE,
        unresolved_rule_ids=tuple(unresolved),
        status_stale=stale,
    )


def plan_unavailable(*, now: datetime, policy: NotificationPolicy) -> Plan:
    """Plan the pushes for a run that never produced an outcome (D4).

    Args:
        now: The dispatch clock (aware).
        policy: The resolved policy.

    Returns:
        ``run-failed`` (urgent, since the ride set is unknown) and
        ``ops:run_failed``; nothing is closed.

    """
    _aware(now, "now")
    return Plan(
        _ordered(list(_failed_run_pushes(imminent=True, policy=policy))),
        frozenset(),
        completed=False,
        incomplete=False,
    )


def _owed_in_episode(push: PlannedPush, row: StateView, *, new_day: bool) -> bool:
    """Report whether an open episode's row is owed another push."""
    if not row.delivered:
        return True
    escalated = (
        row.severity is not None
        and push.severity is not None
        and severity_rank(push.severity) > severity_rank(row.severity)
    )
    kind_rose = push.kind is PushKind.URGENT and row.kind is PushKind.NOTICE
    rearmed = push.run_wide and push.kind is PushKind.URGENT and new_day
    return escalated or kind_rose or rearmed


def _owed(
    push: PlannedPush, row: StateView | None, run_id: str, *, new_day: bool
) -> tuple[bool, bool]:
    """Return ``(owed, starts_new_episode)`` for a push against its row."""
    if push.retry_only:
        return row is not None and row.is_open and not row.delivered, False
    if row is None:
        return True, True
    if push.kind in _RUN_LEVEL_KINDS:
        return not row.delivered, True
    if not row.is_open or (push.episode_start and row.episode_run_id != run_id):
        return True, True
    return _owed_in_episode(push, row, new_day=new_day), False


def _in_cooldown(row: StateView | None, now: datetime, kind: PushKind) -> bool:
    """Report whether a new episode's notice is held by the cooldown (D5).

    The test is the persisted row, not the transient ``episode_start`` flag: a
    held notice writes nothing, so on the next scan its finding is ``ongoing``
    and only the still-closed row shows it is owed as a new episode. An open
    row means the episode never closed (a missed dispatch), so a regression
    on it is news and is not held.
    """
    return (
        kind is PushKind.NOTICE
        and row is not None
        and not row.is_open
        and row.last_sent_at is not None
        and now - row.last_sent_at < RENOTIFY_COOLDOWN
    )


def _is_new_day(row: StateView | None, now: datetime, zone: ZoneInfo) -> bool:
    """Report whether ``now`` is a later local calendar day than the last send."""
    if row is None or row.last_sent_at is None:
        return False
    return now.astimezone(zone).date() > row.last_sent_at.astimezone(zone).date()


def decide(  # noqa: PLR0913 - the clock and quiet flag are keyword-only
    push: PlannedPush,
    state: StateView | None,
    *,
    run_id: str,
    now: datetime,
    quiet: bool,
    policy: NotificationPolicy,
) -> Decision:
    """Decide what to do with one planned push (D4 to D6).

    The order is: in flight, duplicate, cooldown, quiet hours or a used
    bypass, send. A retry uses the kind stored on its row. The cooldown holds
    only a notice that starts a new episode against a closed row, so an
    undelivered notice of an open episode is never held by it.

    Args:
        push: The planned push.
        state: Its ``notification_state`` row, if any.
        run_id: The run being dispatched (a different one reopens an episode).
        now: The dispatch clock (aware).
        quiet: Whether ``now`` is inside quiet hours.
        policy: The resolved policy (the local zone for the daily re-arm).

    Returns:
        The decision. ``STALE`` is never returned here: ``plan`` drops a late
        status or report, because only the plan knows when the run was
        evaluated.

    """
    _aware(now, "now")
    if (
        state is not None
        and state.claimed_at is not None
        and now - state.claimed_at < CLAIM_TTL
    ):
        return Decision.IN_FLIGHT
    owed, new_episode = _owed(
        push, state, run_id, new_day=_is_new_day(state, now, policy.zone)
    )
    if not owed:
        return Decision.DUPLICATE
    kind = state.kind if push.retry_only and state is not None else push.kind
    if new_episode and _in_cooldown(state, now, kind):
        return Decision.COOLDOWN
    if not quiet:
        return Decision.SEND
    # #CRITICAL: Timing: only an urgent push bypasses quiet hours, once per episode.
    # #VERIFY: tests/unit/test_notification_policy.py::test_only_urgent_bypasses_and_only_once.
    if kind is not PushKind.URGENT:
        return Decision.QUIET_HOURS
    spent = not new_episode and state is not None and state.bypass_used
    return Decision.BYPASS_USED if spent else Decision.SEND


__all__ = [
    "CLAIM_LOCK_RETRIES",
    "CLAIM_TTL",
    "CLEAR_PHRASES",
    "MAX_ITEMS_PER_PUSH",
    "RENOTIFY_COOLDOWN",
    "SEND_DEADLINE",
    "URGENT_RETRY_DELAY",
    "Decision",
    "NotificationPolicy",
    "Plan",
    "PlannedPush",
    "PushKind",
    "StateView",
    "TagSet",
    "decide",
    "delivery_tag",
    "finding_kind",
    "in_quiet_hours",
    "plan",
    "plan_unavailable",
    "production_tag",
    "severity_rank",
]
