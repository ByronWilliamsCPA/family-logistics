"""On-demand runs: coalescing, the shared pipeline lock, failures and shutdown.

Threads rendezvous on events, so no outcome depends on timing. Everything is
fictional; the assessor is a fake that stores real run rows.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from fl_notification_service import Clock
from fl_on_demand import (
    JOIN_SECONDS,
    FakeCollector,
    FakeSchool,
    NoAps,
    Notifier,
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
from family_logistics.services.pipeline import PipelineCoordinator
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
    assert wait_until(lambda: not service.in_flight()), "the run never ended"
    progress = service.progress(run_id)
    assert progress is not None
    return progress


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

    def scheduler(self, engine, coordinator, assessor, on_run=None):
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

    def test_no_request_is_taken_after_stop(self, engine):
        service, _ = build(engine, StoringAssessor(engine))
        service.stop()
        service.stop()
        with pytest.raises(OnDemandStoppedError):
            service.request()
