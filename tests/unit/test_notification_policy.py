"""Notification policy: kinds, tags, quiet hours, planning and decisions (ADR 0008).

The policy is pure: ``now`` is always passed in, nothing reads a clock or the
database, and nothing is logged.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fl_service import make_settings

from family_logistics.domain.lifecycle import _RANK
from family_logistics.domain.models import Severity
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
    PushKind,
    TagSet,
    delivery_tag,
    finding_kind,
    in_quiet_hours,
    production_tag,
)
from family_logistics.domain.notification_subjects import is_imminent
from family_logistics.domain.rules import make_finding

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
        with pytest.raises(ValueError, match="aware"):
            in_quiet_hours(datetime(2026, 10, 23, 23, 0), time(22), time(6), LA)  # noqa: DTZ001

    def test_an_aware_boundary_is_refused(self):
        with pytest.raises(ValueError, match="naive"):
            in_quiet_hours(local(2026, 10, 23, 23), time(22, tzinfo=UTC), time(6), LA)

    def test_an_empty_window_is_refused(self):
        with pytest.raises(ValueError, match="differ"):
            in_quiet_hours(local(2026, 10, 23, 23), time(6), time(6), LA)


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
