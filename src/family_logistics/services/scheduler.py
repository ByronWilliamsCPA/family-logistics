"""In-process scheduler (APScheduler 3.x): collect, assess and notify.

Jobs, in ``FL_TIMEZONE``: ``scan`` every ``FL_CHANGE_SCAN_MINUTES``, ``daily``
and ``weekly`` on their crons (the same pipeline under another trigger: collect,
``AssessmentService.run`` with the latest school snapshot, then dispatch);
``purge`` nightly; ``urgent-retry`` once, ``URGENT_RETRY_AFTER`` after a dispatch
reported ``urgent_failed`` (ADR 0008 D8; it schedules no further retry). A run
that raised goes to ``dispatch_unavailable``, so nothing is reported as clear.
Off unless ``FL_SCHEDULER_ENABLED``; ``serve`` starts it (``api/app.py``).

Obligations (ADR 0008 D6, D11):

- one pipeline at a time per process: every tick holds ``pipeline_lock`` from
  collection to dispatch (the watermark is checked at claim time only).
  ``max_instances=1`` and ``coalesce`` stop a job overlapping itself; a scan
  that finds the lock busy is skipped, the other jobs wait ``lock_wait``. The
  M4 on-demand API must take the lock too;
- a crash in a tick is caught, counted and logged by exception class only; only
  a configuration error at construction or start stops startup;
- the scan interval must exceed ``CLAIM_TTL`` (``scheduler_problems``) and a
  status cron inside quiet hours is warned about at start (its push is dropped);
- shutdown waits for the running pipeline; a hard kill mid-dispatch is covered
  by the claim lifetime (at-least-once).

Not built (roadmap): ``ops_alert`` for collection failures, the routing budget
and a degraded database. Until then the tick logs are the signal.

#CRITICAL: Concurrency: overlapping pipelines could dispatch an older run after
a newer one. #VERIFY: tests/unit/test_scheduler.py (TestOverlap).
#CRITICAL: Timing: an incomplete analysis is never reported as clear.
#VERIFY: tests/unit/test_scheduler.py (TestPipeline).
#CRITICAL: Security: children's data; logs carry names, ids and counts only.
#VERIFY: tests/unit/test_scheduler.py (TestLogs).
#ASSUME: Timing: a failed collection within the staleness limit lets the run
use stored events. #VERIFY: tests/unit/test_assessment_service.py.
#EDGE: Timing: a process death mid-pipeline leaves a claim that expires after
``CLAIM_TTL``. #VERIFY: tests/unit/test_notification_service.py (T-svc-16).
"""

# APScheduler 3.x ships no type information; its calls are the only untyped surface.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from family_logistics.adapters.routing.factory import build_route_provider
from family_logistics.core.crypto import AddressCipher
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.db import retention
from family_logistics.db.school_snapshots import SchoolSnapshotRepository
from family_logistics.domain.notification_policy import (
    CLAIM_TTL,
    NotificationPolicy,
    in_quiet_hours,
)
from family_logistics.services.assessments import AssessmentService
from family_logistics.services.notifications import (
    NotificationService,
    NotificationStateError,
)
from family_logistics.services.wiring import build_collector
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from apscheduler.events import JobEvent
    from apscheduler.triggers.base import BaseTrigger
    from sqlalchemy.engine import Engine

    from family_logistics.core.config import Settings
    from family_logistics.domain.events import SchoolSnapshot
    from family_logistics.services.assessments import RunOutcome
    from family_logistics.services.collector import CollectionRun
    from family_logistics.services.notifications import DispatchReport

logger = get_logger(__name__)

SCAN_JOB: Final = "scan"
DAILY_JOB: Final = "daily"
WEEKLY_JOB: Final = "weekly"
PURGE_JOB: Final = "purge"
URGENT_RETRY_JOB: Final = "urgent-retry"

URGENT_RETRY_AFTER: Final = timedelta(minutes=5)
"""The extra pipeline after ``urgent_failed`` (ADR 0008, D8)."""
STARTUP_SCAN_DELAY: Final = timedelta(seconds=60)
"""The first scan runs this long after ``start``, not a whole interval later."""
LOCK_WAIT: Final = timedelta(minutes=30)
"""How long a daily, weekly, purge or retry tick waits for a running pipeline."""
PURGE_HOUR: Final = 3
PURGE_MINUTE: Final = 30
QUIET_SAMPLE_DAYS: Final = 8
SCAN_SKIP_WARN_AFTER: Final = timedelta(hours=1)
"""Consecutive busy scans that add up to this long raise a warning."""
_GRACE_SECONDS: Final = int(CLAIM_TTL.total_seconds())
"""A job that cannot start within this long of its time is skipped."""
_PURGES: Final[tuple[tuple[str, Callable[..., int]], ...]] = (
    ("raw_source_text", retention.purge_raw_source_text),
    ("school_days", retention.purge_expired_school_days),
    ("route_cache", retention.purge_expired_route_cache),
    ("route_requests", retention.purge_expired_route_requests),
    ("notification_state", retention.purge_expired_notification_state),
)
_FAILURES: Final = frozenset(
    {"unavailable", "dispatch_failed", "crashed", "purge_failed"}
)
_SKIPS: Final = frozenset({"skipped_busy", "stopped"})
_MISSABLE: Final = {DAILY_JOB: "daily", WEEKLY_JOB: "weekly"}
"""The jobs whose missed fire is announced, with the trigger they run as."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _log_failure(event: str, exc: Exception, **fields: object) -> None:
    """Log a failure by exception class only: a message may carry household text."""
    logger.error(event, error=type(exc).__name__, **fields)


class Collecting(Protocol):
    """The read-only collector (``services.collector.Collector``)."""

    def run_once(self) -> CollectionRun:
        """Collect every selected calendar once."""
        ...


class Assessing(Protocol):
    """The assessment service (``services.assessments.AssessmentService``)."""

    def run(self, trigger: str, school: SchoolSnapshot | None = None) -> RunOutcome:
        """Run and store one assessment."""
        ...


class SchoolSource(Protocol):
    """The school snapshot store (``SchoolSnapshotRepository``)."""

    def latest(self) -> SchoolSnapshot | None:
        """Return the stored snapshot, or None."""


class Notifying(Protocol):
    """The notification service (``services.notifications.NotificationService``)."""

    def dispatch(self, outcome: RunOutcome) -> DispatchReport:
        """Send the pushes a stored run owes."""
        ...

    def dispatch_unavailable(self, trigger: str) -> DispatchReport:
        """Tell the family a run could not be verified."""
        ...


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one tick did.

    Attributes:
        job: The job name.
        outcome: ``ok``, ``unavailable`` (the run raised, the lock wait ran
            out or a fire was missed; the unavailable push ran),
            ``dispatch_failed``, ``crashed``, ``skipped_busy`` (a scan or purge
            found the pipeline busy), ``stopped`` or ``purge_failed``.
        run_id: The stored run, when there is one.
        run_status: The run's status value, when there is a run.
        collection: ``complete``, ``incomplete``, ``crashed`` or None.
        dispatch: What the notification service reported, when it did.
        counts: Rows purged per table (the purge job only).
    """

    job: str
    outcome: str
    run_id: str | None = None
    run_status: str | None = None
    collection: str | None = None
    dispatch: DispatchReport | None = None
    counts: Mapping[str, int] = field(default_factory=dict[str, int])


@dataclass(slots=True)
class JobStats:
    """Per-job health counters (in memory; the logs carry the history).

    Attributes:
        ticks: Ticks that ran (skipped ones excluded).
        failures: Ticks that ended in a failure outcome.
        skipped: Ticks skipped (busy, stopping, missed or overlapping).
        consecutive_skips: Busy skips in a row; any tick that ran resets it.
        last_outcome: The last tick's outcome.
        last_finished: When the last tick finished.
        last_ok: When the last ``ok`` tick finished.
    """

    ticks: int = 0
    failures: int = 0
    skipped: int = 0
    consecutive_skips: int = 0
    last_outcome: str | None = None
    last_finished: datetime | None = None
    last_ok: datetime | None = None


def _status_crons(settings: Settings) -> tuple[tuple[str, str], ...]:
    """Return the (setting name, expression) of the two status crons."""
    return (
        ("FL_DAILY_CRON", settings.daily_cron),
        ("FL_WEEKLY_CRON", settings.weekly_cron),
    )


def _cron(expression: str, zone: ZoneInfo) -> CronTrigger:
    """Parse a five-field cron expression in ``zone`` (use day names: SUN)."""
    return CronTrigger.from_crontab(expression, timezone=zone)


def _numeric_day_of_week(expression: str) -> bool:
    """Report whether the day-of-week field of a five-field cron has a digit.

    APScheduler 3.x counts day-of-week 0 as Monday where standard cron counts
    it as Sunday, so ``0 15 * * 0`` would fire on the wrong day. Names (``sun``,
    ``mon-fri``) mean the same everywhere.

    Args:
        expression: The cron expression.

    Returns:
        True when the fifth field contains a digit; False for any other shape
        (the parser reports a wrong field count).
    """
    fields = expression.split()
    return len(fields) == 5 and any(ch.isdigit() for ch in fields[4])


def scheduler_problems(settings: Settings, *, now: datetime | None = None) -> list[str]:
    """Return configuration that blocks the scheduler (names only, no values).

    Args:
        settings: Application settings.
        now: The reference instant for "can this cron fire" (tests).

    Returns:
        Problems; empty when the scheduler may start. The scan interval must
        exceed the notification claim lifetime (a crashed dispatch's claim has
        expired before the next scan) and both crons must parse and fire.
    """
    problems: list[str] = []
    if timedelta(minutes=settings.change_scan_minutes) <= CLAIM_TTL:
        minutes = int(CLAIM_TTL.total_seconds() // 60)
        what = "notification claim lifetime when FL_SCHEDULER_ENABLED=true"
        problems.append(
            f"FL_CHANGE_SCAN_MINUTES must exceed the {minutes} minute {what}"
        )
    if settings.route_provider != "ha_relay":
        what = "the other providers cannot be built for a deployment yet"
        problems.append(
            f"FL_ROUTE_PROVIDER must be ha_relay when FL_SCHEDULER_ENABLED=true ({what})"
        )
    zone = ZoneInfo(settings.timezone)
    for name, expression in _status_crons(settings):
        # #CRITICAL: Timing: APScheduler 3.x day-of-week numbers start at Monday.
        # #VERIFY: tests/unit/test_scheduler.py (TestProblems numeric day-of-week).
        if _numeric_day_of_week(expression):
            why = "APScheduler counts 0 as Monday where cron counts it as Sunday"
            problems.append(
                f"{name} must name the day of week (mon, tue, ..., sun): {why}"
            )
            continue
        try:
            fire = _cron(expression, zone).get_next_fire_time(None, now or _utc_now())
        except ValueError:
            problems.append(f"{name} is not a valid five-field cron expression")
            continue
        if fire is None:
            problems.append(f"{name} never fires")
    return problems


def crons_in_quiet_hours(settings: Settings, now: datetime) -> list[str]:
    """Name the status crons that fire inside quiet hours in the next days.

    A held ``status`` or ``report`` is dropped, not deferred (ADR 0008, D6).
    The check probes the start of each day's quiet window, so it does not
    depend on how often a cron fires: the first fire at or after the window
    start is in the window exactly when the window holds any fire.

    Args:
        settings: Application settings (valid crons; see ``scheduler_problems``).
        now: The reference instant.

    Returns:
        The names of the settings (``FL_DAILY_CRON``, ``FL_WEEKLY_CRON``).
    """
    policy = NotificationPolicy.from_settings(settings)
    horizon = now + timedelta(days=QUIET_SAMPLE_DAYS)
    today = now.astimezone(policy.zone).date()
    probes: list[datetime] = []
    for offset in range(-1, QUIET_SAMPLE_DAYS + 1):
        opens = datetime.combine(
            today + timedelta(days=offset), policy.quiet_start, tzinfo=policy.zone
        )
        probes.append(max(opens, now))
    named: list[str] = []
    for name, expression in _status_crons(settings):
        trigger = _cron(expression, policy.zone)
        for probe in probes:
            fire = trigger.get_next_fire_time(None, probe)
            if fire is None or fire > horizon:
                continue
            if in_quiet_hours(fire, policy.quiet_start, policy.quiet_end, policy.zone):
                named.append(name)
                break
    return named


class SchedulerService:
    """Runs collect, assess and notify on a schedule, one pipeline at a time.

    Args:
        settings: Application settings.
        engine: Database engine at the schema head (the retention purges).
        collector: The read-only collector.
        assessor: The assessment service.
        school: The school snapshot store.
        notifier: The notification service.
        clock: The current aware time (replaceable in tests).
        scheduler: The APScheduler instance; a ``BackgroundScheduler`` in
            ``FL_TIMEZONE`` when None.
        lock_wait: How long a waiting tick waits for a running pipeline.
        on_stop: Cleanups run by ``stop`` after the scheduler has shut down.

    Raises:
        ConfigurationError: ``scheduler_problems`` found something.
    """

    def __init__(  # noqa: PLR0913 - keyword-only collaborators, injected for tests
        self,
        settings: Settings,
        engine: Engine,
        *,
        collector: Collecting,
        assessor: Assessing,
        school: SchoolSource,
        notifier: Notifying,
        clock: Callable[[], datetime] = _utc_now,
        scheduler: BackgroundScheduler | None = None,
        lock_wait: timedelta = LOCK_WAIT,
        on_stop: Sequence[Callable[[], None]] = (),
    ) -> None:
        problems = scheduler_problems(settings, now=clock())
        if problems:
            msg = "scheduler configuration is unusable: " + "; ".join(problems)
            raise ConfigurationError(msg, details={"problems": problems})
        self._settings = settings
        self._engine = engine
        self._collector = collector
        self._assessor = assessor
        self._school = school
        self._notifier = notifier
        self._clock = clock
        self._zone = ZoneInfo(settings.timezone)
        self._aps = scheduler or BackgroundScheduler(timezone=self._zone)
        self._lock_wait = lock_wait
        self._on_stop = tuple(on_stop)
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self._stopping = threading.Event()
        self._stats: dict[str, JobStats] = {}
        self._stats_lock = threading.Lock()
        self._registered = False

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> SchedulerService:
        """Build the service and the objects it drives from settings.

        The route provider comes from the factory with the engine, so the
        default ``ha_relay`` setting yields the real provider (ADR 0006).

        Args:
            settings: Application settings (CalDAV credentials required).
            engine: Database engine at the schema head.
            clock: The current aware time (replaceable in tests).

        Returns:
            The service, not started; ``stop`` closes its HTTP clients.

        Raises:
            ConfigurationError: A setting is unusable (the scheduler problems,
                the Apprise client, the route provider or the CalDAV
                credentials); nothing stays open.
            Exception: Any other failure building the collector, passed on
                unchanged after the notification client is closed.
        """
        if not settings.caldav_username or settings.caldav_app_password is None:
            msg = "CalDAV credentials are not configured"
            raise ConfigurationError(msg)
        provider = build_route_provider(settings, engine=engine)
        notifier = NotificationService.from_settings(engine, settings)
        try:
            collector, http = build_collector(settings, engine)
        except Exception:
            notifier.close()
            raise
        cipher = None if settings.data_key is None else AddressCipher(settings.data_key)
        try:
            return cls(
                settings,
                engine,
                collector=collector,
                assessor=AssessmentService(
                    engine, settings, provider, cipher=cipher, clock=clock
                ),
                school=SchoolSnapshotRepository(engine),
                notifier=notifier,
                clock=clock,
                on_stop=(http.close, notifier.close),
            )
        except ConfigurationError:
            http.close()
            notifier.close()
            raise

    def start(self) -> None:
        """Register the jobs and start the scheduler thread.

        A second call while the scheduler is running does nothing.

        Raises:
            ConfigurationError: The service was stopped; build a new one.
        """
        with self._guard:
            if self._stopping.is_set():
                msg = "a stopped scheduler cannot be started again"
                raise ConfigurationError(msg)
            if self._aps.running:
                return
            now = self._clock()
            if not self._registered:
                self._register(now)
            for name in crons_in_quiet_hours(self._settings, now):
                logger.warning("scheduler.cron_in_quiet_hours", setting=name)
            if self._settings.apprise_url is None:
                logger.warning("scheduler.notifications_unconfigured")
            self._aps.start()
        logger.info(
            "scheduler.started",
            mode=self._settings.notification_mode,
            scan_minutes=self._settings.change_scan_minutes,
            jobs=sorted(job.id for job in self._aps.get_jobs()),
        )

    def stop(self) -> None:
        """Stop starting pipelines, wait for the running one and clean up.

        Safe to call more than once and before ``start``.
        """
        # #CRITICAL: Concurrency: APScheduler 3.x ``shutdown(wait=True)`` holds
        # its job-store lock while it waits for running jobs, and the urgent
        # retry needs that lock from a job thread. The guard makes the stop flag
        # and the retry scheduling mutually exclusive, and is released before
        # the shutdown wait so a job that wants it is never blocked behind it.
        # #VERIFY: tests/unit/test_scheduler.py (TestRealSchedulerShutdown).
        with self._guard:
            if self._stopping.is_set():
                return
            self._stopping.set()
        if self._aps.running:
            self._aps.shutdown(wait=True)
        for cleanup in self._on_stop:
            cleanup()
        logger.info("scheduler.stopped")

    def stats(self) -> dict[str, JobStats]:
        """Return a copy of the per-job counters (the health of the ticks).

        Returns:
            Counters by job name.
        """
        with self._stats_lock:
            return {name: replace(s) for name, s in self._stats.items()}

    @property
    def pipeline_lock(self) -> threading.Lock:
        """Return the lock a pipeline holds (the on-demand API must take it)."""
        return self._lock

    def run_scan(self) -> TickReport:
        """Run the incremental pipeline; ``skipped_busy`` when one is running. Never raises."""
        return self._tick(SCAN_JOB, lambda: self._pipeline(SCAN_JOB, "incremental"))

    def run_daily(self) -> TickReport:
        """Run the daily pipeline (the status push). Never raises."""
        return self._tick(
            DAILY_JOB, lambda: self._pipeline(DAILY_JOB, "daily", wait=True)
        )

    def run_weekly(self) -> TickReport:
        """Run the weekly pipeline (the report push). Never raises."""
        return self._tick(
            WEEKLY_JOB, lambda: self._pipeline(WEEKLY_JOB, "weekly", wait=True)
        )

    def run_urgent_retry(self) -> TickReport:
        """Run the one extra pipeline after an urgent push failed. Never raises."""
        return self._tick(
            URGENT_RETRY_JOB,
            lambda: self._pipeline(
                URGENT_RETRY_JOB, "incremental", wait=True, retry=False
            ),
        )

    def run_purge(self) -> TickReport:
        """Run the retention purges, each in its own transaction. Never raises."""
        return self._tick(PURGE_JOB, self._purge)

    def _add(
        self, func: Callable[[], TickReport], trigger: BaseTrigger, job: str
    ) -> None:
        self._aps.add_job(
            func,
            trigger,
            id=job,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_GRACE_SECONDS,
        )

    def _register(self, now: datetime) -> None:
        """Add the recurring jobs and the skipped-job listener."""
        settings = self._settings
        every = IntervalTrigger(
            minutes=settings.change_scan_minutes,
            start_date=now + STARTUP_SCAN_DELAY,
            timezone=self._zone,
        )
        self._add(self.run_scan, every, SCAN_JOB)
        self._add(self.run_daily, _cron(settings.daily_cron, self._zone), DAILY_JOB)
        self._add(self.run_weekly, _cron(settings.weekly_cron, self._zone), WEEKLY_JOB)
        nightly = CronTrigger(hour=PURGE_HOUR, minute=PURGE_MINUTE, timezone=self._zone)
        self._add(self.run_purge, nightly, PURGE_JOB)
        self._aps.add_listener(
            self._on_skipped, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES
        )
        self._registered = True

    def _on_skipped(self, event: JobEvent) -> None:
        """Count a job APScheduler skipped; announce a missed status or report.

        A missed daily or weekly fire is a status the family expected and will
        not get, so it is reported as "could not verify" (ADR 0008, F6). APScheduler
        calls this from a worker thread for a missed fire (no scheduler lock
        held) and from its own thread for an overlapping instance, which is
        only counted; nothing here touches APScheduler.

        Args:
            event: The skipped-job event.
        """
        job = str(event.job_id)
        with self._stats_lock:
            self._stats.setdefault(job, JobStats()).skipped += 1
        missed = event.code == EVENT_JOB_MISSED
        logger.warning(
            "scheduler.job_skipped",
            job=job,
            reason="missed" if missed else "max_instances",
        )
        if missed and job in _MISSABLE:
            self._tick(job, lambda: self._missed(job))

    def _missed(self, job: str) -> TickReport:
        """Send the unavailable push for a daily or weekly fire that never ran."""
        if self._stopping.is_set():
            return TickReport(job, "stopped")
        # #CRITICAL: Timing: a fire missed beyond the grace time means the
        # pipeline did not start (a stuck run or a stalled process). Without this
        # push the family would hear nothing, and silence reads as clear.
        # #VERIFY: tests/unit/test_scheduler.py (TestCouldNotVerify).
        return self._unverified(job, _MISSABLE[job], retry=True)

    def _tick(self, job: str, body: Callable[[], TickReport]) -> TickReport:
        """Run one tick, keeping any crash inside it."""
        started = time.monotonic()
        try:
            report = body()
        except Exception as exc:  # noqa: BLE001 - a tick must never stop the scheduler
            _log_failure("scheduler.job_crashed", exc, job=job)
            report = TickReport(job, "crashed")
        stats = self._record(report)
        logger.info(
            "scheduler.tick_finished",
            job=job,
            outcome=report.outcome,
            run_id=report.run_id,
            run_status=report.run_status,
            collection=report.collection,
            duration_ms=int((time.monotonic() - started) * 1000),
            ticks=stats.ticks,
            failures=stats.failures,
            skipped=stats.skipped,
            consecutive_skips=stats.consecutive_skips,
            **_dispatch_fields(report.dispatch),
        )
        if report.job == SCAN_JOB and report.outcome == "skipped_busy":
            self._warn_if_scans_stuck(stats.consecutive_skips)
        return report

    def _record(self, report: TickReport) -> JobStats:
        """Count one tick; return a copy of the job's counters."""
        with self._stats_lock:
            stats = self._stats.setdefault(report.job, JobStats())
            if report.outcome in _SKIPS:
                stats.skipped += 1
                if report.outcome == "skipped_busy":
                    stats.consecutive_skips += 1
                return replace(stats)
            now = self._clock()
            stats.ticks += 1
            stats.consecutive_skips = 0
            stats.last_outcome = report.outcome
            stats.last_finished = now
            if report.outcome in _FAILURES:
                stats.failures += 1
            elif report.outcome == "ok":
                stats.last_ok = now
            return replace(stats)

    def _warn_if_scans_stuck(self, consecutive: int) -> None:
        """Warn once per hour's worth of scans that found a pipeline running."""
        every = max(
            1,
            SCAN_SKIP_WARN_AFTER
            // timedelta(minutes=self._settings.change_scan_minutes),
        )
        if consecutive % every == 0:
            logger.warning("scheduler.scans_blocked", consecutive_skips=consecutive)

    def _acquire(self, *, wait: bool) -> bool:
        if not wait:
            return self._lock.acquire(blocking=False)
        return self._lock.acquire(timeout=self._lock_wait.total_seconds())

    def _pipeline(
        self, job: str, trigger: str, *, wait: bool = False, retry: bool = True
    ) -> TickReport:
        """Collect, assess and notify under the pipeline lock."""
        # #CRITICAL: Concurrency: one pipeline at a time per process. A scan
        # never queues behind a running pipeline; the others wait ``lock_wait``.
        # #VERIFY: tests/unit/test_scheduler.py (TestOverlap).
        if not self._acquire(wait=wait):
            return self._lock_unavailable(job, trigger, wait=wait, retry=retry)
        try:
            if self._stopping.is_set():
                return TickReport(job, "stopped")
            return self._locked_pipeline(job, trigger, retry=retry)
        finally:
            self._lock.release()

    def _lock_unavailable(
        self, job: str, trigger: str, *, wait: bool, retry: bool
    ) -> TickReport:
        """Handle a tick that never got the pipeline lock.

        A scan that finds it busy is skipped (the next scan covers it). A tick
        that waited the whole ``lock_wait`` is a failure: the status or report
        it owed is not coming, so the family is told "could not verify".
        """
        if self._stopping.is_set():
            return TickReport(job, "stopped")
        if not wait:
            return TickReport(job, "skipped_busy")
        logger.warning("scheduler.lock_wait_expired", job=job)
        # #CRITICAL: Timing: a pipeline that held the lock for ``lock_wait`` is
        # stuck or stale; this runs outside the lock, which is safe because the
        # notification watermark (ADR 0008, D11) makes that run's later push
        # stale and ``dispatch_unavailable`` advances it.
        # #VERIFY: tests/unit/test_scheduler.py (TestCouldNotVerify).
        return self._unverified(job, trigger, retry=retry)

    def _unverified(
        self,
        job: str,
        trigger: str,
        *,
        retry: bool,
        collection: str | None = None,
    ) -> TickReport:
        """Send the "could not verify" push and report the tick as a failure."""
        report, done = self._dispatch(
            lambda: self._notifier.dispatch_unavailable(trigger), job, retry=retry
        )
        return TickReport(
            job,
            "unavailable" if done else "dispatch_failed",
            collection=collection,
            dispatch=report,
        )

    def _locked_pipeline(self, job: str, trigger: str, *, retry: bool) -> TickReport:
        collection = self._collect(job)
        if self._stopping.is_set():
            return TickReport(job, "stopped", collection=collection)
        try:
            outcome = self._assessor.run(trigger, self._school.latest())
        except Exception as exc:  # noqa: BLE001 - an unreadable run is never silence
            _log_failure("scheduler.run_failed", exc, job=job)
            return self._unverified(job, trigger, retry=retry, collection=collection)
        report, done = self._dispatch(
            lambda: self._notifier.dispatch(outcome), job, retry=retry
        )
        return TickReport(
            job,
            "ok" if done else "dispatch_failed",
            run_id=outcome.run_id,
            run_status=outcome.result.run_status.value,
            collection=collection,
            dispatch=report,
        )

    def _collect(self, job: str) -> str:
        """Collect once; a failure is logged and the run judges freshness."""
        try:
            run = self._collector.run_once()
        except Exception as exc:  # noqa: BLE001 - the run still judges the sources
            _log_failure("scheduler.collect_crashed", exc, job=job)
            return "crashed"
        return "complete" if run.complete else "incomplete"

    def _dispatch(
        self, send: Callable[[], DispatchReport], job: str, *, retry: bool
    ) -> tuple[DispatchReport | None, bool]:
        """Dispatch, keep errors in and queue the urgent retry if one is owed.

        Args:
            send: The dispatch call.
            job: The job name, for the log.
            retry: Whether an urgent failure may queue the extra pipeline.

        Returns:
            The report (None when the dispatch raised with none attached) and
            whether the dispatch completed without raising.
        """
        report: DispatchReport | None
        done = True
        try:
            report = send()
        except NotificationStateError as exc:
            # The report says what was delivered before the state write failed.
            _log_failure("scheduler.dispatch_failed", exc, job=job)
            report, done = exc.report, False
        except Exception as exc:  # noqa: BLE001 - includes NotificationTagError
            _log_failure("scheduler.dispatch_failed", exc, job=job)
            report, done = None, False
        if retry and report is not None and report.urgent_failed:
            self._schedule_urgent_retry()
        return report, done

    def _schedule_urgent_retry(self) -> None:
        """Queue one extra pipeline; a pending one is not pushed back.

        Nothing is queued once ``stop`` has begun. The guard keeps the stop flag
        and the APScheduler calls apart (see ``stop``).
        """
        with self._guard:
            if self._stopping.is_set():
                logger.info("scheduler.urgent_retry_skipped", reason="stopping")
                return
            if self._aps.get_job(URGENT_RETRY_JOB) is not None:
                return
            run_at = self._clock() + URGENT_RETRY_AFTER
            self._add(
                self.run_urgent_retry,
                DateTrigger(run_date=run_at, timezone=self._zone),
                URGENT_RETRY_JOB,
            )
        logger.warning("scheduler.urgent_retry_scheduled", run_at=run_at.isoformat())

    def _purge(self) -> TickReport:
        """Run every purge in its own transaction under the pipeline lock."""
        if not self._acquire(wait=True):
            return TickReport(PURGE_JOB, "skipped_busy")
        try:
            if self._stopping.is_set():
                return TickReport(PURGE_JOB, "stopped")
            now = self._clock()
            counts: dict[str, int] = {}
            failed = False
            for name, purge in _PURGES:
                try:
                    with self._engine.begin() as connection:
                        counts[name] = purge(connection, now=now)
                except Exception as exc:  # noqa: BLE001 - one purge must not stop the rest
                    failed = True
                    _log_failure("scheduler.purge_failed", exc, table=name)
            logger.info("scheduler.purged", **counts)
            outcome = "purge_failed" if failed else "ok"
            return TickReport(PURGE_JOB, outcome, counts=counts)
        finally:
            self._lock.release()


def _dispatch_fields(report: DispatchReport | None) -> dict[str, object]:
    """Return the dispatch counts a tick log line carries (no text)."""
    if report is None:
        return {}
    return {
        "sent": report.sent,
        "failed": report.failed,
        "urgent_failed": report.urgent_failed,
        "held": report.held,
        "dropped": report.dropped,
        "stale": report.stale,
        "unconfigured": report.unconfigured,
    }
