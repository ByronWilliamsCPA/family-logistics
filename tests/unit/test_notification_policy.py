"""Notification policy: kinds, tags, quiet hours, planning and decisions (ADR 0008).

The policy is pure: ``now`` is always passed in, nothing reads a clock or the
database, and nothing is logged.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fl_factories import notes, trip
from fl_service import make_settings
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from family_logistics.domain.engine import AssessmentResult, failed_result
from family_logistics.domain.lifecycle import _RANK, Lifecycle, TrackedFinding
from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.notification_policy import (
    CLAIM_LOCK_RETRIES,
    CLAIM_TTL,
    CLEAR_PHRASES,
    MAX_ITEMS_PER_PUSH,
    RENOTIFY_COOLDOWN,
    SEND_DEADLINE,
    URGENT_RETRY_DELAY,
    Decision,
    NotificationPolicy,
    PlannedPush,
    PushKind,
    StateView,
    TagSet,
    decide,
    delivery_tag,
    finding_kind,
    in_quiet_hours,
    plan,
    plan_unavailable,
    production_tag,
)
from family_logistics.domain.notification_subjects import RideIndex, is_imminent
from family_logistics.domain.rules import make_finding
from family_logistics.domain.timeline import DayTimeline, Transition

pytestmark = pytest.mark.unit

LA = ZoneInfo("America/Los_Angeles")
UTC_ZONE = ZoneInfo("UTC")


def local(year, month, day, hour, minute=0, *, fold=0, zone=LA) -> datetime:
    """Return an aware UTC instant from a local wall time."""
    return datetime(year, month, day, hour, minute, fold=fold, tzinfo=zone).astimezone(
        UTC
    )


def policy(mode="shadow", **overrides) -> NotificationPolicy:
    return NotificationPolicy.from_settings(
        make_settings(notification_mode=mode, **overrides)
    )


class TestConstantsAndTags:
    def test_constants_match_the_adr(self):
        assert timedelta(hours=4) == RENOTIFY_COOLDOWN
        assert timedelta(minutes=15) == CLAIM_TTL
        assert timedelta(minutes=10) == SEND_DEADLINE
        assert SEND_DEADLINE < CLAIM_TTL
        assert timedelta(seconds=5) == URGENT_RETRY_DELAY
        assert CLAIM_LOCK_RETRIES == 3
        assert MAX_ITEMS_PER_PUSH == 8
        assert CLEAR_PHRASES == ("Clear", "No open findings", "sources fresh")

    def test_kinds_and_decisions_are_the_adr_vocabulary(self):
        assert {k.value for k in PushKind} == {
            "notice",
            "urgent",
            "status",
            "report",
            "ops",
        }
        assert {d.value for d in Decision} == {
            "send",
            "duplicate",
            "quiet_hours",
            "bypass_used",
            "cooldown",
            "in_flight",
            "stale",
        }

    def test_tag_set_follows_the_settings(self):
        tags = TagSet.from_settings(
            make_settings(
                apprise_tag_report="fl-r",
                apprise_tag_status="fl-s",
                apprise_tag_warning="fl-w",
                apprise_tag_urgent="fl-u",
                apprise_tag_ops="fl-o",
                apprise_tag_shadow="fl-x",
            )
        )
        assert (tags.report, tags.status, tags.warning) == ("fl-r", "fl-s", "fl-w")
        assert (tags.urgent, tags.ops, tags.shadow) == ("fl-u", "fl-o", "fl-x")

    def test_policy_follows_the_settings(self):
        built = policy(
            "production",
            quiet_hours_start=time(21, 30),
            quiet_hours_end=time(5, 45),
            imminent_window_minutes=90,
            change_scan_minutes=15,
            timezone="America/Chicago",
        )
        assert built.zone == ZoneInfo("America/Chicago")
        assert (built.quiet_start, built.quiet_end) == (time(21, 30), time(5, 45))
        assert built.imminent_window == timedelta(minutes=90)
        assert built.status_max_age == timedelta(minutes=15)
        assert built.pickup_tolerance == timedelta(minutes=30)
        assert built.mode == "production"
        assert built.tags.urgent == "fl-urgent"

    def test_default_mode_is_shadow(self):
        assert policy().mode == "shadow"

    def test_settings_refuse_a_time_zone_aware_quiet_hours_time(self):
        """N8: an aware ``time`` raises ``TypeError`` when compared."""
        with pytest.raises(ValueError, match="QUIET_HOURS"):
            make_settings(quiet_hours_start="22:00:00+01:00")
        with pytest.raises(ValueError, match="QUIET_HOURS"):
            make_settings(quiet_hours_end="06:00:00Z")


class TestImminentWindowBounds:
    def test_imminent_window_bounds(self):
        """T-pol-1."""
        window = timedelta(minutes=180)
        due, end = local(2026, 10, 23, 15), local(2026, 10, 23, 15, 45)
        second = timedelta(seconds=1)
        assert is_imminent(due, end, due - window, window)
        assert is_imminent(due, end, due, window)
        assert is_imminent(due, end, due + timedelta(minutes=20), window)
        assert is_imminent(due, end, end, window)
        assert not is_imminent(due, end, due - window - second, window)
        assert not is_imminent(due, end, end + second, window)
        zero = timedelta(0)
        assert not is_imminent(due, end, due - second, zero)
        assert is_imminent(due, end, due + second, zero)


class TestQuietHours:
    def test_quiet_hours_wrap_midnight(self):
        """T-pol-2."""
        start, end = time(22), time(6)

        def inside(hour, minute=0):
            return in_quiet_hours(local(2026, 10, 23, hour, minute), start, end, LA)

        assert not inside(21, 59)
        assert inside(22, 0)
        assert inside(3, 0)
        assert inside(5, 59)
        assert not inside(6, 0)
        assert not inside(12, 0)

    def test_quiet_hours_same_day_window(self):
        """T-pol-3."""
        start, end = time(13), time(15)

        def inside(hour, minute=0):
            return in_quiet_hours(local(2026, 10, 23, hour, minute), start, end, LA)

        assert not inside(12, 59)
        assert inside(13, 0)
        assert inside(14, 59)
        assert not inside(15, 0)
        assert not inside(23, 0)

    def test_the_zone_decides_not_utc(self):
        start, end = time(22), time(6)
        instant = datetime(2026, 10, 24, 6, 30, tzinfo=UTC)  # 23:30 in Los Angeles
        assert in_quiet_hours(instant, start, end, LA)
        assert not in_quiet_hours(instant, start, end, UTC_ZONE)

    def test_quiet_hours_spring_forward(self):
        """T-pol-4: 02:30 does not exist on 2027-03-14 in Los Angeles."""
        start, end = time(2, 30), time(6)
        first = datetime(2027, 3, 14, 8, 0, tzinfo=UTC)  # 00:00 PST
        results = []
        for minute in range(0, 6 * 60, 5):
            instant = first + timedelta(minutes=minute)
            results.append(
                (instant.astimezone(LA).time(), in_quiet_hours(instant, start, end, LA))
            )
        assert all(inside is (t >= time(3) and t < time(6)) for t, inside in results)
        assert not in_quiet_hours(local(2027, 3, 14, 1, 59), start, end, LA)
        assert in_quiet_hours(local(2027, 3, 14, 3, 0), start, end, LA)

    def test_the_wrapping_night_is_an_hour_shorter_on_spring_forward(self):
        start, end = time(22), time(6)
        first = local(2027, 3, 13, 12)
        hours = sum(
            in_quiet_hours(first + timedelta(minutes=m), start, end, LA)
            for m in range(0, 24 * 60, 60)
        )
        assert hours == 7

    def test_quiet_hours_fall_back(self):
        """T-pol-5: 01:30 happens twice on 2026-11-01 in Los Angeles."""
        first = local(2026, 11, 1, 1, 30, fold=0)
        second = local(2026, 11, 1, 1, 30, fold=1)
        assert first != second
        assert in_quiet_hours(first, time(1), time(2), LA)
        assert in_quiet_hours(second, time(1), time(2), LA)
        start, end = time(22), time(6)
        noon = local(2026, 10, 31, 12)
        hours = sum(
            in_quiet_hours(noon + timedelta(hours=h), start, end, LA) for h in range(24)
        )
        assert hours == 9

    def test_a_naive_now_is_refused(self):
        naive = datetime(2026, 10, 23, 23, 0)  # noqa: DTZ001
        with pytest.raises(ValueError, match="aware"):
            in_quiet_hours(naive, time(22), time(6), LA)

    def test_an_aware_boundary_is_refused(self):
        night = local(2026, 10, 23, 23)
        aware_start = time(22, tzinfo=UTC)
        with pytest.raises(ValueError, match="naive"):
            in_quiet_hours(night, aware_start, time(6), LA)

    def test_an_empty_window_is_refused(self):
        night = local(2026, 10, 23, 23)
        with pytest.raises(ValueError, match="differ"):
            in_quiet_hours(night, time(6), time(6), LA)


CAP = "CAP-014"
TABLE = [
    # rule, severity, imminent, kind
    (CAP, Severity.CRITICAL, True, PushKind.NOTICE),
    (CAP, Severity.CRITICAL, False, PushKind.NOTICE),
    (CAP, Severity.INCOMPLETE, True, PushKind.NOTICE),
    (CAP, Severity.INCOMPLETE, False, PushKind.NOTICE),
    (CAP, Severity.WARNING, True, PushKind.NOTICE),
    ("DEST-004", Severity.CRITICAL, True, PushKind.URGENT),
    ("DEST-004", Severity.CRITICAL, False, PushKind.NOTICE),
    ("ROUTE-010", Severity.INCOMPLETE, True, PushKind.URGENT),
    ("ROUTE-010", Severity.INCOMPLETE, False, PushKind.NOTICE),
    ("DATA-011", Severity.INCOMPLETE, True, PushKind.URGENT),
    ("CONF-008", Severity.WARNING, True, PushKind.NOTICE),
    ("CONF-008", Severity.WARNING, False, PushKind.NOTICE),
    ("PASS-003", Severity.WARNING, True, PushKind.NOTICE),
    ("PASS-003", Severity.CRITICAL, True, PushKind.URGENT),
]
PRODUCTION_TAG = {
    PushKind.URGENT: "fl-urgent",
    PushKind.NOTICE: "fl-warning",
    PushKind.STATUS: "fl-status",
    PushKind.REPORT: "fl-report",
    PushKind.OPS: "fl-ops",
}


class TestKindsAndTags:
    @pytest.mark.parametrize("mode", ["shadow", "production"])
    @pytest.mark.parametrize(("rule", "severity", "imminent", "kind"), TABLE)
    def test_severity_kind_tag_table(self, rule, severity, imminent, kind, mode):
        """T-pol-6."""
        found = make_finding(rule, "m", ("k",), severity=severity)
        assert finding_kind(found, imminent=imminent) is kind
        built = policy(mode)
        assert production_tag(kind, built.tags) == PRODUCTION_TAG[kind]
        expected = "fl-shadow" if mode == "shadow" else PRODUCTION_TAG[kind]
        assert delivery_tag(kind, built) == expected

    def test_warning_and_cap_014_are_never_urgent(self):
        """T-pol-7."""
        warning = make_finding("CONF-008", "m", ("k",))
        assert finding_kind(warning, imminent=True) is PushKind.NOTICE
        critical_cap = make_finding("CAP-014", "m", ("k",), severity=Severity.CRITICAL)
        assert finding_kind(critical_cap, imminent=True) is PushKind.NOTICE

    def test_every_kind_has_a_production_tag_and_shadow_collapses_all(self):
        built = policy("shadow")
        for kind in PushKind:
            assert production_tag(kind, built.tags) == PRODUCTION_TAG[kind]
            assert delivery_tag(kind, built) == "fl-shadow"

    def test_the_rank_used_for_escalation_matches_the_lifecycle_rank(self):
        from family_logistics.domain.notification_policy import severity_rank

        for severity in Severity:
            assert severity_rank(severity) == _RANK[severity]


# ---------------------------------------------------------------------------
# Planning and decisions
# ---------------------------------------------------------------------------

RIDE = trip(notes(driver="Riley"), uid="ride")
RIDE_KEY = RIDE.key.as_str()
RIDES = RideIndex.build(current=(RIDE,), previous=(), unparsed=())
EMPTY = RideIndex.build(current=(), previous=(), unparsed=())
NOW = RIDE.start - timedelta(hours=1)
"""One hour before the ride: imminent, afternoon local time (outside quiet hours)."""
SUBJECT = "issue:0123456789abcdef"


def critical(rule="TIME-007", keys=(RIDE_KEY, "route"), severity=Severity.CRITICAL):
    return make_finding(
        rule, "free text that must never matter", keys, severity=severity
    )


def tracked(finding, lifecycle=Lifecycle.NEW):
    return TrackedFinding(finding, lifecycle)


def result_of(
    *findings,
    status=RunStatus.ATTENTION_REQUIRED,
    lifecycle=Lifecycle.NEW,
    timelines=(),
    evaluated_at=NOW,
) -> AssessmentResult:
    return AssessmentResult(
        status,
        tuple(
            f if isinstance(f, TrackedFinding) else tracked(f, lifecycle)
            for f in findings
        ),
        (),
        timelines,
        evaluated_at,
    )


def make_plan(result, *, rides=RIDES, trigger="daily", now=NOW, mode="shadow"):
    return plan(
        run_id="run-2",
        trigger=trigger,
        result=result,
        rides=rides,
        now=now,
        policy=policy(mode),
    )


def keys_of(built):
    return [(p.subject_key, p.kind) for p in built.pushes]


class TestPlanForCompletedRuns:
    def test_a_critical_finding_on_an_imminent_ride_is_urgent(self):
        found = critical()
        built = make_plan(result_of(found), trigger="incremental", mode="production")
        (pushed,) = built.pushes
        assert pushed.subject_key == f"issue:{found.issue_id}"
        assert pushed.kind is PushKind.URGENT
        assert pushed.intended_tag == "fl-urgent"
        assert pushed.rule_id == "TIME-007"
        assert pushed.severity is Severity.CRITICAL
        assert pushed.episode_start is True
        assert pushed.tracked is not None
        assert pushed.subject is not None
        assert pushed.subject.due_at == RIDE.start
        assert pushed.run_wide is False
        assert built.completed is True
        assert built.open_issue_keys == {pushed.subject_key}

    def test_the_intended_tag_is_the_production_tag_even_in_shadow(self):
        built = make_plan(result_of(critical()), trigger="incremental")
        assert built.pushes[0].intended_tag == "fl-urgent"

    def test_a_far_ride_is_a_notice(self):
        far = NOW - timedelta(hours=5)
        built = make_plan(result_of(critical()), trigger="incremental", now=far)
        assert built.pushes[0].kind is PushKind.NOTICE
        assert built.pushes[0].intended_tag == "fl-warning"

    def test_episode_start_follows_the_lifecycle(self):
        for lifecycle, expected in (
            (Lifecycle.NEW, True),
            (Lifecycle.REGRESSED, True),
            (Lifecycle.ONGOING, False),
        ):
            built = make_plan(
                result_of(critical(), lifecycle=lifecycle), trigger="incremental"
            )
            assert built.pushes[0].episode_start is expected

    def test_warning_and_cap_014_are_never_urgent_in_a_plan(self):
        warning = critical("CONF-008", (RIDE_KEY,), Severity.WARNING)
        cap = critical("CAP-014", (RIDE_KEY, "capacity"), Severity.CRITICAL)
        built = make_plan(result_of(warning, cap), trigger="incremental")
        assert {p.kind for p in built.pushes} == {PushKind.NOTICE}

    def test_run_wide_imminence_includes_transitions(self):
        """T-pol-8: no imminent ride, but an imminent bus move."""
        move = Transition(
            "sam", NOW.date(), "loc:school", "home", NOW + timedelta(minutes=30), None
        )
        timelines = (DayTimeline("sam", NOW.date(), (), (move,)),)
        stale_snapshot = make_finding("DATA-011", "m", ("source:ha_snapshot",))
        with_move = make_plan(
            result_of(stale_snapshot, timelines=timelines),
            rides=EMPTY,
            trigger="incremental",
        )
        (pushed,) = with_move.pushes
        assert pushed.kind is PushKind.URGENT
        assert pushed.run_wide is True
        without = make_plan(
            result_of(stale_snapshot), rides=EMPTY, trigger="incremental"
        )
        assert without.pushes[0].kind is PushKind.NOTICE
        assert without.pushes[0].run_wide is True

    def test_unknown_ride_set_is_imminent_once_per_day(self):
        """T-pol-9."""
        stale = make_finding("DATA-011", "m", ("source:icloud",))
        built = make_plan(result_of(stale), rides=None, trigger="incremental")
        (pushed,) = built.pushes
        assert pushed.kind is PushKind.URGENT
        assert pushed.run_wide is True
        today = NOW
        delivered = state(
            kind=PushKind.URGENT,
            severity=Severity.INCOMPLETE,
            last_sent_at=today - timedelta(hours=2),
            episode_run_id="run-2",
        )
        assert decide_it(pushed, None) is Decision.SEND
        assert decide_it(pushed, delivered, now=today) is Decision.DUPLICATE
        tomorrow = today + timedelta(days=1)
        assert decide_it(pushed, delivered, now=tomorrow) is Decision.SEND

    def test_an_unresolved_subject_counts_as_imminent_and_is_reported(self):
        """T-subj-6: a broken key is urgent now and reported by rule ID."""
        broken = critical("TIME-007", ("cal|nope|-", "route"))
        far = NOW - timedelta(hours=12)
        built = make_plan(result_of(broken), trigger="incremental", now=far)
        assert built.pushes[0].kind is PushKind.URGENT
        assert built.unresolved_rule_ids == ("TIME-007",)
        assert built.unresolved == 1

    def test_data_011_is_never_reported_as_unresolved(self):
        stale = make_finding("DATA-011", "m", ("source:none",))
        built = make_plan(result_of(stale), trigger="incremental")
        assert built.unresolved == 0

    def test_a_day_level_finding_is_imminent_only_today(self):
        school = make_finding(
            "SCHOOL-009",
            "m",
            ("person:alex", f"day:{NOW.astimezone(LA).date().isoformat()}", "school"),
            severity=Severity.INCOMPLETE,
        )
        other_day = make_finding(
            "SCHOOL-009",
            "m",
            ("person:alex", "day:2026-10-30", "school"),
            severity=Severity.INCOMPLETE,
        )
        today = make_plan(result_of(school), trigger="incremental")
        later = make_plan(result_of(other_day), trigger="incremental")
        assert today.pushes[0].kind is PushKind.URGENT
        assert later.pushes[0].kind is PushKind.NOTICE
        assert today.unresolved == later.unresolved == 0

    def test_pushes_are_ordered_urgent_notice_status_ops(self):
        warning = critical("CONF-008", (RIDE_KEY,), Severity.WARNING)
        urgent = critical()
        built = make_plan(
            result_of(warning, urgent, status=RunStatus.INCOMPLETE), trigger="daily"
        )
        assert [p.kind for p in built.pushes] == [
            PushKind.URGENT,
            PushKind.NOTICE,
            PushKind.STATUS,
            PushKind.OPS,
        ]


RUN = "run:run-2"
RUN_LEVEL = [
    # status, trigger, expected pushes
    (RunStatus.CLEAR, "daily", [(RUN, PushKind.STATUS)]),
    (RunStatus.CLEAR, "on_demand", [(RUN, PushKind.STATUS)]),
    (RunStatus.CLEAR, "weekly", [(RUN, PushKind.REPORT)]),
    (RunStatus.CLEAR, "incremental", []),
    (RunStatus.ATTENTION_REQUIRED, "daily", [(RUN, PushKind.STATUS)]),
    (RunStatus.ATTENTION_REQUIRED, "on_demand", [(RUN, PushKind.STATUS)]),
    (RunStatus.ATTENTION_REQUIRED, "weekly", [(RUN, PushKind.REPORT)]),
    (RunStatus.ATTENTION_REQUIRED, "incremental", []),
    (
        RunStatus.INCOMPLETE,
        "daily",
        [(RUN, PushKind.STATUS), ("ops:run_incomplete", PushKind.OPS)],
    ),
    (
        RunStatus.INCOMPLETE,
        "on_demand",
        [(RUN, PushKind.STATUS), ("ops:run_incomplete", PushKind.OPS)],
    ),
    (
        RunStatus.INCOMPLETE,
        "weekly",
        [(RUN, PushKind.REPORT), ("ops:run_incomplete", PushKind.OPS)],
    ),
    (RunStatus.INCOMPLETE, "incremental", [("ops:run_incomplete", PushKind.OPS)]),
]


class TestRunLevelPushes:
    @pytest.mark.parametrize(("status", "trigger", "expected"), RUN_LEVEL)
    def test_status_and_report_by_trigger(self, status, trigger, expected):
        """T-pol-15: the D3 run-level table."""
        built = make_plan(result_of(status=status), trigger=trigger)
        assert keys_of(built) == expected
        assert built.completed is True
        assert built.incomplete is (status is RunStatus.INCOMPLETE)
        for pushed in built.pushes:
            if pushed.kind in {PushKind.STATUS, PushKind.REPORT}:
                assert pushed.run_status is status
                assert pushed.rule_id is None
                assert pushed.intended_tag == (
                    "fl-status" if pushed.kind is PushKind.STATUS else "fl-report"
                )

    def test_an_unknown_trigger_plans_no_status(self):
        built = make_plan(result_of(status=RunStatus.CLEAR), trigger="surprise")
        assert built.pushes == ()

    def test_a_late_status_is_dropped_not_sent(self):
        late = NOW + timedelta(minutes=61)
        for status in (RunStatus.CLEAR, RunStatus.INCOMPLETE):
            built = make_plan(result_of(status=status), now=late)
            assert not [
                p for p in built.pushes if p.kind in {PushKind.STATUS, PushKind.REPORT}
            ]
            assert built.status_stale is True
        on_time = make_plan(
            result_of(status=RunStatus.CLEAR), now=NOW + timedelta(minutes=60)
        )
        assert on_time.status_stale is False
        assert keys_of(on_time) == [(RUN, PushKind.STATUS)]

    def test_a_late_incomplete_run_still_raises_its_ops_episode(self):
        late = NOW + timedelta(hours=3)
        built = make_plan(result_of(status=RunStatus.INCOMPLETE), now=late)
        assert keys_of(built) == [("ops:run_incomplete", PushKind.OPS)]


class TestFailedAndUnavailableRuns:
    def failed(self, *previous, now=NOW):
        return failed_result(now, "assessment raised RuntimeError", previous)

    def test_failed_run_plans_retries_only(self):
        """T-pol-14."""
        first = critical()
        second = critical("DEST-004", (RIDE_KEY, "passenger:sam", "destination"))
        built = make_plan(self.failed(first, second), trigger="daily")
        assert built.completed is False
        assert built.incomplete is False
        keys = {p.subject_key: p for p in built.pushes}
        assert set(keys) == {
            "run-failed",
            "ops:run_failed",
            f"issue:{first.issue_id}",
            f"issue:{second.issue_id}",
        }
        assert keys["run-failed"].kind is PushKind.URGENT
        assert keys["run-failed"].run_wide is True
        assert keys["ops:run_failed"].kind is PushKind.OPS
        for found in (first, second):
            retry = keys[f"issue:{found.issue_id}"]
            assert retry.retry_only is True
            assert retry.episode_start is False
        assert not [
            p for p in built.pushes if p.kind in {PushKind.STATUS, PushKind.REPORT}
        ]

    def test_a_failed_run_with_no_imminent_ride_is_a_notice(self):
        built = make_plan(self.failed(), trigger="daily", now=NOW - timedelta(hours=9))
        assert keys_of(built) == [
            ("run-failed", PushKind.NOTICE),
            ("ops:run_failed", PushKind.OPS),
        ]

    def test_a_failed_run_with_no_readable_history_is_urgent(self):
        built = make_plan(
            self.failed(), rides=None, trigger="daily", now=NOW - timedelta(hours=9)
        )
        assert built.pushes[0].subject_key == "run-failed"
        assert built.pushes[0].kind is PushKind.URGENT

    def test_a_failed_run_reads_imminence_from_the_history_rides(self):
        history = RideIndex.build(current=(), previous=(RIDE,), unparsed=())
        built = make_plan(self.failed(), rides=history, trigger="daily")
        assert built.pushes[0].kind is PushKind.URGENT

    def test_a_failed_run_never_counts_unresolved_subjects(self):
        """Its timelines are empty, so coverage findings cannot resolve."""
        coverage = make_finding(
            "RIDE-005",
            "m",
            ("person:sam", "day:2026-10-23", "from:a", "to:b"),
        )
        built = make_plan(self.failed(coverage), trigger="daily")
        assert built.unresolved == 0

    @pytest.mark.parametrize("trigger", ["weekly", "daily", "on_demand", "incremental"])
    def test_a_failed_run_never_plans_a_status_or_report(self, trigger):
        built = make_plan(self.failed(), trigger=trigger)
        assert all(
            p.kind not in {PushKind.STATUS, PushKind.REPORT} for p in built.pushes
        )
        assert all(p.run_status is not RunStatus.CLEAR for p in built.pushes)

    def test_plan_unavailable(self):
        """T-pol-16."""
        built = plan_unavailable(now=NOW, policy=policy("production"))
        assert keys_of(built) == [
            ("run-failed", PushKind.URGENT),
            ("ops:run_failed", PushKind.OPS),
        ]
        assert built.pushes[0].intended_tag == "fl-urgent"
        assert built.pushes[0].run_wide is True
        assert built.completed is False
        assert built.incomplete is False
        assert built.open_issue_keys == frozenset()


def state(**overrides) -> StateView:
    values = {
        "is_open": True,
        "delivered": True,
        "kind": PushKind.NOTICE,
        "severity": Severity.WARNING,
        "bypass_used": False,
        "episode_run_id": "run-1",
        "last_sent_at": NOW - timedelta(hours=1),
        "claimed_at": None,
    }
    return StateView(**(values | overrides))


def push(**overrides) -> PlannedPush:
    values = {
        "subject_key": SUBJECT,
        "kind": PushKind.NOTICE,
        "intended_tag": "fl-warning",
        "severity": Severity.WARNING,
    }
    return PlannedPush(**(values | overrides))


def decide_it(pushed, row, *, now=NOW, quiet=False, run_id="run-2", mode="shadow"):
    return decide(pushed, row, run_id=run_id, now=now, quiet=quiet, policy=policy(mode))


class TestDecide:
    def test_a_new_subject_is_sent(self):
        assert decide_it(push(), None) is Decision.SEND

    def test_a_fresh_claim_is_in_flight_and_an_old_claim_is_abandoned(self):
        claimed = state(delivered=False, claimed_at=NOW - timedelta(minutes=14))
        assert decide_it(push(), claimed) is Decision.IN_FLIGHT
        abandoned = state(delivered=False, claimed_at=NOW - CLAIM_TTL)
        assert decide_it(push(), abandoned) is Decision.SEND

    def test_in_flight_is_checked_before_everything_else(self):
        claimed = state(
            delivered=True, claimed_at=NOW - timedelta(minutes=1), bypass_used=True
        )
        urgent = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        assert decide_it(urgent, claimed, quiet=True) is Decision.IN_FLIGHT

    def test_ongoing_after_delivery_is_a_duplicate(self):
        assert decide_it(push(), state()) is Decision.DUPLICATE

    def test_open_but_never_delivered_is_owed(self):
        """Held, failed, or open before the service existed."""
        undelivered = state(delivered=False, severity=None, last_sent_at=None)
        assert decide_it(push(), undelivered) is Decision.SEND

    def test_escalation_and_no_downgrade(self):
        """T-pol-12 (F5)."""
        warning = state(kind=PushKind.NOTICE, severity=Severity.WARNING)
        worse = push(severity=Severity.CRITICAL)
        assert decide_it(worse, warning) is Decision.SEND
        incomplete = push(severity=Severity.INCOMPLETE)
        assert decide_it(incomplete, warning) is Decision.SEND
        urgent_row = state(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        assert decide_it(push(severity=Severity.CRITICAL), urgent_row) is (
            Decision.DUPLICATE
        )
        assert decide_it(push(severity=Severity.WARNING), urgent_row) is (
            Decision.DUPLICATE
        )
        notice_critical = state(kind=PushKind.NOTICE, severity=Severity.CRITICAL)
        escalated_kind = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        assert decide_it(escalated_kind, notice_critical) is Decision.SEND

    def test_regression_from_another_run_starts_an_episode(self):
        """T-pol-13 (F4)."""
        delivered = state(
            episode_run_id="run-0",
            kind=PushKind.URGENT,
            severity=Severity.CRITICAL,
            last_sent_at=NOW - timedelta(days=2),
        )
        regressed = push(
            kind=PushKind.URGENT, severity=Severity.CRITICAL, episode_start=True
        )
        assert decide_it(regressed, delivered) is Decision.SEND

    def test_a_redispatch_of_the_same_run_keeps_the_episode(self):
        delivered = state(episode_run_id="run-2", kind=PushKind.URGENT)
        again = push(
            kind=PushKind.URGENT, severity=Severity.WARNING, episode_start=True
        )
        assert decide_it(again, delivered) is Decision.DUPLICATE

    def test_a_closed_row_is_a_new_episode(self):
        closed = state(
            is_open=False,
            delivered=False,
            severity=None,
            last_sent_at=NOW - RENOTIFY_COOLDOWN,
        )
        assert decide_it(push(), closed) is Decision.SEND

    def test_notice_cooldown_after_regression(self):
        """T-pol-11."""
        closed = state(
            is_open=False,
            delivered=False,
            severity=None,
            last_sent_at=NOW - timedelta(hours=1),
        )
        regressed = push(episode_start=True)
        assert decide_it(regressed, closed) is Decision.COOLDOWN
        long_ago = replace(closed, last_sent_at=NOW - RENOTIFY_COOLDOWN)
        assert decide_it(regressed, long_ago) is Decision.SEND
        urgent = push(
            kind=PushKind.URGENT, severity=Severity.CRITICAL, episode_start=True
        )
        assert decide_it(urgent, closed) is Decision.SEND
        never = replace(closed, last_sent_at=None)
        assert decide_it(regressed, never) is Decision.SEND

    def test_a_held_notice_stays_held_on_the_next_scan(self):
        """A held regression is ONGOING on the next scan and must stay held."""
        closed = state(
            is_open=False,
            delivered=False,
            severity=None,
            last_sent_at=NOW - timedelta(hours=1),
        )
        ongoing = push(episode_start=False)
        assert decide_it(ongoing, closed) is Decision.COOLDOWN
        long_ago = replace(closed, last_sent_at=NOW - RENOTIFY_COOLDOWN)
        assert decide_it(ongoing, long_ago) is Decision.SEND

    def test_an_open_delivered_row_is_not_held_by_the_cooldown(self):
        """A regression after a missed dispatch is news, not flapping (F4)."""
        open_row = state(
            episode_run_id="run-1",
            kind=PushKind.NOTICE,
            severity=Severity.WARNING,
            last_sent_at=NOW - timedelta(hours=1),
        )
        regressed = push(episode_start=True)
        assert decide_it(regressed, open_row, run_id="run-2") is Decision.SEND

    def test_cooldown_is_checked_before_quiet_hours(self):
        closed = state(is_open=False, delivered=False, severity=None, last_sent_at=NOW)
        assert decide_it(push(episode_start=True), closed, quiet=True) is (
            Decision.COOLDOWN
        )

    def test_only_urgent_bypasses_and_only_once(self):
        """T-pol-10."""
        for kind in (PushKind.NOTICE, PushKind.STATUS, PushKind.REPORT, PushKind.OPS):
            assert decide_it(push(kind=kind), None, quiet=True) is Decision.QUIET_HOURS
        urgent = push(kind=PushKind.URGENT, severity=Severity.INCOMPLETE)
        assert decide_it(urgent, None, quiet=True) is Decision.SEND
        used = state(
            kind=PushKind.URGENT,
            severity=Severity.INCOMPLETE,
            bypass_used=True,
        )
        worse = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        assert decide_it(worse, used, quiet=True) is Decision.BYPASS_USED
        assert decide_it(worse, used, quiet=False) is Decision.SEND

    def test_a_new_episode_gets_its_own_bypass(self):
        used = state(
            episode_run_id="run-0",
            bypass_used=True,
            kind=PushKind.URGENT,
            severity=Severity.CRITICAL,
        )
        regressed = push(
            kind=PushKind.URGENT, severity=Severity.CRITICAL, episode_start=True
        )
        assert decide_it(regressed, used, quiet=True) is Decision.SEND

    def test_a_held_issue_is_owed_again_after_quiet_hours(self):
        pushed = push(episode_start=True)
        assert decide_it(pushed, None, quiet=True) is Decision.QUIET_HOURS
        assert decide_it(pushed, None, quiet=False) is Decision.SEND

    def test_the_daily_rearm_applies_to_urgent_run_wide_pushes_only(self):
        sent = NOW - timedelta(hours=2)
        row = state(
            kind=PushKind.URGENT, severity=Severity.INCOMPLETE, last_sent_at=sent
        )
        rearm = push(kind=PushKind.URGENT, severity=Severity.INCOMPLETE, run_wide=True)
        assert decide_it(rearm, row) is Decision.DUPLICATE
        assert decide_it(rearm, row, now=NOW + timedelta(days=1)) is Decision.SEND
        not_wide = replace(rearm, run_wide=False)
        assert decide_it(not_wide, row, now=NOW + timedelta(days=1)) is (
            Decision.DUPLICATE
        )
        notice = push(severity=Severity.INCOMPLETE, run_wide=True)
        notice_row = state(severity=Severity.INCOMPLETE, last_sent_at=sent)
        assert decide_it(notice, notice_row, now=NOW + timedelta(days=1)) is (
            Decision.DUPLICATE
        )

    def test_the_rearm_uses_the_local_calendar_day(self):
        late = local(2026, 10, 23, 23, 30)
        row = state(
            kind=PushKind.URGENT, severity=Severity.INCOMPLETE, last_sent_at=late
        )
        rearm = push(kind=PushKind.URGENT, severity=Severity.INCOMPLETE, run_wide=True)
        same_local_day = local(2026, 10, 23, 23, 59)
        next_local_day = local(2026, 10, 24, 0, 1)
        assert decide_it(rearm, row, now=same_local_day) is Decision.DUPLICATE
        assert decide_it(rearm, row, now=next_local_day) is Decision.SEND

    def test_the_rearm_does_not_grant_a_second_bypass(self):
        row = state(
            kind=PushKind.URGENT,
            severity=Severity.INCOMPLETE,
            bypass_used=True,
            last_sent_at=local(2026, 10, 23, 23, 30),
        )
        rearm = push(kind=PushKind.URGENT, severity=Severity.INCOMPLETE, run_wide=True)
        night = local(2026, 10, 24, 0, 30)
        assert decide_it(rearm, row, now=night, quiet=True) is Decision.BYPASS_USED
        morning = local(2026, 10, 24, 6, 30)
        assert decide_it(rearm, row, now=morning, quiet=False) is Decision.SEND

    def test_a_retry_is_sent_only_for_an_open_undelivered_row(self):
        retry = push(retry_only=True)
        assert decide_it(retry, None) is Decision.DUPLICATE
        assert decide_it(retry, state()) is Decision.DUPLICATE
        closed = state(is_open=False, delivered=False)
        assert decide_it(retry, closed) is Decision.DUPLICATE
        undelivered = state(delivered=False, severity=None, last_sent_at=None)
        assert decide_it(retry, undelivered) is Decision.SEND

    def test_a_retry_uses_the_kind_stored_on_the_row(self):
        """An urgent push that failed at 14:00 is retried at night as urgent."""
        retry = push(retry_only=True, kind=PushKind.NOTICE)
        urgent_row = state(
            delivered=False, kind=PushKind.URGENT, severity=None, last_sent_at=None
        )
        assert decide_it(retry, urgent_row, quiet=True) is Decision.SEND
        notice_row = replace(urgent_row, kind=PushKind.NOTICE)
        assert decide_it(retry, notice_row, quiet=True) is Decision.QUIET_HOURS

    def test_status_and_report_rows_ignore_the_open_flag(self):
        status = push(subject_key="run:run-2", kind=PushKind.STATUS)
        assert decide_it(status, None) is Decision.SEND
        delivered = state(is_open=False, kind=PushKind.STATUS, episode_run_id="run-2")
        assert decide_it(status, delivered) is Decision.DUPLICATE
        failed = state(
            is_open=False,
            delivered=False,
            kind=PushKind.STATUS,
            severity=None,
            last_sent_at=None,
        )
        assert decide_it(status, failed) is Decision.SEND
        assert decide_it(status, None, quiet=True) is Decision.QUIET_HOURS

    def test_ops_episodes_notify_once_until_closed(self):
        ops = push(subject_key="ops:run_incomplete", kind=PushKind.OPS, severity=None)
        assert decide_it(ops, None) is Decision.SEND
        delivered = state(kind=PushKind.OPS, severity=None)
        assert decide_it(ops, delivered) is Decision.DUPLICATE
        closed = state(is_open=False, delivered=False, kind=PushKind.OPS, severity=None)
        assert decide_it(ops, closed) is Decision.SEND
        assert decide_it(ops, None, quiet=True) is Decision.QUIET_HOURS

    def test_a_naive_clock_is_refused(self):
        planned = push()
        resolved = policy()
        naive = datetime(2026, 10, 23, 12)  # noqa: DTZ001
        with pytest.raises(ValueError, match="aware"):
            decide(planned, None, run_id="r", now=naive, quiet=False, policy=resolved)


# ---------------------------------------------------------------------------
# Properties of the never-clear rule
# ---------------------------------------------------------------------------

PROPS = settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
TRIGGERS = st.sampled_from(["weekly", "daily", "on_demand", "incremental", "other"])
SEVERITIES = st.sampled_from(list(Severity))
RULE_KEYS = st.sampled_from(
    [
        ("TIME-007", (RIDE_KEY, "route")),
        ("CONF-008", (RIDE_KEY,)),
        ("CAP-014", (RIDE_KEY, "capacity")),
        ("DATA-011", ("source:icloud",)),
        ("TIME-007", ("cal|broken|-", "route")),
    ]
)
FINDINGS = st.builds(
    lambda rule_keys, severity: make_finding(
        rule_keys[0], "x", rule_keys[1], severity=severity
    ),
    RULE_KEYS,
    SEVERITIES,
)
OFFSETS = st.integers(min_value=-24 * 60, max_value=24 * 60)
RIDE_SETS = st.sampled_from([RIDES, EMPTY, None])


@st.composite
def run_results(draw, statuses):
    findings = draw(st.lists(FINDINGS, max_size=4))
    lifecycle = draw(st.sampled_from(list(Lifecycle)))
    status = draw(st.sampled_from(statuses))
    return result_of(*findings, status=status, lifecycle=lifecycle)


class TestNeverClearProperties:
    @PROPS
    @given(
        run_results([RunStatus.INCOMPLETE]),
        TRIGGERS,
        RIDE_SETS,
        OFFSETS,
        st.sampled_from(["shadow", "production"]),
    )
    def test_an_incomplete_run_is_never_planned_as_clear(
        self, result, trigger, rides, offset, mode
    ):
        built = make_plan(
            result,
            rides=rides,
            trigger=trigger,
            now=NOW + timedelta(minutes=offset),
            mode=mode,
        )
        for pushed in built.pushes:
            assert pushed.run_status in {None, RunStatus.INCOMPLETE}
        assert built.incomplete is True

    @PROPS
    @given(st.lists(FINDINGS, max_size=4), TRIGGERS, RIDE_SETS, OFFSETS)
    def test_a_failed_run_never_plans_a_status_or_a_report(
        self, findings, trigger, rides, offset
    ):
        failed = failed_result(NOW, "boom", findings)
        built = make_plan(
            failed, rides=rides, trigger=trigger, now=NOW + timedelta(minutes=offset)
        )
        assert built.completed is False
        assert all(
            p.kind not in {PushKind.STATUS, PushKind.REPORT} for p in built.pushes
        )
        assert all(p.run_status is None for p in built.pushes)
        assert any(p.subject_key == "run-failed" for p in built.pushes)

    @PROPS
    @given(OFFSETS, st.sampled_from(["shadow", "production"]))
    def test_an_unavailable_run_never_plans_a_status_or_a_report(self, offset, mode):
        built = plan_unavailable(
            now=NOW + timedelta(minutes=offset), policy=policy(mode)
        )
        assert built.completed is False
        assert {p.subject_key for p in built.pushes} == {"run-failed", "ops:run_failed"}
        assert all(p.run_status is None for p in built.pushes)

    @PROPS
    @given(
        run_results(list(RunStatus)),
        TRIGGERS,
        RIDE_SETS,
        OFFSETS,
    )
    def test_a_status_push_always_carries_the_runs_own_status(
        self, result, trigger, rides, offset
    ):
        built = make_plan(
            result, rides=rides, trigger=trigger, now=NOW + timedelta(minutes=offset)
        )
        for pushed in built.pushes:
            if pushed.kind in {PushKind.STATUS, PushKind.REPORT}:
                assert pushed.run_status is result.run_status
                assert result.run_status is not RunStatus.FAILED


class TestInProgressRideProperties:
    @PROPS
    @given(
        st.integers(min_value=-180, max_value=45),
        st.sampled_from([Severity.CRITICAL, Severity.INCOMPLETE]),
        st.sampled_from(["TIME-007", "DEST-004", "ROUTE-010", "ORDER-012"]),
        st.booleans(),
    )
    def test_an_imminent_or_in_progress_ride_is_never_downgraded_or_held(
        self, minutes_from_start, severity, rule, quiet
    ):
        now = RIDE.start + timedelta(minutes=minutes_from_start)
        found = critical(rule, (RIDE_KEY, "route"), severity)
        built = make_plan(result_of(found), trigger="incremental", now=now)
        (pushed,) = built.pushes
        assert pushed.kind is PushKind.URGENT
        assert pushed.intended_tag == "fl-urgent"
        assert decide_it(pushed, None, now=now, quiet=quiet) is Decision.SEND
        closed = state(
            is_open=False,
            delivered=False,
            severity=None,
            last_sent_at=now - timedelta(minutes=1),
        )
        assert decide_it(pushed, closed, now=now, quiet=quiet) is Decision.SEND
        delivered = state(
            kind=PushKind.URGENT,
            severity=severity,
            episode_run_id="run-2",
            last_sent_at=now - timedelta(minutes=1),
        )
        assert decide_it(pushed, delivered, now=now, quiet=quiet) is (
            Decision.DUPLICATE
        )

    @PROPS
    @given(
        st.booleans(),
        st.sampled_from(list(Severity)),
        st.booleans(),
        st.booleans(),
        st.booleans(),
        st.booleans(),
    )
    def test_an_urgent_push_is_never_held_by_quiet_hours_or_cooldown(
        self, quiet, delivered_severity, is_open, delivered, bypass_used, episode_start
    ):
        urgent = push(
            kind=PushKind.URGENT,
            severity=Severity.CRITICAL,
            episode_start=episode_start,
        )
        row = state(
            is_open=is_open,
            delivered=delivered,
            kind=PushKind.URGENT,
            severity=delivered_severity,
            bypass_used=bypass_used,
        )
        decision = decide_it(urgent, row, quiet=quiet)
        assert decision not in {Decision.QUIET_HOURS, Decision.COOLDOWN}
        if decision is Decision.BYPASS_USED:
            assert quiet
            assert bypass_used


ZONES = st.sampled_from(
    [
        "America/Los_Angeles",
        "America/New_York",
        "America/Sao_Paulo",
        "Europe/London",
        "Australia/Lord_Howe",
        "Australia/Sydney",
        "Asia/Kolkata",
        "UTC",
    ]
)
TIMES = st.builds(
    time,
    st.integers(min_value=0, max_value=23),
    st.integers(min_value=0, max_value=59),
)
INSTANTS = st.datetimes(
    min_value=datetime(2026, 1, 1),  # noqa: DTZ001
    max_value=datetime(2028, 12, 31),  # noqa: DTZ001
).map(lambda value: value.replace(tzinfo=UTC))
DST_NIGHTS = st.sampled_from(
    [
        datetime(2027, 3, 14, 8, tzinfo=UTC),
        datetime(2026, 11, 1, 7, tzinfo=UTC),
        datetime(2027, 3, 28, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 0, tzinfo=UTC),
        datetime(2026, 10, 4, 14, tzinfo=UTC),
    ]
).flatmap(
    lambda base: st.integers(min_value=0, max_value=36 * 60).map(
        lambda minutes: base + timedelta(minutes=minutes)
    )
)


class TestQuietHoursProperties:
    @PROPS
    @given(st.one_of(INSTANTS, DST_NIGHTS), TIMES, TIMES, ZONES)
    def test_quiet_hours_never_raise_and_the_two_windows_partition_the_day(
        self, instant, start, end, zone
    ):
        assume(start != end)
        zone_info = ZoneInfo(zone)
        inside = in_quiet_hours(instant, start, end, zone_info)
        outside = in_quiet_hours(instant, end, start, zone_info)
        assert isinstance(inside, bool)
        assert inside is not outside
