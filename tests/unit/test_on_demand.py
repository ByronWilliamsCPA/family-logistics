"""On-demand runs: coalescing, the shared pipeline lock, failures and shutdown.

Threads rendezvous on events, so no outcome depends on timing. Everything is
fictional; the assessor is a fake that stores real run rows.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fl_notification_service import Clock
from fl_on_demand import (
    JOIN_SECONDS,
    FakeCollector,
    FakeSchool,
    NoAps,
    Notifier,
    ProbeLock,
    StoringAssessor,
    in_thread,
    make_engine,
    wait_until,
)
from fl_service import make_settings

from family_logistics.db.assessments import AssessmentRepository
from family_logistics.domain.models import RunStatus
from family_logistics.services.on_demand import (
    OnDemandService,
    OnDemandStoppedError,
)
from family_logistics.services.pipeline import (
    REMEMBERED_FAILURES,
    PipelineCoordinator,
)
from family_logistics.services.scheduler import SCAN_JOB, SchedulerService

pytestmark = pytest.mark.unit


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


def build(engine, assessor, *, coordinator=None, **options):
    coordinator = coordinator or PipelineCoordinator()
    service = OnDemandService(
        coordinator,
        lambda: assessor,
        FakeSchool(),
        AssessmentRepository(engine),
        **options,
    )
    return service, coordinator


def finished(service, run_id):
    def ended():
        progress = service.progress(run_id)
        return progress is not None and progress.state == "finished"

    assert wait_until(ended), "the run never ended"
    progress = service.progress(run_id)
    assert progress is not None
    return progress


def make_scheduler(engine, coordinator, assessor, on_run=None):
    """A scheduler that shares the coordinator's lock and tracks its runs."""
    return SchedulerService(
        make_settings(),
        engine,
        collector=FakeCollector(on_run),
        assessor=coordinator.tracked(assessor),
        school=FakeSchool(),
        notifier=Notifier(),
        clock=Clock(),
        scheduler=NoAps(),
        lock=coordinator.lock,
    )


class RaisesWhenReleased:
    """A scheduled assessor that raises before any run row exists."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.run_id: str | None = None

    def run(self, trigger, school=None, *, run_id=None):
        self.run_id = run_id
        self.started.set()
        assert self.release.wait(JOIN_SECONDS), "the test never released the run"
        msg = "database busy before the run began"
        raise RuntimeError(msg)


class TestCoalescing:
    def test_two_concurrent_requests_share_one_run(self, engine):
        hold = threading.Event()
        assessor = StoringAssessor(engine, hold=hold)
        service, _ = build(engine, assessor)
        barrier = threading.Barrier(2)

        def press():
            barrier.wait(JOIN_SECONDS)
            return service.request()

        first, first_result = in_thread(press)
        second, second_result = in_thread(press)
        assert assessor.started.wait(JOIN_SECONDS)
        first.join(JOIN_SECONDS)
        second.join(JOIN_SECONDS)
        tickets = [first_result[0], second_result[0]]
        hold.set()
        assert tickets[0].run_id == tickets[1].run_id
        assert sorted(t.coalesced for t in tickets) == [False, True]
        progress = finished(service, tickets[0].run_id)
        assert assessor.run_ids == [tickets[0].run_id]
        assert progress.status is RunStatus.CLEAR
        service.stop()

    def test_a_request_while_a_run_is_queued_gets_the_queued_id(self, engine):
        assessor = StoringAssessor(engine)
        service, coordinator = build(engine, assessor)
        coordinator.lock.acquire()
        first = service.request()
        second = service.request()
        coordinator.lock.release()
        assert (second.run_id, second.coalesced) == (first.run_id, True)
        finished(service, first.run_id)
        assert assessor.run_ids == [first.run_id]
        service.stop()

    def test_a_request_after_the_run_finished_starts_a_new_one(self, engine):
        service, _ = build(engine, StoringAssessor(engine))
        first = service.request()
        finished(service, first.run_id)
        second = service.request()
        finished(service, second.run_id)
        assert second.run_id != first.run_id
        assert not second.coalesced
        service.stop()

    def test_a_request_joins_a_scheduled_run_that_is_executing(self, engine):
        assessor = StoringAssessor(engine)
        service, coordinator = build(engine, assessor)
        with coordinator.lock, coordinator.tracking("scheduled-run"):
            ticket = service.request()
        assert (ticket.run_id, ticket.coalesced) == ("scheduled-run", True)
        assert assessor.run_ids == []

    def test_a_run_that_waits_for_the_lock_reports_queued_then_runs(self, engine):
        assessor = StoringAssessor(engine)
        service, coordinator = build(engine, assessor)
        coordinator.lock.acquire()
        ticket = service.request()
        progress = service.progress(ticket.run_id)
        assert progress is not None
        assert (progress.state, progress.status) == ("queued", None)
        assert assessor.run_ids == []
        coordinator.lock.release()
        assert finished(service, ticket.run_id).status is RunStatus.CLEAR
        assert assessor.run_ids == [ticket.run_id]
        assert assessor.triggers == ["on_demand"]
        service.stop()

    def test_an_unknown_run_is_not_found(self, engine):
        service, _ = build(engine, StoringAssessor(engine))
        assert service.progress("00000000-0000-4000-8000-000000000000") is None


class TestSharedLock:
    """One lock covers scheduled and on-demand runs, scheduler on or off."""

    scheduler = staticmethod(make_scheduler)

    def test_the_scheduler_uses_the_coordinator_lock(self, engine):
        coordinator = PipelineCoordinator()
        scheduler = self.scheduler(engine, coordinator, StoringAssessor(engine))
        assert scheduler.pipeline_lock is coordinator.lock

    def test_a_scan_executing_blocks_the_on_demand_run_and_it_joins_it(self, engine):
        hold = threading.Event()
        assessor = StoringAssessor(engine, hold=hold)
        service, coordinator = build(engine, assessor)
        scheduler = self.scheduler(engine, coordinator, assessor)
        scan, scan_result = in_thread(scheduler.run_scan)
        assert assessor.started.wait(JOIN_SECONDS)
        ticket = service.request()
        hold.set()
        scan.join(JOIN_SECONDS)
        assert ticket.coalesced
        assert ticket.run_id == assessor.run_ids[0]
        assert [r.outcome for r in scan_result] == ["ok"]
        assert assessor.max_running == 1
        assert assessor.run_ids == [ticket.run_id]
        service.stop()

    def test_a_scan_collecting_queues_the_run_behind_it(self, engine):
        entered, release = threading.Event(), threading.Event()
        assessor = StoringAssessor(engine)
        service, coordinator = build(engine, assessor)
        scheduler = self.scheduler(
            engine,
            coordinator,
            assessor,
            on_run=lambda: (entered.set(), release.wait(JOIN_SECONDS)),
        )
        scan, _ = in_thread(scheduler.run_scan)
        assert entered.wait(JOIN_SECONDS)
        ticket = service.request()
        assert not ticket.coalesced
        assert assessor.run_ids == []
        release.set()
        scan.join(JOIN_SECONDS)
        assert finished(service, ticket.run_id).status is RunStatus.CLEAR
        assert len(assessor.run_ids) == 2
        assert assessor.run_ids[1] == ticket.run_id
        assert assessor.max_running == 1
        service.stop()

    def test_a_scan_that_finds_an_on_demand_run_executing_is_skipped(self, engine):
        hold = threading.Event()
        assessor = StoringAssessor(engine, hold=hold)
        service, coordinator = build(engine, assessor)
        scheduler = self.scheduler(engine, coordinator, assessor)
        ticket = service.request()
        assert assessor.started.wait(JOIN_SECONDS)
        assert scheduler.run_scan().outcome == "skipped_busy"
        hold.set()
        finished(service, ticket.run_id)
        assert scheduler.stats()[SCAN_JOB].skipped == 1
        assert assessor.run_ids == [ticket.run_id]
        service.stop()


class TestFailures:
    def test_a_run_that_raises_before_its_row_is_failed_never_clear(self, engine):
        service, _ = build(engine, StoringAssessor(engine, fail_at="before_begin"))
        ticket = service.request()
        progress = finished(service, ticket.run_id)
        assert (progress.state, progress.status) == ("finished", RunStatus.FAILED)
        assert progress.counts is None
        service.stop()

    def test_a_run_that_dies_after_its_row_reads_as_failed(self, engine):
        service, _ = build(engine, StoringAssessor(engine, fail_at="after_begin"))
        ticket = service.request()
        progress = finished(service, ticket.run_id)
        assert (progress.state, progress.status) == ("finished", RunStatus.FAILED)
        service.stop()

    def test_a_failed_run_does_not_block_the_next_request(self, engine):
        service, coordinator = build(
            engine, StoringAssessor(engine, fail_at="before_begin")
        )
        first = service.request()
        finished(service, first.run_id)
        assert coordinator.lock.acquire(blocking=False), "the lock was left held"
        coordinator.lock.release()
        assert coordinator.active_run_id is None
        assert service.request().run_id != first.run_id
        service.stop()

    def test_a_factory_that_raises_fails_the_run(self, engine):
        def refuse():
            msg = "no route provider"
            raise RuntimeError(msg)

        service = OnDemandService(
            PipelineCoordinator(),
            refuse,
            FakeSchool(),
            AssessmentRepository(engine),
        )
        ticket = service.request()
        assert finished(service, ticket.run_id).status is RunStatus.FAILED
        service.stop()

    def test_a_run_that_never_gets_the_lock_fails(self, engine):
        assessor = StoringAssessor(engine)
        service, coordinator = build(
            engine, assessor, lock_wait=timedelta(milliseconds=50)
        )
        coordinator.lock.acquire()
        ticket = service.request()
        progress = finished(service, ticket.run_id)
        coordinator.lock.release()
        assert progress.status is RunStatus.FAILED
        assert assessor.run_ids == []
        service.stop()

    def test_the_real_service_without_a_school_snapshot_is_never_clear(self, engine):
        service = OnDemandService.from_settings(
            make_settings(), engine, PipelineCoordinator()
        )
        ticket = service.request()
        progress = finished(service, ticket.run_id)
        assert progress.state == "finished"
        assert progress.status is not RunStatus.CLEAR
        assert progress.status in {RunStatus.INCOMPLETE, RunStatus.FAILED}
        assert progress.trigger == "on_demand"
        service.stop()

    def test_a_provider_the_deployment_cannot_build_fails_the_run(self, engine):
        service = OnDemandService.from_settings(
            make_settings(route_provider="google_routes"),
            engine,
            PipelineCoordinator(),
        )
        ticket = service.request()
        assert finished(service, ticket.run_id).status is RunStatus.FAILED
        service.stop()

    def test_only_a_bounded_number_of_failures_is_remembered(self, engine):
        service, _ = build(engine, StoringAssessor(engine, fail_at="before_begin"))
        ids = []
        for _ in range(35):
            ticket = service.request()
            finished(service, ticket.run_id)
            ids.append(ticket.run_id)
        assert service.progress(ids[0]) is None
        assert service.progress(ids[-1]).status is RunStatus.FAILED
        service.stop()


class TestFailureMemory:
    """A run id that was handed out resolves even when the run left no row."""

    def test_a_joined_scheduled_run_that_fails_before_its_row_is_failed(self, engine):
        assessor = RaisesWhenReleased()
        service, coordinator = build(engine, assessor)
        scheduler = make_scheduler(engine, coordinator, assessor)
        scan, scan_result = in_thread(scheduler.run_scan)
        assert assessor.started.wait(JOIN_SECONDS)
        ticket = service.request()
        assert ticket.coalesced
        assert ticket.run_id == coordinator.active_run_id
        assessor.release.set()
        scan.join(JOIN_SECONDS)
        assert [r.outcome for r in scan_result] == ["unavailable"]
        assert AssessmentRepository(engine).get_run(ticket.run_id) is None
        progress = service.progress(ticket.run_id)
        assert progress is not None, "a handed-out run id must always resolve"
        assert (progress.state, progress.status) == ("finished", RunStatus.FAILED)
        service.stop()

    def test_a_tracked_run_that_raises_is_remembered_and_the_error_passes_on(
        self, engine
    ):
        coordinator = PipelineCoordinator()
        assessor = RaisesWhenReleased()
        assessor.release.set()
        with coordinator.lock, pytest.raises(RuntimeError):
            coordinator.tracked(assessor).run("daily")
        assert coordinator.active_run_id is None
        assert assessor.run_id is not None
        assert coordinator.has_failed(assessor.run_id)

    def test_a_tracked_run_that_succeeds_is_not_remembered_as_failed(self, engine):
        coordinator = PipelineCoordinator()
        assessor = StoringAssessor(engine)
        with coordinator.lock:
            outcome = coordinator.tracked(assessor).run("daily")
        assert not coordinator.has_failed(outcome.run_id)

    def test_the_memory_is_bounded_and_forgets_the_oldest_first(self):
        coordinator = PipelineCoordinator()
        for number in range(REMEMBERED_FAILURES + 1):
            coordinator.remember_failed(f"run-{number}")
        assert not coordinator.has_failed("run-0")
        assert coordinator.has_failed("run-1")
        assert coordinator.has_failed(f"run-{REMEMBERED_FAILURES}")

    def test_remembering_a_run_again_keeps_it_the_newest(self):
        coordinator = PipelineCoordinator()
        coordinator.remember_failed("first")
        for number in range(REMEMBERED_FAILURES - 1):
            coordinator.remember_failed(f"run-{number}")
        coordinator.remember_failed("first")
        coordinator.remember_failed("last")
        assert coordinator.has_failed("first")


class TestProgressBranches:
    """The answers for runs that no in-memory mark describes."""

    def seed_unfinished_row(self, engine, run_id):
        now = datetime.now(UTC)
        AssessmentRepository(engine).begin_run(
            "daily", now, now, now, rules_version="test", run_id=run_id
        )

    def test_an_unfinished_row_nothing_is_executing_is_a_dead_run(self, engine):
        service, _ = build(engine, StoringAssessor(engine))
        self.seed_unfinished_row(engine, "died-before-a-restart")
        progress = service.progress("died-before-a-restart")
        assert progress is not None
        assert (progress.state, progress.status) == ("finished", RunStatus.FAILED)
        assert progress.trigger == "daily"
        assert progress.counts is None
        assert progress.started_at is not None
        assert progress.finished_at is None

    def test_an_unfinished_row_that_is_executing_reads_as_running(self, engine):
        service, coordinator = build(engine, StoringAssessor(engine))
        self.seed_unfinished_row(engine, "executing")
        with coordinator.lock, coordinator.tracking("executing"):
            progress = service.progress("executing")
        assert progress is not None
        assert (progress.state, progress.status) == ("running", None)
        assert progress.trigger == "daily"

    def test_a_run_tracked_before_its_row_exists_reads_as_running(self, engine):
        service, coordinator = build(engine, StoringAssessor(engine))
        with coordinator.lock, coordinator.tracking("no-row-yet"):
            progress = service.progress("no-row-yet")
        assert progress is not None
        assert (progress.state, progress.trigger) == ("running", None)
        assert progress.started_at is None

    def test_a_run_with_a_finish_time_is_never_running_while_still_marked(self, engine):
        assessor = StoringAssessor(engine)
        service, coordinator = build(engine, assessor)
        with coordinator.lock:
            outcome = assessor.run("daily")
            with coordinator.tracking(outcome.run_id):
                progress = service.progress(outcome.run_id)
        assert progress is not None
        assert (progress.state, progress.status) == ("finished", RunStatus.CLEAR)


class TestSettling:
    """No stale answer in the moment a run has ended but its lock is not free."""

    def settle(self, engine, assessor):
        probe = ProbeLock()
        service, coordinator = build(engine, assessor)
        coordinator.lock = probe
        seen: dict[str, object] = {}
        probe.acquire()
        ticket = service.request()
        assert probe.waiting.wait(JOIN_SECONDS)

        def look():
            seen["progress"] = service.progress(ticket.run_id)
            seen["next"] = service.request()

        probe.on_release("on-demand-run", look)
        probe.release()
        assert wait_until(lambda: "next" in seen), "the run never ended"
        service.stop()
        return ticket, seen

    def test_a_finished_run_is_not_queued_and_is_not_joined(self, engine):
        ticket, seen = self.settle(engine, StoringAssessor(engine))
        progress = seen["progress"]
        assert (progress.state, progress.status) == ("finished", RunStatus.CLEAR)
        assert seen["next"].run_id != ticket.run_id
        assert not seen["next"].coalesced

    def test_a_failed_run_is_not_queued_and_is_not_joined(self, engine):
        ticket, seen = self.settle(
            engine, StoringAssessor(engine, fail_at="before_begin")
        )
        progress = seen["progress"]
        assert (progress.state, progress.status) == ("finished", RunStatus.FAILED)
        assert seen["next"].run_id != ticket.run_id
        assert not seen["next"].coalesced


class TestInFlight:
    def test_idle_means_nothing_in_flight(self, engine):
        service, _ = build(engine, StoringAssessor(engine))
        assert not service.in_flight()

    def test_a_scheduled_pipeline_that_is_still_collecting_is_in_flight(self, engine):
        entered, release = threading.Event(), threading.Event()
        assessor = StoringAssessor(engine)
        service, coordinator = build(engine, assessor)
        scheduler = make_scheduler(
            engine,
            coordinator,
            assessor,
            on_run=lambda: (entered.set(), release.wait(JOIN_SECONDS)),
        )
        scan, _ = in_thread(scheduler.run_scan)
        assert entered.wait(JOIN_SECONDS)
        assert coordinator.active_run_id is None
        assert service.in_flight()
        release.set()
        scan.join(JOIN_SECONDS)
        assert not service.in_flight()

    def test_a_queued_run_is_in_flight_until_it_ends(self, engine):
        service, coordinator = build(engine, StoringAssessor(engine))
        coordinator.lock.acquire()
        ticket = service.request()
        assert service.in_flight()
        coordinator.lock.release()
        finished(service, ticket.run_id)
        assert not service.in_flight()
        service.stop()


class TestShutdown:
    def test_stop_waits_for_the_run_that_is_executing(self, engine):
        hold = threading.Event()
        assessor = StoringAssessor(engine, hold=hold)
        service, coordinator = build(engine, assessor)
        ticket = service.request()
        assert assessor.started.wait(JOIN_SECONDS)
        stopper, _ = in_thread(service.stop)
        stopper.join(0.2)
        assert stopper.is_alive(), "stop returned while the run was executing"
        hold.set()
        stopper.join(JOIN_SECONDS)
        assert not stopper.is_alive()
        progress = service.progress(ticket.run_id)
        assert progress is not None
        assert progress.status is RunStatus.CLEAR
        assert not coordinator.lock.locked()

    def test_stop_abandons_a_run_that_is_still_queued(self, engine):
        assessor = StoringAssessor(engine)
        service, coordinator = build(engine, assessor)
        coordinator.lock.acquire()
        ticket = service.request()
        service.stop()
        coordinator.lock.release()
        assert assessor.run_ids == []
        progress = service.progress(ticket.run_id)
        assert progress is not None
        assert progress.status is RunStatus.FAILED

    def test_a_queued_run_never_starts_once_the_stop_has_begun(self, engine):
        probe = ProbeLock()
        assessor = StoringAssessor(engine)
        service, coordinator = build(engine, assessor)
        coordinator.lock = probe
        probe.acquire()
        ticket = service.request()
        assert probe.waiting.wait(JOIN_SECONDS), "the run never began to wait"
        service.begin_stop()
        probe.release()
        assert wait_until(lambda: not service.in_flight()), "the run never ended"
        assert assessor.run_ids == []
        progress = service.progress(ticket.run_id)
        assert progress is not None
        assert progress.status is RunStatus.FAILED
        service.stop()

    def test_begin_stop_refuses_new_requests_but_does_not_wait(self, engine):
        hold = threading.Event()
        assessor = StoringAssessor(engine, hold=hold)
        service, coordinator = build(engine, assessor)
        ticket = service.request()
        assert assessor.started.wait(JOIN_SECONDS)
        stopper, _ = in_thread(service.begin_stop)
        stopper.join(JOIN_SECONDS)
        assert not stopper.is_alive(), "begin_stop waited for the run"
        with pytest.raises(OnDemandStoppedError):
            service.request()
        hold.set()
        service.stop()
        progress = service.progress(ticket.run_id)
        assert progress is not None
        assert progress.status is RunStatus.CLEAR
        assert not coordinator.lock.locked()

    def test_no_request_is_taken_after_stop(self, engine):
        service, _ = build(engine, StoringAssessor(engine))
        service.stop()
        service.stop()
        with pytest.raises(OnDemandStoppedError):
            service.request()
