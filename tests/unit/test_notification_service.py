"""Notification service: dispatch, retries, claims and audit (ADR 0008, T-svc-*).

The service runs against a real SQLite database at the schema head, the real
``AppriseClient`` over ``httpx.MockTransport`` and a settable clock. Nothing
here uses a real address, name or calendar identifier.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import httpx
import pytest
from fl_notification_service import (
    EMPTY_RIDES,
    FAR,
    MORNING,
    NIGHT,
    NIGHT_RIDE,
    NIGHT_RIDE_KEY,
    NIGHT_RIDES,
    NOW,
    RIDE_KEY,
    RIDES,
    Clock,
    Posts,
    audit_of,
    captured_events,
    finding,
    go,
    make_service,
    outcome,
    result_of,
    seeded,
    texts,
)
from fl_notification_state import all_rows, get_row, get_watermark, make_engine
from fl_service import make_settings
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from family_logistics.adapters.apprise.client import (
    AppriseClient,
    NotificationTagError,
)
from family_logistics.core.exceptions import DatabaseError
from family_logistics.db import schema
from family_logistics.db.notifications import NotificationStateBusyError
from family_logistics.domain.engine import failed_result
from family_logistics.domain.lifecycle import Lifecycle
from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.notification_policy import (
    CLAIM_TTL,
    CLEAR_PHRASES,
    SEND_DEADLINE,
    URGENT_RETRY_DELAY,
)
from family_logistics.services import notifications as service_module
from family_logistics.services.notifications import (
    DispatchReport,
    NotificationService,
)

pytestmark = pytest.mark.unit

SIX_TAGS = {
    "fl-report",
    "fl-status",
    "fl-warning",
    "fl-urgent",
    "fl-ops",
    "fl-shadow",
}


@pytest.fixture
def engine(tmp_path):
    built = make_engine(tmp_path)
    seeded(built)
    yield built
    built.dispose()


def key_of(found) -> str:
    return f"issue:{found.issue_id}"


def warning(index: int = 0):
    """A warning on the ride (imminent at NOW, but a warning is only a notice)."""
    return finding("CONF-008", (RIDE_KEY, f"k{index}"), Severity.WARNING)


def audit_count(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            select(func.count()).select_from(schema.audit_log)
        ).scalar_one()


class TestNeverClear:
    def test_failed_run_sends_could_not_verify_never_clear(self, engine):
        """T-svc-1."""
        service, posts, _clock = make_service(engine, mode="production")
        failed = failed_result(NOW, "boom", (finding(),))
        service.dispatch(outcome(failed, "run-1", "daily", RIDES))
        assert "fl-urgent" in posts.tags
        assert not {"fl-status", "fl-report"} & set(posts.tags)
        assert not [p for p in CLEAR_PHRASES if p in texts(posts)]

    def test_failed_run_without_an_imminent_ride_is_a_warning(self, engine):
        service, posts, _clock = make_service(
            engine, mode="production", clock=Clock(FAR)
        )
        failed = failed_result(FAR, "boom", ())
        service.dispatch(outcome(failed, "run-1", "daily", EMPTY_RIDES))
        assert sorted(posts.tags) == ["fl-ops", "fl-warning"]
        assert "could not be verified" in texts(posts)

    def test_incomplete_daily_status_is_labelled(self, engine):
        """T-svc-2."""
        service, posts, clock = make_service(engine, mode="production")
        go(service, clock, NOW, status=RunStatus.INCOMPLETE, trigger="daily")
        status = [p for p in posts.payloads if p["tag"] == "fl-status"]
        assert len(status) == 1
        assert "could not fully verify" in status[0]["body"]
        assert not [p for p in CLEAR_PHRASES if p in texts(posts)]

    @pytest.mark.parametrize("mode", ["shadow", "production"])
    @pytest.mark.parametrize("trigger", ["daily", "weekly", "on_demand", "incremental"])
    @pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.INCOMPLETE])
    def test_failed_and_incomplete_runs_never_produce_clear_wording(
        self, engine, status, trigger, mode
    ):
        service, posts, _clock = make_service(engine, mode=mode)
        if status is RunStatus.FAILED:
            result = failed_result(NOW, "boom", (finding(),))
        else:
            result = result_of(finding(), status=status)
        service.dispatch(outcome(result, "run-1", trigger))
        assert posts.payloads
        assert not [p for p in CLEAR_PHRASES if p in texts(posts)]


class TestQuietHours:
    def test_imminent_critical_bypasses_quiet_hours(self, engine):
        """T-svc-3."""
        service, posts, clock = make_service(engine, mode="production")
        urgent = finding(keys=(NIGHT_RIDE_KEY, "route"))
        report = go(service, clock, NIGHT, urgent, rides=NIGHT_RIDES)
        assert posts.tags == ["fl-urgent"]
        assert report.sent == 1
        assert report.held == 0

    def test_ride_in_progress_is_urgent_at_night(self, engine):
        """T-svc-3b: five minutes into the window, during quiet hours."""
        service, posts, clock = make_service(engine, mode="production")
        urgent = finding(keys=(NIGHT_RIDE_KEY, "route"))
        started = NIGHT_RIDE.start + timedelta(minutes=5)
        go(service, clock, started, urgent, rides=NIGHT_RIDES)
        assert posts.tags == ["fl-urgent"]

    def test_held_notice_sent_after_quiet_hours(self, engine):
        """T-svc-4: a held notice is not claimed, then posts once at 06:10."""
        service, posts, clock = make_service(engine, mode="production")
        held = warning()
        first = go(service, clock, NIGHT, held, run_id="run-1")
        assert posts.payloads == []
        assert first.held == 1
        assert all_rows(engine) == []
        second = go(
            service, clock, MORNING, held, run_id="run-2", lifecycle=Lifecycle.ONGOING
        )
        assert posts.tags == ["fl-warning"]
        assert second.sent == 1
        row = get_row(engine, key_of(held), "production")
        assert row.delivered is True

    def test_a_status_in_quiet_hours_is_dropped_not_held(self, engine):
        service, posts, clock = make_service(engine, mode="production")
        report = go(
            service, clock, NIGHT, status=RunStatus.CLEAR, trigger="daily", rides=None
        )
        assert posts.payloads == []
        assert report.dropped == 1
        assert report.held == 0


class TestIdempotence:
    def test_same_run_dispatched_twice_sends_once(self, engine):
        """T-svc-5."""
        service, posts, clock = make_service(engine)
        urgent = finding()
        first = go(service, clock, NOW, urgent)
        second = go(service, clock, NOW, urgent)
        assert len(posts.payloads) == 1
        assert (first.sent, first.duplicates) == (1, 0)
        assert (second.sent, second.duplicates) == (0, 1)

    def test_concurrent_double_dispatch_is_idempotent(self, engine):
        """T-svc-6: two services race for one outcome; one payload per subject."""
        pair = [make_service(engine) for _ in range(2)]
        barrier = threading.Barrier(2)
        reports: list[DispatchReport] = []
        urgent = finding()

        def race(index: int) -> None:
            service, _, clock = pair[index]
            barrier.wait()
            reports.append(go(service, clock, NOW, urgent))

        threads = [threading.Thread(target=race, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(len(posts.payloads) for _, posts, _ in pair) == 1
        assert sorted(r.sent for r in reports) == [0, 1]
        assert get_row(engine, key_of(urgent)).delivered is True


class TestDeliveryFailure:
    def test_failed_delivery_retried_next_dispatch(self, engine):
        """T-svc-7: a 500 then a 200 on the next dispatch delivers once."""
        service, posts, clock = make_service(engine, posts=Posts([500]))
        held = warning()
        first = go(service, clock, NOW, held)
        assert (first.sent, first.failed, first.urgent_failed) == (0, 1, 0)
        assert get_row(engine, key_of(held)).delivered is False
        second = go(service, clock, NOW, held)
        assert (second.sent, second.failed) == (1, 0)
        assert len(posts.payloads) == 2
        row = get_row(engine, key_of(held))
        assert (row.delivered, row.send_count) == (True, 1)

    def test_a_non_urgent_failure_is_not_retried_in_the_dispatch(self, engine):
        service, posts, clock = make_service(engine, posts=Posts([500]))
        go(service, clock, NOW, warning())
        assert len(posts.payloads) == 1
        assert clock.sleeps == []

    def test_urgent_retried_once_in_dispatch(self, engine):
        """T-svc-7b: a 500 then a 200 delivers in one dispatch."""
        service, posts, clock = make_service(engine, posts=Posts([500, 200]))
        report = go(service, clock, NOW, finding())
        assert (report.sent, report.failed, report.urgent_failed) == (1, 0, 0)
        assert len(posts.payloads) == 2
        assert clock.sleeps == [URGENT_RETRY_DELAY.total_seconds()]

    def test_urgent_retry_then_release_leaves_the_push_owed(self, engine):
        """T-svc-7b: two 500s count as urgent_failed; the claim is released."""
        urgent = finding()
        service, posts, clock = make_service(engine, posts=Posts([500, 500]))
        report = go(service, clock, NOW, urgent)
        assert (report.sent, report.failed, report.urgent_failed) == (0, 1, 1)
        assert len(posts.payloads) == 2
        row = get_row(engine, key_of(urgent))
        assert (row.delivered, row.claim_token) == (False, None)
        again = go(service, clock, NOW, urgent)
        assert (again.sent, len(posts.payloads)) == (1, 3)

    def test_failed_run_retries_undelivered_urgent(self, engine):
        """T-svc-8: urgent failed in run 1; run 2 failed; the urgent is retried."""
        urgent = finding()
        service, _posts, clock = make_service(
            engine, mode="production", posts=Posts([500, 500])
        )
        go(service, clock, NOW, urgent)
        assert get_row(engine, key_of(urgent), "production").delivered is False
        later = NOW + timedelta(minutes=10)
        clock.now = later
        failed = failed_result(later, "boom", (urgent,))
        service.dispatch(outcome(failed, "run-2", "incremental"))
        assert get_row(engine, key_of(urgent), "production").delivered is True

    @pytest.mark.parametrize(
        "script",
        [
            [httpx.ConnectError("down")],
            [httpx.ReadTimeout("slow")],
            [500],
            [404],
            [424],
        ],
    )
    def test_dispatch_never_raises_on_delivery_failure(self, engine, script):
        """T-svc-17."""
        service, _posts, clock = make_service(engine, posts=Posts(script))
        report = go(service, clock, NOW, warning())
        assert (report.sent, report.failed) == (0, 1)

    def test_one_failed_send_does_not_stop_the_others(self, engine):
        service, posts, clock = make_service(engine, posts=Posts([500]))
        report = go(
            service, clock, NOW, warning(), status=RunStatus.INCOMPLETE, trigger="daily"
        )
        assert report.failed == 1
        assert report.sent == len(posts.payloads) - 1


class TestUnavailableRun:
    def test_dispatch_unavailable_tells_the_family(self, engine):
        """T-svc-9."""
        service, posts, clock = make_service(engine, mode="production")
        first = service.dispatch_unavailable("daily")
        assert sorted(posts.tags) == ["fl-ops", "fl-urgent"]
        assert first.run_id.startswith("unavailable-")
        assert len(first.run_id) <= 36
        clock.advance(timedelta(minutes=30))
        second = service.dispatch_unavailable("daily")
        assert len(posts.payloads) == 2
        assert second.sent == 0
        assert not [p for p in CLEAR_PHRASES if p in texts(posts)]


class TestTags:
    def test_shadow_mode_routes_everything_to_shadow_tag(self, engine):
        """T-svc-10: urgent, notice, status, report and ops all go to fl-shadow."""
        service, posts, clock = make_service(engine)
        go(
            service,
            clock,
            NOW,
            finding(),
            warning(),
            status=RunStatus.INCOMPLETE,
            trigger="daily",
        )
        go(
            service,
            clock,
            NOW + timedelta(minutes=1),
            run_id="run-2",
            status=RunStatus.INCOMPLETE,
            trigger="weekly",
        )
        assert len(posts.payloads) >= 4
        assert set(posts.tags) == {"fl-shadow"}
        assert all(p["title"].startswith("[shadow]") for p in posts.payloads)

    def test_tags_never_outside_the_six(self, engine):
        """T-svc-11: production uses each kind's own tag, all within the six."""
        service, posts, clock = make_service(engine, mode="production")
        go(
            service,
            clock,
            NOW,
            finding(),
            warning(),
            status=RunStatus.INCOMPLETE,
            trigger="daily",
        )
        go(
            service,
            clock,
            NOW + timedelta(minutes=1),
            run_id="run-2",
            status=RunStatus.INCOMPLETE,
            trigger="weekly",
        )
        assert set(posts.tags) == SIX_TAGS - {"fl-shadow"}

    def test_a_tag_the_client_does_not_own_raises_and_releases_the_claim(self, engine):
        """The client refuses a tag it does not own; unsent claims are released."""
        _, _, clock = make_service(engine)
        built = make_settings(
            notification_mode="shadow", apprise_url="http://x.invalid"
        )
        client = AppriseClient(
            "http://apprise.example.invalid",
            "family-logistics",
            frozenset({"fl-ops"}),
            transport=httpx.MockTransport(Posts()),
        )
        service = NotificationService(
            engine, built, client, clock=clock, sleep=clock.sleep
        )
        urgent = finding()
        with pytest.raises(NotificationTagError):
            go(service, clock, NOW, urgent)
        row = get_row(engine, key_of(urgent))
        assert (row.delivered, row.claim_token) == (False, None)


class TestEpisodes:
    def test_resolved_closes_state_and_recurrence_notifies(self, engine):
        """T-svc-12: resolve, then regress after the cooldown."""
        service, posts, clock = make_service(engine, mode="production")
        found = finding()
        hour = timedelta(hours=1)
        go(service, clock, FAR, found, run_id="run-1")
        assert len(posts.payloads) == 1
        go(service, clock, FAR + hour, run_id="run-2")
        assert get_row(engine, key_of(found), "production").is_open is False
        go(service, clock, FAR + 5 * hour, found, run_id="run-3")
        assert len(posts.payloads) == 2

    def test_ongoing_not_renotified(self, engine):
        """T-svc-13."""
        service, posts, clock = make_service(engine)
        found = finding()
        go(service, clock, NOW, found, run_id="run-1")
        for index, minutes in enumerate((10, 20), start=2):
            go(
                service,
                clock,
                NOW + timedelta(minutes=minutes),
                found,
                run_id=f"run-{index}",
                lifecycle=Lifecycle.ONGOING,
            )
        assert len(posts.payloads) == 1

    def test_urgent_then_notice_not_duplicated(self, engine):
        """T-svc-14: an urgent delivered while imminent is not re-sent later."""
        service, posts, clock = make_service(engine, mode="production")
        found = finding()
        go(service, clock, NOW, found, run_id="run-1")
        assert posts.tags == ["fl-urgent"]
        go(
            service,
            clock,
            NOW + timedelta(hours=3),
            found,
            run_id="run-2",
            lifecycle=Lifecycle.ONGOING,
        )
        assert posts.tags == ["fl-urgent"]

    def test_an_escalation_is_sent_again_and_worded_as_one(self, engine):
        service, posts, clock = make_service(engine, mode="production")
        go(service, clock, NOW, warning(), run_id="run-1")
        worse = finding("CONF-008", (RIDE_KEY, "k0"), Severity.CRITICAL)
        go(
            service,
            clock,
            NOW + timedelta(minutes=10),
            worse,
            run_id="run-2",
            lifecycle=Lifecycle.ONGOING,
        )
        assert posts.tags == ["fl-warning", "fl-urgent"]
        assert "now critical" in posts.bodies[-1]

    def test_regression_after_missed_dispatch_notifies(self, engine):
        """T-svc-25: the resolve run was never dispatched; the regression pushes."""
        service, posts, clock = make_service(engine, mode="production")
        found = finding()
        go(service, clock, FAR, found, run_id="run-1")
        later = FAR + timedelta(hours=5)
        go(service, clock, later, found, run_id="run-3", lifecycle=Lifecycle.REGRESSED)
        assert len(posts.payloads) == 2

    def test_incomplete_episode_one_ops_push(self, engine):
        """T-svc-23: three incomplete runs raise one ops push; a clear run closes it."""
        service, posts, clock = make_service(engine, mode="production")
        for index in range(3):
            go(
                service,
                clock,
                NOW + timedelta(minutes=10 * index),
                run_id=f"run-{index}",
                status=RunStatus.INCOMPLETE,
            )
        assert posts.tags == ["fl-ops"]
        go(
            service,
            clock,
            NOW + timedelta(minutes=40),
            run_id="run-clear",
            status=RunStatus.CLEAR,
        )
        row = get_row(engine, "ops:run_incomplete", "production")
        assert row.is_open is False


class TestChunking:
    def test_notices_are_chunked(self, engine):
        """T-svc-22: 20 owed notices make 3 payloads (8, 8, 4)."""
        service, posts, clock = make_service(engine, mode="production")
        found = tuple(warning(index) for index in range(20))
        report = go(service, clock, NOW, *found)
        assert report.sent == 3
        assert sorted(p["title"] for p in posts.payloads) == [
            "Ride check: 4 items need attention",
            "Ride check: 8 items need attention",
            "Ride check: 8 items need attention",
        ]
        rows = [get_row(engine, key_of(f), "production") for f in found]
        assert all(row.delivered and row.send_count == 1 for row in rows)
        assert "more." not in texts(posts)


class TestStaleOutcomes:
    def test_older_clear_after_newer_failed_sends_nothing(self, engine):
        """T-svc-21: the newer failed run was dispatched first."""
        service, posts, clock = make_service(engine, mode="production")
        newer = NOW + timedelta(minutes=10)
        clock.now = newer
        service.dispatch(outcome(failed_result(newer, "boom", ()), "run-2", "daily"))
        sent = len(posts.payloads)
        clock.now = newer
        older = result_of(status=RunStatus.CLEAR, evaluated_at=NOW)
        report = service.dispatch(outcome(older, "run-1", "daily"))
        assert report.stale is True
        assert (report.sent, len(posts.payloads)) == (0, sent)
        assert not [p for p in CLEAR_PHRASES if p in texts(posts)]

    def test_a_stale_outcome_is_logged_with_the_watermark_run(
        self, engine, monkeypatch
    ):
        service, _posts, clock = make_service(engine)
        go(service, clock, NOW + timedelta(minutes=10), run_id="run-2")
        with captured_events(monkeypatch) as events:
            report = go(service, clock, NOW, finding(), run_id="run-1")
        stale = [e for e in events if e["event"] == "notification.stale_outcome"]
        assert report.stale is True
        assert [(e["run_id"], e["watermark_run_id"]) for e in stale] == [
            ("run-1", "run-2")
        ]

    def test_an_unreadable_watermark_does_not_hide_the_stale_outcome(
        self, engine, monkeypatch
    ):
        service, _posts, clock = make_service(engine)
        go(service, clock, NOW + timedelta(minutes=10), run_id="run-2")

        def unreadable():
            raise OperationalError("select", {}, Exception("locked"))

        monkeypatch.setattr(service._repo, "watermark_run_id", unreadable)
        with captured_events(monkeypatch) as events:
            report = go(service, clock, NOW, finding(), run_id="run-1")
        stale = [e for e in events if e["event"] == "notification.stale_outcome"]
        assert report.stale is True
        assert [e["watermark_run_id"] for e in stale] == [None]

    def test_a_late_status_is_logged_as_suppressed(self, engine, monkeypatch):
        service, _posts, clock = make_service(engine, mode="production")
        clock.now = NOW + timedelta(minutes=61)
        result = result_of(status=RunStatus.CLEAR, evaluated_at=NOW)
        with captured_events(monkeypatch) as events:
            service.dispatch(outcome(result, "run-1", "daily"))
        reasons = [
            e["reason"] for e in events if e["event"] == "notification.suppressed"
        ]
        assert reasons == ["status_stale"]

    def test_late_status_refused(self, engine):
        """T-svc-24: an outcome older than one scan interval sends no status."""
        service, posts, clock = make_service(engine, mode="production")
        late = NOW + timedelta(minutes=61)
        clock.now = late
        report = service.dispatch(
            outcome(
                result_of(status=RunStatus.CLEAR, evaluated_at=NOW), "run-1", "daily"
            )
        )
        assert posts.payloads == []
        assert report.status_stale is True


class TestCrashRecovery:
    def test_crash_after_send_resends_after_claim_ttl(self, engine, monkeypatch):
        """T-svc-16: no resend before CLAIM_TTL, one resend after."""
        urgent = finding()
        service, posts, clock = make_service(engine)

        def crash(*args, **kwargs):
            raise NotificationStateBusyError(
                "busy", operation="complete", table="notification_state"
            )

        monkeypatch.setattr(service._repo, "complete", crash)
        with pytest.raises(DatabaseError):
            go(service, clock, NOW, urgent)
        assert len(posts.payloads) == 1
        restarted, posts_after, _ = make_service(engine, clock=clock)
        soon = go(restarted, clock, NOW + CLAIM_TTL - timedelta(minutes=1), urgent)
        assert (soon.sent, soon.in_flight) == (0, 1)
        later = go(restarted, clock, NOW + CLAIM_TTL + timedelta(minutes=1), urgent)
        assert later.sent == 1
        assert len(posts_after.payloads) == 1
        assert get_row(engine, key_of(urgent)).delivered is True

    def test_a_failed_release_raises_after_the_other_sends(self, engine, monkeypatch):
        service, posts, clock = make_service(engine, posts=Posts([500]))

        def broken(*args, **kwargs):
            raise OperationalError("update", {}, Exception("locked"))

        monkeypatch.setattr(service._repo, "complete", broken)
        with pytest.raises(DatabaseError, match="could not be written"):
            go(service, clock, NOW, warning(0), warning(1))
        assert len(posts.payloads) == 1

    def test_the_send_deadline_releases_what_is_unsent(self, engine):
        clock = Clock()
        posts = Posts(on_post=lambda: clock.advance(SEND_DEADLINE))
        service, _, _ = make_service(engine, posts=posts, clock=clock)
        urgent = finding()
        held = warning()
        report = go(service, clock, NOW, urgent, held)
        assert (report.sent, report.released) == (1, 1)
        assert len(posts.payloads) == 1
        row = get_row(engine, key_of(held))
        assert (row.delivered, row.claim_token) == (False, None)

    def test_a_failed_release_leaves_the_claim_to_expire_and_raises(
        self, engine, monkeypatch
    ):
        def refuse(*args, **kwargs):
            raise ValueError("refused")

        def locked(*args, **kwargs):
            raise OperationalError("update", {}, Exception("locked"))

        monkeypatch.setattr(service_module, "render", refuse)
        service, posts, clock = make_service(engine)
        monkeypatch.setattr(service._repo, "release", locked)
        urgent = finding()
        with pytest.raises(DatabaseError, match="could not be written"):
            go(service, clock, NOW, urgent)
        assert posts.payloads == []
        assert get_row(engine, key_of(urgent)).claim_token is not None

    def test_a_claim_failure_raises_and_sends_nothing(self, engine, monkeypatch):
        service, posts, clock = make_service(engine)

        def busy(*args, **kwargs):
            raise NotificationStateBusyError(
                "busy", operation="claim", table="notification_state"
            )

        monkeypatch.setattr(service._repo, "claim", busy)
        with pytest.raises(DatabaseError, match="claim"):
            go(service, clock, NOW, finding())
        assert posts.payloads == []


class TestUnconfigured:
    def test_unconfigured_client_writes_nothing(self, engine, monkeypatch):
        """T-svc-18: shadow mode with no Apprise URL plans and writes nothing."""
        service, posts, clock = make_service(engine, configured=False)
        with captured_events(monkeypatch) as events:
            report = go(service, clock, NOW, finding())
        assert report.unconfigured is True
        assert report.sent == 0
        assert all_rows(engine) == []
        assert get_watermark(engine) is None
        names = [e["event"] for e in events]
        assert "notification.not_configured" in names
        assert posts.payloads == []


class TestAudit:
    def test_audit_per_delivered_push(self, engine):
        """T-svc-19: one row per delivered push; none for held or failed."""
        service, _posts, clock = make_service(engine, mode="production", audit=True)
        found = (warning(0), warning(1))
        go(service, clock, NOW, *found)
        repo = audit_of(service)
        assert [len(repo.entries_for(key_of(f))) for f in found] == [1, 1]
        assert audit_count(engine) == 2
        entry = repo.entries_for(key_of(found[0]))[0]
        assert (entry.actor, entry.action) == ("notifier", "notification_sent")
        assert entry.correlation_id == "run-1"

    def test_no_audit_for_held_or_failed_pushes(self, engine):
        service, posts, clock = make_service(
            engine, mode="production", audit=True, posts=Posts([500])
        )
        go(service, clock, NIGHT, warning(0), run_id="run-1")
        go(service, clock, MORNING, warning(0), run_id="run-2")
        assert len(posts.payloads) == 1
        assert audit_count(engine) == 0

    def test_no_audit_without_key(self, engine):
        """T-svc-20."""
        service, _posts, clock = make_service(engine, audit=False)
        report = go(service, clock, NOW, finding())
        assert report.sent == 1
        assert audit_count(engine) == 0

    def test_an_audit_failure_never_undoes_the_send(self, engine, monkeypatch):
        service, _posts, clock = make_service(engine, audit=True)
        repo = audit_of(service)

        def broken(**kwargs):
            raise DatabaseError("audit down", operation="insert", table="audit_log")

        monkeypatch.setattr(repo, "record", broken)
        urgent = finding()
        report = go(service, clock, NOW, urgent)
        assert report.sent == 1
        assert get_row(engine, key_of(urgent)).delivered is True


class TestDirectoryAndRenderFailure:
    def test_names_are_loaded_once_per_dispatch(self, engine, monkeypatch):
        calls: list[int] = []
        real = service_module.load_name_rows

        def counting(built):
            calls.append(1)
            return real(built)

        monkeypatch.setattr(service_module, "load_name_rows", counting)
        service, _posts, clock = make_service(engine)
        go(service, clock, NOW, finding(), warning(0), warning(1))
        assert len(calls) == 1
        go(service, clock, NOW, finding())
        assert len(calls) == 1

    def test_an_unreadable_directory_still_delivers_generic_text(
        self, engine, monkeypatch
    ):
        def broken(built):
            raise OperationalError("select", {}, Exception("locked"))

        monkeypatch.setattr(service_module, "load_name_rows", broken)
        service, posts, clock = make_service(engine)
        report = go(service, clock, NOW, finding())
        assert report.sent == 1
        assert "Sam" not in texts(posts)

    def test_render_failure_is_counted_and_the_claim_released(
        self, engine, monkeypatch
    ):
        def refuse(*args, **kwargs):
            raise ValueError("SENTINEL-RENDER-TEXT")

        monkeypatch.setattr(service_module, "render", refuse)
        service, posts, clock = make_service(engine)
        urgent = finding()
        with captured_events(monkeypatch) as events:
            report = go(service, clock, NOW, urgent)
        assert (report.sent, report.failed, report.urgent_failed) == (0, 1, 1)
        assert posts.payloads == []
        row = get_row(engine, key_of(urgent))
        assert (row.delivered, row.claim_token, row.failed_attempts) == (False, None, 0)
        failed = [e for e in events if e["event"] == "notification.render_failed"]
        assert [e["error"] for e in failed] == ["ValueError"]
        assert "SENTINEL-RENDER-TEXT" not in repr(events)
        monkeypatch.undo()
        retried = go(service, clock, NOW, urgent)
        assert retried.sent == 1


class TestReport:
    def test_the_report_names_the_run_and_mode(self, engine):
        service, _posts, clock = make_service(engine, mode="production")
        report = go(service, clock, NOW, finding(), run_id="run-9")
        assert (report.run_id, report.mode) == ("run-9", "production")
        assert report.unresolved == 0

    def test_unresolved_subjects_are_counted_and_logged_by_rule(
        self, engine, monkeypatch
    ):
        service, _posts, clock = make_service(engine)
        coverage = finding(
            "RIDE-005", ("person:sam", "day:2026-10-23", "from:a", "to:b")
        )
        with captured_events(monkeypatch) as events:
            report = go(service, clock, NOW, coverage, rides=EMPTY_RIDES)
        assert report.unresolved == 1
        assert report.sent == 1
        unresolved = [
            e for e in events if e["event"] == "notification.subject_unresolved"
        ]
        assert len(unresolved) == report.unresolved
        assert all(e["rule_id"] == "RIDE-005" for e in unresolved)

    def test_close_closes_the_client(self, engine):
        service, _posts, _clock = make_service(engine)
        service.close()
        unconfigured, _, _ = make_service(engine, configured=False)
        unconfigured.close()

    def test_from_settings_builds_a_service(self, engine):
        built = NotificationService.from_settings(
            engine, make_settings(apprise_url="http://apprise.example.invalid")
        )
        built.close()
        bare = NotificationService.from_settings(engine, make_settings())
        assert bare.dispatch_unavailable("daily").unconfigured is True
        bare.close()
