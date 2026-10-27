"""The scheduler service: one pipeline at a time, never clear on failure (M3 gate).

Everything here is fictional. Time is injected (``Clock``); no test sleeps. The
overlap tests use threads that rendezvous on events, so their outcome does not
depend on timing.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import structlog
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED, JobEvent
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi.testclient import TestClient
from fl_engine import NOW as ENGINE_NOW
from fl_engine import routes
from fl_notification_service import (
    SENTINEL,
    Clock,
    Posts,
    finding,
    make_service,
    outcome,
    result_of,
)
from fl_notification_state import all_rows, make_engine, put_row
from fl_on_demand import StoringAssessor
from fl_service import make_settings
from structlog.testing import capture_logs

from family_logistics.adapters.apprise.client import NotificationTagError
from family_logistics.api.app import create_app
from family_logistics.cli import startup_problems
from family_logistics.core.config import Settings
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.db.school_snapshots import SchoolSnapshotRepository
from family_logistics.domain.models import RunStatus
from family_logistics.services import scheduler as scheduler_module
from family_logistics.services.assessments import AssessmentService
from family_logistics.services.collector import CollectionRun
from family_logistics.services.notifications import (
    DispatchReport,
    NotificationService,
    NotificationStateError,
)
from family_logistics.services.pipeline import PipelineCoordinator
from family_logistics.services.scheduler import (
    DAILY_JOB,
    PURGE_JOB,
    SCAN_JOB,
    URGENT_RETRY_JOB,
    WEEKLY_JOB,
    SchedulerService,
    crons_in_quiet_hours,
    scheduler_problems,
)

pytestmark = pytest.mark.unit

WINDOW = datetime(2026, 10, 24, tzinfo=UTC)
JOIN_SECONDS = 10
SAMPLE_CREDENTIAL = "-".join(["sample", "value", "only"])


def complete_run() -> CollectionRun:
    return CollectionRun(window_start=WINDOW, window_end=WINDOW + timedelta(days=7))


def failed_run() -> CollectionRun:
    return CollectionRun(
        window_start=WINDOW,
        window_end=WINDOW + timedelta(days=7),
        discovery_error="DiscoveryError",
    )


class FakeCollector:
    def __init__(self, calls: list[str], *, result=None, error=None, on_run=None):
        self.calls = calls
        self.result = result or complete_run()
        self.error = error
        self.on_run = on_run

    def run_once(self):
        self.calls.append("collect")
        if self.on_run is not None:
            self.on_run()
        if self.error is not None:
            raise self.error
        return self.result


class FakeAssessor:
    def __init__(self, calls: list[str], *, result=None, error=None):
        self.calls = calls
        self.result = result if result is not None else outcome(result_of())
        self.error = error
        self.triggers: list[str] = []
        self.schools: list[object] = []

    def run(self, trigger, school=None):
        self.calls.append("run")
        self.triggers.append(trigger)
        self.schools.append(school)
        if self.error is not None:
            raise self.error
        return self.result


class FakeSchool:
    SNAPSHOT = object()

    def latest(self):
        return self.SNAPSHOT


class FakeNotifier:
    def __init__(self, calls: list[str], *, reports=None, error=None, on_call=None):
        self.calls = calls
        self.reports = list(reports or [])
        self.error = error
        self.on_call = on_call
        self.dispatched: list[object] = []
        self.unavailable: list[str] = []

    def _answer(self):
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        return self.reports.pop(0) if self.reports else DispatchReport("r", "shadow")

    def dispatch(self, run_outcome):
        self.calls.append("dispatch")
        self.dispatched.append(run_outcome)
        return self._answer()

    def dispatch_unavailable(self, trigger):
        self.calls.append("dispatch_unavailable")
        self.unavailable.append(trigger)
        return self._answer()


class FakeAps:
    """The slice of ``BackgroundScheduler`` the service uses, recorded."""

    def __init__(self):
        self.running = False
        self.jobs: dict[str, SimpleNamespace] = {}
        self.listeners: list[tuple[object, int]] = []
        self.shutdown_waited: bool | None = None

    def add_job(self, func, trigger, **options):
        self.jobs[options["id"]] = SimpleNamespace(
            func=func, trigger=trigger, **options
        )

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def get_jobs(self):
        return list(self.jobs.values())

    def add_listener(self, callback, mask):
        self.listeners.append((callback, mask))

    def start(self):
        self.running = True

    def shutdown(self, wait=True):
        self.running = False
        self.shutdown_waited = wait


class Rig:
    """A service over fakes, with the shared call log."""

    def __init__(self, engine, **options):
        self.calls: list[str] = options.pop("calls", None) or []
        self.clock = options.pop("clock", None) or Clock()
        self.collector = options.pop("collector", None) or FakeCollector(self.calls)
        self.assessor = options.pop("assessor", None) or FakeAssessor(self.calls)
        self.notifier = options.pop("notifier", None) or FakeNotifier(self.calls)
        self.aps = options.pop("aps", None) or FakeAps()
        settings = options.pop("settings", None) or make_settings()
        school = options.pop("school", None) or FakeSchool()
        self.on_stop_calls: list[str] = []
        self.service = SchedulerService(
            settings,
            engine,
            collector=self.collector,
            assessor=self.assessor,
            school=school,
            notifier=self.notifier,
            clock=self.clock,
            scheduler=self.aps,
            on_stop=[lambda: self.on_stop_calls.append("closed")],
            **options,
        )


@contextmanager
def scheduler_events(monkeypatch):
    """Capture the scheduler's structured events (its logger may be cached)."""
    with capture_logs() as events:
        monkeypatch.setattr(
            scheduler_module, "logger", structlog.get_logger("scheduler-test")
        )
        yield events


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


@pytest.fixture
def rig(engine):
    return Rig(engine)


def in_thread(function):
    """Run ``function`` in a thread; return the thread and a result list."""
    results: list[object] = []
    thread = threading.Thread(target=lambda: results.append(function()))
    thread.start()
    return thread, results


@contextmanager
def pipeline_held(engine, **options):
    """Yield a rig whose pipeline lock is held by a running scan until exit."""
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    collector = FakeCollector(
        calls, on_run=lambda: (entered.set(), release.wait(JOIN_SECONDS))
    )
    rig = Rig(engine, collector=collector, calls=calls, **options)
    holder, _ = in_thread(rig.service.run_scan)
    assert entered.wait(JOIN_SECONDS)
    try:
        yield rig
    finally:
        release.set()
        holder.join(JOIN_SECONDS)


class TestProblems:
    def test_defaults_are_usable(self):
        assert scheduler_problems(make_settings()) == []

    @pytest.mark.parametrize("minutes", [5, 14, 15])
    def test_a_scan_interval_not_above_the_claim_lifetime_is_a_problem(self, minutes):
        problems = scheduler_problems(make_settings(change_scan_minutes=minutes))
        assert len(problems) == 1
        assert "FL_CHANGE_SCAN_MINUTES" in problems[0]
        assert "15 minute" in problems[0]

    def test_an_interval_just_above_the_claim_lifetime_is_accepted(self):
        assert scheduler_problems(make_settings(change_scan_minutes=16)) == []

    def test_an_unparseable_cron_is_named_without_its_value(self):
        problems = scheduler_problems(make_settings(daily_cron="not a cron"))
        assert problems == ["FL_DAILY_CRON is not a valid five-field cron expression"]

    def test_a_cron_that_never_fires_is_a_problem(self):
        settings = make_settings(weekly_cron="0 0 31 2 *")
        assert scheduler_problems(settings) == ["FL_WEEKLY_CRON never fires"]

    @pytest.mark.parametrize(
        "expression",
        ["0 15 * * 0", "0 15 * * 7", "0 15 * * 1-5", "0 15 * * 1,3", "0 15 * * */2"],
    )
    @pytest.mark.parametrize("name", ["daily_cron", "weekly_cron"])
    def test_a_numeric_day_of_week_is_refused(self, name, expression):
        problems = scheduler_problems(make_settings(**{name: expression}))
        assert len(problems) == 1
        assert name.upper() in problems[0]
        assert "day of week" in problems[0]
        assert "mon, tue" in problems[0]

    @pytest.mark.parametrize(
        "expression",
        ["0 15 * * sun", "0 15 * * SUN", "0 8 * * mon-fri", "0 8 * * mon,wed"],
    )
    def test_day_names_are_accepted(self, expression):
        settings = make_settings(weekly_cron=expression, daily_cron=expression)
        assert scheduler_problems(settings) == []

    def test_a_star_or_a_digit_in_another_field_is_not_a_day_of_week(self):
        assert scheduler_problems(make_settings(daily_cron="*/30 9-17 1,15 * *")) == []

    def test_apscheduler_still_counts_day_zero_as_monday(self):
        """The reason numbers are refused; if this fails the refusal can go."""
        trigger = CronTrigger.from_crontab("0 15 * * 0", timezone=UTC)
        sunday_evening = datetime(2026, 10, 25, 16, 0, tzinfo=UTC)
        fire = trigger.get_next_fire_time(None, sunday_evening)
        assert fire is not None
        assert fire.strftime("%A") == "Monday"

    @pytest.mark.parametrize("provider", ["static", "google_routes"])
    def test_a_route_provider_the_factory_cannot_build_is_a_problem(self, provider):
        problems = scheduler_problems(make_settings(route_provider=provider))
        assert len(problems) == 1
        assert "FL_ROUTE_PROVIDER" in problems[0]

    def test_construction_refuses_a_short_interval_with_a_clear_error(self, engine):
        settings = make_settings(change_scan_minutes=10)
        with pytest.raises(ConfigurationError) as caught:
            Rig(engine, settings=settings)
        assert "FL_CHANGE_SCAN_MINUTES must exceed" in str(caught.value)


class TestQuietHoursWarning:
    NOW = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)

    def test_default_crons_are_outside_quiet_hours(self):
        assert crons_in_quiet_hours(make_settings(), self.NOW) == []

    def test_a_daily_cron_inside_quiet_hours_is_named(self):
        settings = make_settings(daily_cron="0 23 * * *")
        assert crons_in_quiet_hours(settings, self.NOW) == ["FL_DAILY_CRON"]

    def test_a_weekly_cron_inside_quiet_hours_is_named(self):
        settings = make_settings(weekly_cron="30 5 * * SUN")
        assert crons_in_quiet_hours(settings, self.NOW) == ["FL_WEEKLY_CRON"]

    def test_an_every_minute_cron_is_named_however_often_it_fires(self):
        settings = make_settings(daily_cron="* * * * *")
        morning = datetime(2026, 10, 26, 16, 0, tzinfo=UTC)  # 09:00 local
        assert crons_in_quiet_hours(settings, morning) == ["FL_DAILY_CRON"]

    def test_start_warns_about_each_such_cron(self, engine, monkeypatch):
        settings = make_settings(daily_cron="0 23 * * *", weekly_cron="0 3 * * SUN")
        warned = Rig(engine, settings=settings)
        with scheduler_events(monkeypatch) as events:
            warned.service.start()
        names = [e["setting"] for e in events if e["event"].endswith("quiet_hours")]
        assert names == ["FL_DAILY_CRON", "FL_WEEKLY_CRON"]


class TestRegistration:
    def test_start_registers_the_four_jobs_one_instance_each(self, rig):
        rig.service.start()
        jobs = rig.aps.jobs
        assert set(jobs) == {SCAN_JOB, DAILY_JOB, WEEKLY_JOB, PURGE_JOB}
        for job in jobs.values():
            assert job.max_instances == 1
            assert job.coalesce is True
            assert job.misfire_grace_time == 900
        assert rig.aps.running

    def test_the_scan_uses_the_configured_interval_and_a_short_first_delay(self, rig):
        rig.service.start()
        trigger = rig.aps.jobs[SCAN_JOB].trigger
        assert isinstance(trigger, IntervalTrigger)
        assert trigger.interval == timedelta(minutes=60)
        assert trigger.start_date == rig.clock.now + timedelta(seconds=60)

    def test_crons_use_the_configured_expressions_in_the_configured_zone(self, engine):
        settings = make_settings(
            daily_cron="15 18 * * *", timezone="America/Los_Angeles"
        )
        rig = Rig(engine, settings=settings)
        rig.service.start()
        daily = rig.aps.jobs[DAILY_JOB].trigger
        assert "hour='18'" in str(daily)
        assert "minute='15'" in str(daily)
        assert str(daily.timezone) == "America/Los_Angeles"

    def test_a_second_start_does_not_register_twice(self, rig):
        rig.service.start()
        rig.service.start()
        assert len(rig.aps.jobs) == 4
        assert len(rig.aps.listeners) == 1

    def test_a_missed_or_overlapping_job_is_counted_as_skipped(self, rig):
        rig.service.start()
        callback, mask = rig.aps.listeners[0]
        assert mask == EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES
        callback(JobEvent(EVENT_JOB_MISSED, SCAN_JOB, "default"))
        callback(JobEvent(EVENT_JOB_MAX_INSTANCES, SCAN_JOB, "default"))
        assert rig.service.stats()[SCAN_JOB].skipped == 2

    def test_a_second_start_on_a_real_scheduler_does_nothing(self, engine):
        real = BackgroundScheduler(timezone="Etc/UTC")
        rig = Rig(engine, aps=real)
        rig.service.start()
        rig.service.start()
        assert real.running
        assert len(real.get_jobs()) == 4
        rig.service.stop()

    def test_a_real_scheduler_starts_and_stops_cleanly(self, engine):
        real = BackgroundScheduler(timezone="Etc/UTC")
        rig = Rig(engine, aps=real)
        rig.service.start()
        assert sorted(job.id for job in real.get_jobs()) == [
            DAILY_JOB,
            PURGE_JOB,
            SCAN_JOB,
            WEEKLY_JOB,
        ]
        rig.service.stop()
        assert not real.running
        assert rig.on_stop_calls == ["closed"]


class TestPipeline:
    def test_a_scan_collects_then_assesses_then_dispatches(self, rig):
        report = rig.service.run_scan()
        assert rig.calls == ["collect", "run", "dispatch"]
        assert report.outcome == "ok"
        assert report.collection == "complete"
        assert report.run_id == "run-1"
        assert report.run_status == RunStatus.ATTENTION_REQUIRED.value
        assert rig.assessor.triggers == ["incremental"]

    def test_the_latest_school_snapshot_reaches_the_run(self, rig):
        rig.service.run_scan()
        assert rig.assessor.schools == [FakeSchool.SNAPSHOT]

    def test_daily_and_weekly_use_their_triggers(self, rig):
        rig.service.run_daily()
        rig.service.run_weekly()
        assert rig.assessor.triggers == ["daily", "weekly"]

    def test_the_dispatch_receives_the_stored_run(self, rig):
        rig.service.run_scan()
        assert rig.notifier.dispatched == [rig.assessor.result]

    def test_an_incomplete_collection_still_runs_and_is_reported(self, engine):
        calls: list[str] = []
        rig = Rig(engine, collector=FakeCollector(calls, result=failed_run()))
        report = rig.service.run_scan()
        assert report.collection == "incomplete"
        assert report.outcome == "ok"
        assert rig.notifier.dispatched

    def test_a_collector_crash_is_kept_and_the_run_judges_freshness(self, engine):
        calls: list[str] = []
        collector = FakeCollector(calls, error=RuntimeError(SENTINEL))
        rig = Rig(engine, collector=collector, calls=calls)
        report = rig.service.run_scan()
        assert report.collection == "crashed"
        assert report.outcome == "ok"
        assert "run" in rig.calls

    def test_a_failed_stored_run_is_dispatched_not_dropped(self, engine):
        failed = outcome(result_of(status=RunStatus.FAILED), run_id="run-failed")
        rig = Rig(engine, assessor=FakeAssessor([], result=failed))
        report = rig.service.run_scan()
        assert report.run_status == "failed"
        assert rig.notifier.dispatched == [failed]

    def test_a_run_that_raised_goes_to_the_unavailable_push_never_silence(self, engine):
        calls: list[str] = []
        rig = Rig(
            engine,
            assessor=FakeAssessor(calls, error=RuntimeError(SENTINEL)),
            notifier=FakeNotifier(calls),
            calls=calls,
        )
        report = rig.service.run_scan()
        assert report.outcome == "unavailable"
        assert report.run_id is None
        assert rig.notifier.unavailable == ["incremental"]
        assert rig.notifier.dispatched == []

    def test_an_unavailable_push_that_also_fails_is_a_dispatch_failure(self, engine):
        calls: list[str] = []
        rig = Rig(
            engine,
            assessor=FakeAssessor(calls, error=RuntimeError("x")),
            notifier=FakeNotifier(calls, error=OSError("x")),
        )
        assert rig.service.run_scan().outcome == "dispatch_failed"

    def test_a_dispatch_error_is_kept_and_counted(self, engine):
        rig = Rig(engine, notifier=FakeNotifier([], error=OSError("x")))
        report = rig.service.run_scan()
        assert report.outcome == "dispatch_failed"
        assert report.dispatch is None
        assert rig.service.stats()[SCAN_JOB].failures == 1

    def test_a_tag_error_does_not_stop_the_tick_or_the_next_one(self, engine):
        notifier = FakeNotifier([], error=NotificationTagError("tag", field="tag"))
        rig = Rig(engine, notifier=notifier)
        assert rig.service.run_scan().outcome == "dispatch_failed"
        notifier.error = None
        assert rig.service.run_scan().outcome == "ok"

    def test_an_unexpected_error_is_a_crashed_tick_and_the_scheduler_continues(
        self, engine
    ):
        assessor = FakeAssessor([], result=object())
        rig = Rig(engine, assessor=assessor)
        assert rig.service.run_scan().outcome == "crashed"
        assessor.result = outcome(result_of())
        assert rig.service.run_scan().outcome == "ok"
        stats = rig.service.stats()[SCAN_JOB]
        assert (stats.ticks, stats.failures) == (2, 1)
        assert stats.last_outcome == "ok"
        assert stats.last_ok is not None


class TestUrgentRetry:
    URGENT = DispatchReport("run-1", "shadow", urgent_failed=1)

    def test_an_urgent_failure_schedules_one_extra_pipeline_in_five_minutes(
        self, engine
    ):
        rig = Rig(engine, notifier=FakeNotifier([], reports=[self.URGENT]))
        rig.service.run_scan()
        job = rig.aps.jobs[URGENT_RETRY_JOB]
        assert isinstance(job.trigger, DateTrigger)
        assert job.trigger.run_date == rig.clock.now + timedelta(minutes=5)
        assert job.max_instances == 1
        assert job.func == rig.service.run_urgent_retry

    def test_no_retry_is_scheduled_when_nothing_urgent_failed(self, rig):
        rig.service.run_scan()
        assert URGENT_RETRY_JOB not in rig.aps.jobs

    def test_a_pending_retry_is_not_pushed_back(self, engine):
        reports = [self.URGENT, self.URGENT]
        rig = Rig(engine, notifier=FakeNotifier([], reports=reports))
        rig.service.run_scan()
        first = rig.aps.jobs[URGENT_RETRY_JOB]
        rig.clock.advance(timedelta(minutes=2))
        rig.service.run_scan()
        assert rig.aps.jobs[URGENT_RETRY_JOB] is first

    def test_a_state_error_carrying_an_urgent_failure_still_schedules_the_retry(
        self, engine
    ):
        error = NotificationStateError(
            "unwritable", operation="complete", report=self.URGENT
        )
        rig = Rig(engine, notifier=FakeNotifier([], error=error))
        report = rig.service.run_scan()
        assert report.outcome == "dispatch_failed"
        assert report.dispatch is self.URGENT
        assert URGENT_RETRY_JOB in rig.aps.jobs

    def test_an_unavailable_push_that_fails_urgently_also_schedules_the_retry(
        self, engine
    ):
        assessor = FakeAssessor([], error=ValueError(SENTINEL))
        notifier = FakeNotifier([], reports=[self.URGENT])
        rig = Rig(engine, assessor=assessor, notifier=notifier)
        report = rig.service.run_scan()
        assert report.outcome == "unavailable"
        assert URGENT_RETRY_JOB in rig.aps.jobs

    def test_nothing_is_scheduled_once_the_service_is_stopping(self, engine):
        rig = Rig(engine, notifier=FakeNotifier([], reports=[self.URGENT]))
        rig.notifier.on_call = rig.service.stop
        report = rig.service.run_scan()
        assert report.outcome == "ok"
        assert URGENT_RETRY_JOB not in rig.aps.jobs

    def test_the_retry_is_a_fresh_pipeline_that_never_chains(self, engine):
        reports = [self.URGENT]
        rig = Rig(engine, notifier=FakeNotifier([], reports=reports))
        rig.service.run_scan()
        del rig.aps.jobs[URGENT_RETRY_JOB]
        rig.notifier.reports.append(self.URGENT)
        report = rig.service.run_urgent_retry()
        assert report.outcome == "ok"
        assert rig.calls.count("collect") == 2
        assert URGENT_RETRY_JOB not in rig.aps.jobs


class TestOverlap:
    def test_a_scan_that_finds_a_pipeline_running_is_skipped_not_queued(self, engine):
        entered, release = threading.Event(), threading.Event()
        calls: list[str] = []
        collector = FakeCollector(
            calls, on_run=lambda: (entered.set(), release.wait(JOIN_SECONDS))
        )
        rig = Rig(
            engine,
            collector=collector,
            assessor=FakeAssessor(calls),
            notifier=FakeNotifier(calls),
            calls=calls,
        )
        first, first_result = in_thread(rig.service.run_scan)
        assert entered.wait(JOIN_SECONDS)
        second = rig.service.run_scan()
        release.set()
        first.join(JOIN_SECONDS)
        assert second.outcome == "skipped_busy"
        assert [r.outcome for r in first_result] == ["ok"]
        assert calls == ["collect", "run", "dispatch"]
        assert rig.service.stats()[SCAN_JOB].skipped == 1

    def test_a_purge_that_cannot_get_the_lock_in_time_is_skipped(self, engine):
        with pipeline_held(engine, lock_wait=timedelta(0)) as rig:
            purge = rig.service.run_purge()
            assert purge.outcome == "skipped_busy"
            assert rig.notifier.unavailable == []

    def test_a_waiting_job_runs_after_the_pipeline_and_never_inside_it(self, engine):
        entered, release = threading.Event(), threading.Event()
        calls: list[str] = []
        collector = FakeCollector(
            calls, on_run=lambda: (entered.set(), release.wait(JOIN_SECONDS))
        )
        rig = Rig(
            engine,
            collector=collector,
            assessor=FakeAssessor(calls),
            notifier=FakeNotifier(calls),
            calls=calls,
        )
        first, _ = in_thread(rig.service.run_scan)
        assert entered.wait(JOIN_SECONDS)
        second, second_result = in_thread(rig.service.run_daily)
        release.set()
        first.join(JOIN_SECONDS)
        second.join(JOIN_SECONDS)
        assert [r.outcome for r in second_result] == ["ok"]
        assert calls == ["collect", "run", "dispatch"] * 2

    def test_two_overlapping_scans_never_double_send(self, engine):
        posts = Posts()
        service, _, clock = make_service(engine, posts=posts)
        stored = outcome(result_of(finding()))
        entered, release = threading.Event(), threading.Event()
        calls: list[str] = []
        collector = FakeCollector(
            calls, on_run=lambda: (entered.set(), release.wait(JOIN_SECONDS))
        )
        rig = Rig(
            engine,
            collector=collector,
            assessor=FakeAssessor(calls, result=stored),
            notifier=service,
            clock=clock,
            calls=calls,
        )
        first, first_result = in_thread(rig.service.run_scan)
        assert entered.wait(JOIN_SECONDS)
        second = rig.service.run_scan()
        release.set()
        first.join(JOIN_SECONDS)
        assert second.outcome == "skipped_busy"
        assert [r.outcome for r in first_result] == ["ok"]
        assert len(posts.payloads) == 1
        assert set(posts.tags) == {"fl-shadow"}

    def test_the_lock_is_released_after_a_crash(self, engine):
        assessor = FakeAssessor([], result=object())
        rig = Rig(engine, assessor=assessor)
        assert rig.service.run_scan().outcome == "crashed"
        assert not rig.service.pipeline_lock.locked()


class TestCouldNotVerify:
    """A tick that cannot start owes the family a "could not verify" push."""

    URGENT = DispatchReport("run-1", "shadow", urgent_failed=1)

    @pytest.mark.parametrize(
        ("method", "job", "trigger"),
        [("run_daily", DAILY_JOB, "daily"), ("run_weekly", WEEKLY_JOB, "weekly")],
    )
    def test_a_status_job_that_waited_out_the_lock_says_it_could_not_verify(
        self, engine, method, job, trigger
    ):
        with pipeline_held(engine, lock_wait=timedelta(0)) as rig:
            report = getattr(rig.service, method)()
            assert report.outcome == "unavailable"
            assert rig.notifier.unavailable == [trigger]
            assert rig.notifier.dispatched == []
        stats = rig.service.stats()[job]
        assert (stats.ticks, stats.failures) == (1, 1)

    def test_the_lock_wait_expiry_is_logged_without_content(self, engine, monkeypatch):
        with pipeline_held(engine, lock_wait=timedelta(0)) as rig:
            with scheduler_events(monkeypatch) as events:
                rig.service.run_daily()
            expired = [e for e in events if e["event"] == "scheduler.lock_wait_expired"]
        assert len(expired) == 1
        assert set(expired[0]) == {"event", "job", "log_level"}
        assert expired[0]["log_level"] == "warning"

    def test_an_urgent_retry_that_waited_out_the_lock_says_it_could_not_verify(
        self, engine
    ):
        notifier = FakeNotifier([], reports=[self.URGENT])
        with pipeline_held(engine, lock_wait=timedelta(0), notifier=notifier) as rig:
            report = rig.service.run_urgent_retry()
            assert report.outcome == "unavailable"
            assert rig.notifier.unavailable == ["incremental"]
            assert URGENT_RETRY_JOB not in rig.aps.jobs

    def test_a_busy_scan_is_skipped_without_a_push(self, engine):
        with pipeline_held(engine) as rig:
            report = rig.service.run_scan()
            assert report.outcome == "skipped_busy"
            assert rig.notifier.unavailable == []
        assert rig.service.stats()[SCAN_JOB].failures == 0

    def test_a_push_that_fails_is_counted_and_never_raised(self, engine):
        notifier = FakeNotifier([], error=RuntimeError(SENTINEL))
        with pipeline_held(engine, lock_wait=timedelta(0), notifier=notifier) as rig:
            report = rig.service.run_daily()
        assert report.outcome == "dispatch_failed"
        assert rig.service.stats()[DAILY_JOB].failures == 1

    def test_an_urgent_failure_of_that_push_schedules_the_retry(self, engine):
        notifier = FakeNotifier([], reports=[self.URGENT])
        with pipeline_held(engine, lock_wait=timedelta(0), notifier=notifier) as rig:
            rig.service.run_daily()
            assert URGENT_RETRY_JOB in rig.aps.jobs

    def test_nothing_is_sent_when_the_wait_ended_because_of_a_stop(self, engine):
        with pipeline_held(engine, lock_wait=timedelta(0)) as rig:
            rig.service.stop()
            report = rig.service.run_daily()
            assert report.outcome == "stopped"
            assert rig.notifier.unavailable == []

    @pytest.mark.parametrize(
        ("job", "trigger"), [(DAILY_JOB, "daily"), (WEEKLY_JOB, "weekly")]
    )
    def test_a_missed_status_fire_says_it_could_not_verify(self, rig, job, trigger):
        rig.service.start()
        callback, _ = rig.aps.listeners[0]
        callback(JobEvent(EVENT_JOB_MISSED, job, "default"))
        assert rig.notifier.unavailable == [trigger]
        stats = rig.service.stats()[job]
        assert (stats.skipped, stats.failures) == (1, 1)

    @pytest.mark.parametrize(
        ("code", "job"),
        [
            (EVENT_JOB_MISSED, SCAN_JOB),
            (EVENT_JOB_MISSED, PURGE_JOB),
            (EVENT_JOB_MAX_INSTANCES, DAILY_JOB),
        ],
    )
    def test_other_skipped_jobs_send_nothing(self, rig, code, job):
        rig.service.start()
        callback, _ = rig.aps.listeners[0]
        callback(JobEvent(code, job, "default"))
        assert rig.notifier.unavailable == []

    def test_a_missed_fire_during_shutdown_sends_nothing(self, rig):
        rig.service.start()
        callback, _ = rig.aps.listeners[0]
        rig.service.stop()
        callback(JobEvent(EVENT_JOB_MISSED, DAILY_JOB, "default"))
        assert rig.notifier.unavailable == []

    def test_a_missed_fire_whose_push_fails_does_not_stop_the_scheduler(self, engine):
        rig = Rig(engine, notifier=FakeNotifier([], error=RuntimeError(SENTINEL)))
        rig.service.start()
        callback, _ = rig.aps.listeners[0]
        callback(JobEvent(EVENT_JOB_MISSED, DAILY_JOB, "default"))
        assert rig.service.stats()[DAILY_JOB].failures == 1
        assert rig.service.run_scan().outcome == "dispatch_failed"


class TestBlockedScans:
    def settings(self):
        return make_settings(change_scan_minutes=20)

    def test_consecutive_busy_scans_are_counted_and_a_run_resets_them(self, engine):
        with pipeline_held(engine, settings=self.settings()) as rig:
            rig.service.run_scan()
            rig.service.run_scan()
            assert rig.service.stats()[SCAN_JOB].consecutive_skips == 2
        assert rig.service.run_scan().outcome == "ok"
        assert rig.service.stats()[SCAN_JOB].consecutive_skips == 0

    def test_an_hours_worth_of_busy_scans_warns(self, engine, monkeypatch):
        with (
            pipeline_held(engine, settings=self.settings()) as rig,
            scheduler_events(monkeypatch) as events,
        ):
            for _ in range(3):
                rig.service.run_scan()
        warned = [e for e in events if e["event"] == "scheduler.scans_blocked"]
        assert [e["consecutive_skips"] for e in warned] == [3]
        assert warned[0]["log_level"] == "warning"

    def test_fewer_busy_scans_do_not_warn(self, engine, monkeypatch):
        with (
            pipeline_held(engine, settings=self.settings()) as rig,
            scheduler_events(monkeypatch) as events,
        ):
            rig.service.run_scan()
            rig.service.run_scan()
        assert not [e for e in events if e["event"] == "scheduler.scans_blocked"]

    def test_the_tick_log_carries_the_counters(self, engine, monkeypatch):
        with pipeline_held(engine) as rig, scheduler_events(monkeypatch) as events:
            rig.service.run_scan()
        tick = next(e for e in events if e["event"] == "scheduler.tick_finished")
        assert (tick["failures"], tick["skipped"], tick["consecutive_skips"]) == (
            0,
            1,
            1,
        )


class TestShutdown:
    def test_stop_waits_for_the_running_job_and_runs_the_cleanups_once(self, rig):
        rig.service.start()
        rig.service.stop()
        rig.service.stop()
        assert rig.aps.shutdown_waited is True
        assert rig.on_stop_calls == ["closed"]

    def test_stop_before_start_is_safe(self, rig):
        rig.service.stop()
        assert rig.aps.shutdown_waited is None
        assert rig.on_stop_calls == ["closed"]

    def test_a_stopped_service_cannot_start_again(self, rig):
        rig.service.stop()
        with pytest.raises(ConfigurationError):
            rig.service.start()

    def test_no_pipeline_starts_after_stop(self, rig):
        rig.service.stop()
        report = rig.service.run_scan()
        assert report.outcome == "stopped"
        assert rig.calls == []

    def test_a_stop_during_collection_skips_assessment_and_dispatch(self, engine):
        calls: list[str] = []
        collector = FakeCollector(calls)
        rig = Rig(
            engine,
            collector=collector,
            assessor=FakeAssessor(calls),
            notifier=FakeNotifier(calls),
            calls=calls,
        )
        collector.on_run = rig.service.stop
        report = rig.service.run_scan()
        assert report.outcome == "stopped"
        assert calls == ["collect"]
        assert not rig.service.pipeline_lock.locked()

    def test_stopped_ticks_are_skips_not_failures(self, rig):
        rig.service.stop()
        rig.service.run_scan()
        stats = rig.service.stats()[SCAN_JOB]
        assert (stats.ticks, stats.failures, stats.skipped) == (0, 0, 1)

    def test_a_purge_that_waited_for_the_lock_does_not_run_after_a_stop(
        self, engine, monkeypatch
    ):
        ran: list[str] = []
        monkeypatch.setattr(
            scheduler_module,
            "_PURGES",
            (("only", lambda _c, *, now: ran.append("purged") or 1),),
        )
        with pipeline_held(engine) as rig:
            waiting, results = in_thread(rig.service.run_purge)
            rig.service.stop()
        waiting.join(JOIN_SECONDS)
        assert [r.outcome for r in results] == ["stopped"]
        assert ran == []


class SignallingExecutor(ThreadPoolExecutor):
    """A pool whose ``shutdown`` says it was reached (APScheduler holds its
    job-store lock while it runs)."""

    def __init__(self, reached: threading.Event):
        super().__init__(max_workers=2)
        self.reached = reached

    def shutdown(self, wait=True):
        self.reached.set()
        super().shutdown(wait)


class TestRealSchedulerShutdown:
    """APScheduler 3.x ``shutdown(wait=True)`` holds the job-store lock.

    On a regression the first test fails after ``JOIN_SECONDS``; the deadlocked
    pool thread then keeps the interpreter from exiting, so the CI job timeout
    ends the run.
    """

    URGENT = DispatchReport("run-1", "shadow", urgent_failed=1)

    def test_stop_during_an_urgent_failed_dispatch_does_not_deadlock(self, engine):
        in_dispatch, go, reached = (threading.Event() for _ in range(3))

        def hold_the_dispatch():
            in_dispatch.set()
            go.wait(JOIN_SECONDS)

        real = BackgroundScheduler(
            timezone="Etc/UTC", executors={"default": SignallingExecutor(reached)}
        )
        notifier = FakeNotifier([], reports=[self.URGENT], on_call=hold_the_dispatch)
        rig = Rig(engine, aps=real, notifier=notifier)
        rig.service.start()
        real.add_job(rig.service.run_scan, id="now")
        assert in_dispatch.wait(JOIN_SECONDS)
        stopper = threading.Thread(target=rig.service.stop, daemon=True)
        stopper.start()
        assert reached.wait(JOIN_SECONDS)
        go.set()
        stopper.join(JOIN_SECONDS)
        assert not stopper.is_alive()
        assert not rig.service.pipeline_lock.locked()
        assert rig.on_stop_calls == ["closed"]

    def test_a_retry_queued_before_the_stop_is_kept_for_shutdown_to_drop(self, engine):
        # A real scheduler runs a job whose run_date is already past, so the
        # fixed fake NOW (days ago) would race the assertion; use the wall clock.
        real = BackgroundScheduler(timezone="Etc/UTC")
        rig = Rig(
            engine,
            aps=real,
            clock=Clock(datetime.now(UTC)),
            notifier=FakeNotifier([], reports=[self.URGENT]),
        )
        rig.service.start()
        rig.service.run_scan()
        assert real.get_job(URGENT_RETRY_JOB) is not None
        rig.service.stop()
        assert not real.running


class TestPurge:
    def test_every_purge_runs_and_expired_notification_state_goes(self, engine):
        clock = Clock(datetime(2026, 10, 26, 10, 30, tzinfo=UTC))
        old = clock.now - timedelta(days=200)
        put_row(engine, is_open=False, updated_at=old, created_at=old)
        rig = Rig(engine, clock=clock)
        report = rig.service.run_purge()
        assert report.outcome == "ok"
        assert set(report.counts) == {
            "raw_source_text",
            "school_days",
            "route_cache",
            "route_requests",
            "notification_state",
        }
        assert report.counts["notification_state"] == 1
        assert all_rows(engine) == []

    def test_one_failing_purge_does_not_stop_the_rest(self, engine, monkeypatch):
        ran: list[str] = []

        def boom(connection, *, now):
            raise RuntimeError(SENTINEL)

        def fine(connection, *, now):
            ran.append("fine")
            return 3

        monkeypatch.setattr(
            scheduler_module, "_PURGES", (("first", boom), ("second", fine))
        )
        rig = Rig(engine)
        report = rig.service.run_purge()
        assert report.outcome == "purge_failed"
        assert report.counts == {"second": 3}
        assert ran == ["fine"]
        assert rig.service.stats()[PURGE_JOB].failures == 1


class TestLogs:
    def test_a_crash_is_logged_by_class_name_only(self, engine, monkeypatch):
        assessor = FakeAssessor([], error=ValueError(SENTINEL))
        rig = Rig(engine, assessor=assessor)
        with scheduler_events(monkeypatch) as events:
            rig.service.run_scan()
        failure = next(e for e in events if e["event"] == "scheduler.run_failed")
        assert failure["error"] == "ValueError"
        assert SENTINEL not in repr(events)

    def test_a_tick_logs_its_health_fields(self, rig, monkeypatch):
        with scheduler_events(monkeypatch) as events:
            rig.service.run_scan()
        tick = next(e for e in events if e["event"] == "scheduler.tick_finished")
        assert tick["job"] == SCAN_JOB
        assert tick["outcome"] == "ok"
        assert tick["run_id"] == "run-1"
        assert tick["collection"] == "complete"
        assert tick["sent"] == 0


class TestEndToEnd:
    """Real assessment and notification services behind the scheduler."""

    def build(self, engine):
        clock = Clock(ENGINE_NOW)
        notifier, posts, _ = make_service(engine, clock=clock)
        assessor = AssessmentService(engine, make_settings(), routes(), clock=clock)
        calls: list[str] = []
        rig = Rig(
            engine,
            collector=FakeCollector(calls),
            assessor=assessor,
            school=SchoolSnapshotRepository(engine),
            notifier=notifier,
            clock=clock,
        )
        return rig, posts

    def test_a_run_without_fresh_sources_is_never_reported_clear(self, engine):
        rig, posts = self.build(engine)
        report = rig.service.run_scan()
        assert report.outcome == "ok"
        assert report.run_status == RunStatus.INCOMPLETE.value
        assert report.run_status != RunStatus.CLEAR.value
        assert set(posts.tags) <= {"fl-shadow"}

    def test_a_run_that_raised_is_announced_in_shadow_mode(self, engine):
        clock = Clock()
        notifier, posts, _ = make_service(engine, clock=clock)
        calls: list[str] = []
        rig = Rig(
            engine,
            assessor=FakeAssessor(calls, error=RuntimeError(SENTINEL)),
            notifier=notifier,
            clock=clock,
        )
        report = rig.service.run_scan()
        assert report.outcome == "unavailable"
        assert report.dispatch is not None
        assert report.dispatch.mode == "shadow"
        assert posts.payloads
        assert set(posts.tags) == {"fl-shadow"}
        assert SENTINEL not in "".join(posts.bodies)


class TestFromSettings:
    def settings(self, **overrides):
        values = {
            "caldav_username": "morgan@example.invalid",
            "caldav_app_password": SAMPLE_CREDENTIAL,
            "apprise_url": "http://apprise.example.invalid",
        }
        return make_settings(**(values | overrides))

    def count_closes(self, monkeypatch):
        closed: list[str] = []
        original = NotificationService.close

        def close(self_):
            closed.append("notifier")
            original(self_)

        monkeypatch.setattr(NotificationService, "close", close)
        return closed

    def test_builds_a_service_and_stop_closes_what_it_opened(self, engine, monkeypatch):
        closed = self.count_closes(monkeypatch)
        service = SchedulerService.from_settings(self.settings(), engine)
        service.stop()
        assert closed == ["notifier"]

    def test_a_coordinator_gives_the_scheduler_its_lock_and_tracks_its_runs(
        self, engine, monkeypatch
    ):
        coordinator = PipelineCoordinator()
        marked: list[tuple[str | None, str | None]] = []

        class Recording(StoringAssessor):
            def run(self, trigger, school=None, *, run_id=None):
                marked.append((coordinator.active_run_id, run_id))
                return super().run(trigger, school, run_id=run_id)

        monkeypatch.setattr(
            scheduler_module, "AssessmentService", lambda *_a, **_k: Recording(engine)
        )
        monkeypatch.setattr(
            scheduler_module,
            "build_collector",
            lambda *_a, **_k: (FakeCollector([]), SimpleNamespace(close=lambda: None)),
        )
        monkeypatch.setattr(
            NotificationService,
            "dispatch",
            lambda _self, run: DispatchReport(run.run_id, "shadow"),
        )
        service = SchedulerService.from_settings(
            self.settings(), engine, coordinator=coordinator
        )
        try:
            assert service.pipeline_lock is coordinator.lock
            assert service.run_scan().outcome == "ok"
        finally:
            service.stop()
        assert len(marked) == 1
        active, run_id = marked[0]
        assert run_id is not None
        assert active == run_id
        assert coordinator.active_run_id is None

    def test_without_a_coordinator_the_scheduler_has_a_private_lock(self, engine):
        coordinator = PipelineCoordinator()
        service = SchedulerService.from_settings(self.settings(), engine)
        service.stop()
        assert service.pipeline_lock is not coordinator.lock

    def test_missing_caldav_credentials_are_a_configuration_error(
        self, engine, monkeypatch
    ):
        closed = self.count_closes(monkeypatch)
        settings = make_settings(apprise_url="http://apprise.example.invalid")
        with pytest.raises(ConfigurationError):
            SchedulerService.from_settings(settings, engine)
        assert closed == []

    def test_another_collector_error_is_not_called_missing_credentials(
        self, engine, monkeypatch
    ):
        closed = self.count_closes(monkeypatch)

        def broken(*_args, **_kwargs):
            msg = "unrelated wiring failure"
            raise ValueError(msg)

        monkeypatch.setattr(scheduler_module, "build_collector", broken)
        settings = self.settings()
        with pytest.raises(ValueError, match="unrelated wiring failure") as caught:
            SchedulerService.from_settings(settings, engine)
        assert caught.type is ValueError
        assert closed == ["notifier"]

    def test_a_short_interval_is_refused_and_nothing_stays_open(
        self, engine, monkeypatch
    ):
        closed = self.count_closes(monkeypatch)
        settings = self.settings(change_scan_minutes=5)
        with pytest.raises(ConfigurationError):
            SchedulerService.from_settings(settings, engine)
        assert closed == ["notifier"]


class TestWiring:
    """``serve`` and the app start the scheduler only behind the setting."""

    def test_the_app_does_not_build_a_scheduler_when_disabled(
        self, tmp_path, monkeypatch
    ):
        def refuse(*_args, **_kwargs):
            msg = "the scheduler must not be built when it is disabled"
            raise AssertionError(msg)

        monkeypatch.setattr(SchedulerService, "from_settings", refuse)
        app = create_app(Settings(database_path=tmp_path / "fl.db"))
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200

    def test_the_app_starts_and_stops_the_scheduler_when_enabled(
        self, tmp_path, monkeypatch
    ):
        events: list[str] = []

        class Stub:
            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

        monkeypatch.setattr(SchedulerService, "from_settings", lambda *_a, **_k: Stub())
        settings = Settings(database_path=tmp_path / "fl.db", scheduler_enabled=True)
        with TestClient(create_app(settings)):
            assert events == ["start"]
        assert events == ["start", "stop"]

    def test_a_failed_start_still_stops_the_service(self, tmp_path, monkeypatch):
        events: list[str] = []

        class Failing:
            def start(self):
                events.append("start")
                msg = "start failed"
                raise RuntimeError(msg)

            def stop(self):
                events.append("stop")

        monkeypatch.setattr(
            SchedulerService, "from_settings", lambda *_a, **_k: Failing()
        )
        settings = Settings(database_path=tmp_path / "fl.db", scheduler_enabled=True)
        app = create_app(settings)
        with pytest.raises(RuntimeError, match="start failed"), TestClient(app):
            pass
        assert events == ["start", "stop"]

    def test_a_configuration_error_stops_app_startup(self, tmp_path):
        settings = Settings(database_path=tmp_path / "fl.db", scheduler_enabled=True)
        app = create_app(settings)
        with pytest.raises(ConfigurationError), TestClient(app):
            pass

    def test_serve_refuses_an_unusable_scheduler_configuration(self, tmp_path):
        settings = Settings(
            ha_token="x" * 32,
            database_path=tmp_path / "fl.db",
            scheduler_enabled=True,
            change_scan_minutes=10,
        )
        problems = startup_problems(settings, upgrade_db=True)
        assert any("FL_CALDAV_USERNAME" in p for p in problems)
        assert any("FL_CHANGE_SCAN_MINUTES" in p for p in problems)

    @pytest.mark.parametrize("provider", ["static", "google_routes"])
    def test_serve_refuses_a_route_provider_the_scheduler_cannot_build(
        self, tmp_path, provider
    ):
        settings = Settings(
            ha_token="x" * 32,
            database_path=tmp_path / "fl.db",
            scheduler_enabled=True,
            route_provider=provider,
            caldav_username="morgan@example.invalid",
            caldav_app_password=SAMPLE_CREDENTIAL,
        )
        problems = startup_problems(settings, upgrade_db=True)
        assert len(problems) == 1
        assert "FL_ROUTE_PROVIDER" in problems[0]

    def test_serve_ignores_scheduler_settings_when_disabled(self, tmp_path):
        settings = Settings(
            ha_token="x" * 32,
            database_path=tmp_path / "fl.db",
            change_scan_minutes=10,
        )
        assert startup_problems(settings, upgrade_db=True) == []
