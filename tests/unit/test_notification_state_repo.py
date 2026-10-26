"""Notification state repository: claims, watermark, completion and closing.

ADR 0008, D9 and D11. These run on a file-backed SQLite database at head (WAL,
single writer) with real transactions; the pure decision function is tested in
``test_notification_policy.py`` and only its use is checked here.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from fl_notification_state import (
    ISSUE,
    ISSUE_B,
    NIGHT,
    T0,
    all_rows,
    delivered_row,
    get_row,
    get_watermark,
    make_engine,
    make_plan,
    minutes,
    policy,
    push,
    put_row,
)
from fl_service import seed_registry
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from family_logistics.core.database import create_sqlite_engine
from family_logistics.core.exceptions import DatabaseError, ValidationError
from family_logistics.db.notifications import (
    NotificationStateBusyError,
    NotificationStateRepository,
    load_name_rows,
)
from family_logistics.domain.models import Severity
from family_logistics.domain.notification_policy import (
    CLAIM_LOCK_RETRIES,
    CLAIM_TTL,
    RENOTIFY_COOLDOWN,
    Decision,
    PushKind,
)

pytestmark = pytest.mark.unit

RUN_FAILED = "run-failed"
OPS_FAILED = "ops:run_failed"
OPS_INCOMPLETE = "ops:run_incomplete"
STATUS = "run:run-1"


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path)
    yield engine
    engine.dispose()


@pytest.fixture
def repo(engine):
    return NotificationStateRepository(
        engine, lock_timeout_ms=50, sleep=lambda _s: None
    )


def claim(
    repo,
    *pushes,
    run_id="run-1",
    evaluated_at=T0,
    now=T0,
    quiet=False,
    mode="shadow",
    token="tok-a",
    pol=None,
    **plan_kwargs,
):
    """Claim ``pushes`` as one plan and return the ``ClaimResult``."""
    return repo.claim(
        make_plan(*pushes, **plan_kwargs),
        mode=mode,
        token=token,
        run_id=run_id,
        evaluated_at=evaluated_at,
        now=now,
        quiet=quiet,
        policy=pol or policy(mode),
    )


def decisions(result):
    return [d.decision for d in result.decisions]


class TestWatermark:
    """T-db-5: an older outcome is stale, an equal one only for the same run."""

    def test_the_first_claim_creates_the_watermark(self, repo, engine):
        claim(repo, push())
        mark = get_watermark(engine)
        assert (mark.id, mark.evaluated_at, mark.run_id) == (1, T0, "run-1")

    def test_a_newer_outcome_advances_it(self, repo, engine):
        claim(repo, push(), evaluated_at=T0, run_id="run-1")
        later = T0 + minutes(5)
        claim(repo, push(), evaluated_at=later, run_id="run-2", now=later)
        mark = get_watermark(engine)
        assert (mark.evaluated_at, mark.run_id) == (later, "run-2")

    def test_an_older_outcome_is_stale_and_writes_nothing(self, repo, engine):
        newer = T0 + minutes(5)
        claim(repo, push(ISSUE_B), evaluated_at=newer, run_id="run-2", now=newer)
        before = all_rows(engine)
        result = claim(repo, push(), evaluated_at=T0, run_id="run-1", now=newer)
        assert result.stale
        assert decisions(result) == [Decision.STALE]
        assert result.claimed == ()
        assert all_rows(engine) == before
        assert get_watermark(engine).run_id == "run-2"

    def test_an_equal_timestamp_from_the_same_run_is_a_redispatch(self, repo):
        claim(repo, push(), token="tok-a")
        result = claim(repo, push(ISSUE_B), token="tok-b")
        assert not result.stale
        assert decisions(result) == [Decision.SEND]

    def test_an_equal_timestamp_from_another_run_is_stale(self, repo):
        claim(repo, push(), run_id="run-1")
        result = claim(repo, push(ISSUE_B), run_id="run-2")
        assert result.stale
        assert decisions(result) == [Decision.STALE]

    def test_a_stale_outcome_closes_nothing(self, repo, engine):
        newer = T0 + minutes(9)
        claim(
            repo, push(), push(ISSUE_B), evaluated_at=newer, run_id="run-9", now=newer
        )
        result = claim(repo, evaluated_at=T0, run_id="run-1", now=newer)
        assert result.stale
        assert get_row(engine).is_open

    def test_the_watermark_advances_even_when_nothing_is_sent(self, repo, engine):
        put_row(engine, **delivered_row())
        later = T0 + minutes(5)
        result = claim(repo, push(), evaluated_at=later, run_id="run-1", now=later)
        assert decisions(result) == [Decision.DUPLICATE]
        assert get_watermark(engine).evaluated_at == later

    def test_the_outcome_time_is_compared_not_the_clock(self, repo, engine):
        claim(repo, push(ISSUE_B), evaluated_at=T0, run_id="run-1", now=T0)
        result = claim(
            repo,
            push(),
            evaluated_at=T0 - minutes(1),
            run_id="run-0",
            now=T0 + minutes(30),
        )
        assert result.stale
        assert get_watermark(engine).evaluated_at == T0


class TestClaim:
    def test_a_new_subject_is_claimed_and_recorded(self, repo, engine):
        result = claim(repo, push(episode_start=True))
        assert decisions(result) == [Decision.SEND]
        (claimed,) = result.claimed
        assert claimed.subject_key == ISSUE
        row = get_row(engine)
        assert (row.kind, row.tag, row.rule_id) == ("notice", "fl-warning", "DEST-004")
        assert (row.is_open, row.delivered, row.severity) == (True, False, None)
        assert (row.claim_token, row.claimed_at) == ("tok-a", T0)
        assert (row.episode_run_id, row.run_id, row.evaluated_at) == (
            "run-1",
            "run-1",
            T0,
        )
        assert (row.created_at, row.updated_at) == (T0, T0)

    def test_a_run_status_row_is_never_open(self, repo, engine):
        claim(repo, push(STATUS, PushKind.STATUS))
        row = get_row(engine, STATUS)
        assert (row.is_open, row.kind, row.tag) == (False, "status", "fl-status")
        assert row.rule_id is None

    def test_ops_and_run_failed_rows_are_open_episodes(self, repo, engine):
        claim(
            repo,
            push(RUN_FAILED, PushKind.URGENT, run_wide=True),
            push(OPS_FAILED, PushKind.OPS),
            completed=False,
        )
        assert get_row(engine, RUN_FAILED).is_open
        assert get_row(engine, OPS_FAILED).is_open

    def test_a_fresh_claim_blocks_another_dispatch(self, repo, engine):
        claim(repo, push(), token="tok-a")
        result = claim(repo, push(), token="tok-b")
        assert decisions(result) == [Decision.IN_FLIGHT]
        assert get_row(engine).claim_token == "tok-a"

    def test_a_claim_expires_after_the_ttl(self, repo, engine):
        claim(repo, push(), token="tok-a")
        just_before = T0 + CLAIM_TTL - timedelta(seconds=1)
        blocked = claim(repo, push(), token="tok-b", now=just_before)
        assert decisions(blocked) == [Decision.IN_FLIGHT]
        at_ttl = T0 + CLAIM_TTL
        taken = claim(repo, push(), token="tok-c", now=at_ttl)
        assert decisions(taken) == [Decision.SEND]
        row = get_row(engine)
        assert (row.claim_token, row.claimed_at) == ("tok-c", at_ttl)

    def test_an_abandoned_claim_cannot_complete_over_its_successor(self, repo, engine):
        claim(repo, push(), token="tok-a")
        taken = T0 + CLAIM_TTL
        claim(repo, push(), token="tok-c", now=taken)
        repo.complete(
            [push()],
            mode="shadow",
            token="tok-a",
            delivered=True,
            now=taken,
            quiet=False,
        )
        row = get_row(engine)
        assert (row.delivered, row.claim_token) == (False, "tok-c")

    def test_a_delivered_subject_is_a_duplicate(self, repo, engine):
        claim(repo, push(episode_start=True))
        repo.complete(
            [push()],
            mode="shadow",
            token="tok-a",
            delivered=True,
            now=T0,
            quiet=False,
        )
        result = claim(repo, push(), token="tok-b", now=T0 + minutes(1))
        assert decisions(result) == [Decision.DUPLICATE]
        assert result.claimed == ()
        assert get_row(engine).send_count == 1

    def test_every_planned_push_gets_a_decision_in_order(self, repo):
        result = claim(repo, push(ISSUE_B, PushKind.URGENT), push(ISSUE))
        assert [d.push.subject_key for d in result.decisions] == [ISSUE_B, ISSUE]
        assert decisions(result) == [Decision.SEND, Decision.SEND]

    def test_modes_have_separate_rows(self, repo, engine):
        claim(repo, push(), mode="shadow", token="tok-a")
        result = claim(repo, push(), mode="production", token="tok-b")
        assert decisions(result) == [Decision.SEND]
        assert get_row(engine, mode="shadow").claim_token == "tok-a"
        assert get_row(engine, mode="production").claim_token == "tok-b"

    def test_a_naive_clock_is_refused(self, repo):
        naive = T0.replace(tzinfo=None)
        plan = make_plan(push())
        pol = policy()
        with pytest.raises(ValidationError, match="timezone-aware"):
            repo.claim(
                plan,
                mode="shadow",
                token="t",
                run_id="run-1",
                evaluated_at=T0,
                now=naive,
                quiet=False,
                policy=pol,
            )

    def test_a_naive_outcome_time_is_refused(self, repo):
        naive = T0.replace(tzinfo=None)
        plan = make_plan(push())
        pol = policy()
        with pytest.raises(ValidationError, match="timezone-aware"):
            repo.claim(
                plan,
                mode="shadow",
                token="t",
                run_id="run-1",
                evaluated_at=naive,
                now=T0,
                quiet=False,
                policy=pol,
            )


class TestEscalationAndRetry:
    def test_an_escalation_keeps_the_delivered_kind_until_completion(
        self, repo, engine
    ):
        put_row(engine, **delivered_row())
        urgent = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        result = claim(repo, urgent)
        assert decisions(result) == [Decision.SEND]
        row = get_row(engine)
        # #CRITICAL: Data integrity: a failed escalation must still look like a
        # delivered warning notice, or the escalation would never be re-owed.
        assert (row.kind, row.tag, row.severity, row.delivered) == (
            "notice",
            "fl-warning",
            "warning",
            True,
        )
        assert row.claim_token == "tok-a"

    def test_a_failed_escalation_is_owed_again(self, repo, engine):
        put_row(engine, **delivered_row())
        urgent = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        claim(repo, urgent)
        repo.complete(
            [urgent], mode="shadow", token="tok-a", delivered=False, now=T0, quiet=False
        )
        again = claim(repo, urgent, token="tok-b", now=T0 + minutes(1))
        assert decisions(again) == [Decision.SEND]

    def test_a_delivered_escalation_updates_kind_and_severity(self, repo, engine):
        put_row(engine, **delivered_row())
        urgent = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        claim(repo, urgent)
        repo.complete(
            [urgent], mode="shadow", token="tok-a", delivered=True, now=T0, quiet=False
        )
        row = get_row(engine)
        assert (row.kind, row.tag, row.severity, row.send_count) == (
            "urgent",
            "fl-urgent",
            "critical",
            2,
        )

    def test_an_undelivered_row_takes_the_planned_kind(self, repo, engine):
        put_row(engine)
        result = claim(repo, push(kind=PushKind.URGENT, severity=Severity.CRITICAL))
        assert decisions(result) == [Decision.SEND]
        row = get_row(engine)
        assert (row.kind, row.tag) == ("urgent", "fl-urgent")

    def test_a_retry_uses_the_kind_stored_on_the_row(self, repo, engine):
        put_row(engine, kind="urgent", tag="fl-urgent")
        planned = push(retry_only=True, kind=PushKind.NOTICE)
        result = claim(repo, planned, completed=False, quiet=True, now=NIGHT)
        assert decisions(result) == [Decision.SEND]
        (claimed,) = result.claimed
        assert (claimed.kind, claimed.intended_tag) == (PushKind.URGENT, "fl-urgent")
        row = get_row(engine)
        assert (row.kind, row.tag, row.claim_token) == ("urgent", "fl-urgent", "tok-a")

    def test_a_retried_notice_row_is_held_in_quiet_hours(self, repo, engine):
        put_row(engine)
        planned = push(retry_only=True, kind=PushKind.URGENT)
        result = claim(repo, planned, completed=False, quiet=True, now=NIGHT)
        assert decisions(result) == [Decision.QUIET_HOURS]
        assert get_row(engine).claim_token is None

    def test_a_retry_with_no_row_is_not_owed(self, repo, engine):
        planned = push(retry_only=True)
        result = claim(repo, planned, completed=False)
        assert decisions(result) == [Decision.DUPLICATE]
        assert all_rows(engine) == []

    def test_a_retry_of_a_delivered_row_is_not_owed(self, repo, engine):
        put_row(engine, **delivered_row())
        result = claim(repo, push(retry_only=True), completed=False)
        assert decisions(result) == [Decision.DUPLICATE]


class TestNewEpisodes:
    def test_a_regression_after_a_missed_dispatch_resets_the_episode(
        self, repo, engine
    ):
        put_row(engine, **delivered_row(bypass_used=True, send_count=3))
        result = claim(repo, push(episode_start=True), run_id="run-2")
        assert decisions(result) == [Decision.SEND]
        row = get_row(engine)
        assert (row.delivered, row.severity, row.bypass_used, row.send_count) == (
            False,
            None,
            False,
            0,
        )
        assert (row.episode_run_id, row.run_id) == ("run-2", "run-2")
        assert row.last_sent_at == T0
        assert row.first_sent_at == T0

    def test_the_same_run_keeps_its_episode(self, repo, engine):
        put_row(engine, **delivered_row())
        result = claim(repo, push(episode_start=True), run_id="run-1")
        assert decisions(result) == [Decision.DUPLICATE]
        assert get_row(engine).send_count == 1

    def test_an_ongoing_finding_does_not_start_an_episode(self, repo, engine):
        put_row(engine, **delivered_row())
        result = claim(repo, push(episode_start=False), run_id="run-2")
        assert decisions(result) == [Decision.DUPLICATE]
        assert get_row(engine).episode_run_id == "run-1"

    def test_a_closed_row_reopens_after_the_cooldown(self, repo, engine):
        sent = T0
        put_row(
            engine, **delivered_row(sent, is_open=False, severity=None, delivered=False)
        )
        later = sent + RENOTIFY_COOLDOWN
        result = claim(
            repo,
            push(episode_start=True),
            run_id="run-3",
            evaluated_at=later,
            now=later,
        )
        assert decisions(result) == [Decision.SEND]
        row = get_row(engine)
        assert (row.is_open, row.delivered, row.episode_run_id) == (
            True,
            False,
            "run-3",
        )

    def test_a_closed_row_inside_the_cooldown_stays_closed(self, repo, engine):
        put_row(engine, **delivered_row(is_open=False, severity=None, delivered=False))
        before = get_row(engine)
        soon = T0 + RENOTIFY_COOLDOWN - timedelta(seconds=1)
        result = claim(
            repo,
            push(episode_start=True),
            run_id="run-3",
            evaluated_at=soon,
            now=soon,
        )
        assert decisions(result) == [Decision.COOLDOWN]
        assert get_row(engine) == before

    def test_an_urgent_recurrence_ignores_the_cooldown(self, repo, engine):
        put_row(engine, **delivered_row(is_open=False, severity=None, delivered=False))
        soon = T0 + minutes(10)
        result = claim(
            repo,
            push(kind=PushKind.URGENT, severity=Severity.CRITICAL, episode_start=True),
            run_id="run-3",
            evaluated_at=soon,
            now=soon,
        )
        assert decisions(result) == [Decision.SEND]


class TestHeldPushes:
    """A held push is not claimed; its row must re-derive on the next scan."""

    def test_a_held_new_subject_writes_no_row(self, repo, engine):
        result = claim(repo, push(episode_start=True), quiet=True, now=NIGHT)
        assert decisions(result) == [Decision.QUIET_HOURS]
        assert result.claimed == ()
        assert all_rows(engine) == []

    def test_a_held_regression_on_an_open_row_records_the_reopen(self, repo, engine):
        put_row(engine, **delivered_row(bypass_used=True, send_count=2))
        result = claim(
            repo, push(episode_start=True), run_id="run-2", quiet=True, now=NIGHT
        )
        assert decisions(result) == [Decision.QUIET_HOURS]
        row = get_row(engine)
        assert (row.is_open, row.delivered, row.severity) == (True, False, None)
        assert (row.bypass_used, row.send_count) == (False, 0)
        assert (row.episode_run_id, row.run_id, row.updated_at) == (
            "run-2",
            "run-2",
            NIGHT,
        )
        assert (row.claim_token, row.claimed_at) == (None, None)

    def test_a_held_ops_push_on_an_open_row_of_another_run_records_nothing(
        self, repo, engine
    ):
        put_row(
            engine,
            **delivered_row(
                subject_key=OPS_FAILED,
                kind="ops",
                tag="fl-ops",
                rule_id=None,
                severity=None,
                delivered=False,
                send_count=0,
            ),
        )
        before = get_row(engine, OPS_FAILED)
        result = claim(
            repo,
            push(OPS_FAILED, PushKind.OPS),
            run_id="run-2",
            evaluated_at=NIGHT,
            quiet=True,
            now=NIGHT,
            completed=False,
        )
        assert decisions(result) == [Decision.QUIET_HOURS]
        assert result.claimed == ()
        assert get_row(engine, OPS_FAILED) == before

    def test_a_delivered_ops_push_on_an_open_row_is_a_duplicate_at_night(
        self, repo, engine
    ):
        put_row(
            engine,
            **delivered_row(
                subject_key=OPS_FAILED,
                kind="ops",
                tag="fl-ops",
                rule_id=None,
                severity=None,
            ),
        )
        before = get_row(engine, OPS_FAILED)
        result = claim(
            repo,
            push(OPS_FAILED, PushKind.OPS),
            run_id="run-2",
            evaluated_at=NIGHT,
            quiet=True,
            now=NIGHT,
            completed=False,
        )
        assert decisions(result) == [Decision.DUPLICATE]
        assert get_row(engine, OPS_FAILED) == before

    def test_the_same_run_redispatched_at_night_writes_nothing(self, repo, engine):
        put_row(engine, **delivered_row())
        claim(repo, push(episode_start=True), run_id="run-2", quiet=True, now=NIGHT)
        after_first = get_row(engine)
        result = claim(
            repo, push(episode_start=True), run_id="run-2", quiet=True, now=NIGHT
        )
        assert decisions(result) == [Decision.QUIET_HOURS]
        assert get_row(engine) == after_first

    def test_the_held_reopen_is_sent_by_the_next_scan(self, repo, engine):
        put_row(engine, **delivered_row())
        claim(repo, push(episode_start=True), run_id="run-2", quiet=True, now=NIGHT)
        morning = NIGHT + timedelta(hours=7)
        # The finding is ongoing on the next scan: only the stored reopen owes it.
        result = claim(
            repo,
            push(episode_start=False),
            run_id="run-3",
            evaluated_at=morning,
            now=morning,
        )
        assert decisions(result) == [Decision.SEND]

    def test_a_held_regression_on_a_closed_row_leaves_it_closed(self, repo, engine):
        put_row(engine, **delivered_row(is_open=False, severity=None, delivered=False))
        before = get_row(engine)
        later = T0 + timedelta(hours=8)
        result = claim(
            repo,
            push(episode_start=True),
            run_id="run-2",
            quiet=True,
            evaluated_at=later,
            now=later,
        )
        assert decisions(result) == [Decision.QUIET_HOURS]
        assert get_row(engine) == before

    def test_a_used_bypass_holds_a_later_escalation_unchanged(self, repo, engine):
        put_row(
            engine,
            **delivered_row(kind="urgent", tag="fl-urgent", bypass_used=True),
        )
        before = get_row(engine)
        result = claim(
            repo,
            push(kind=PushKind.URGENT, severity=Severity.CRITICAL),
            quiet=True,
            now=NIGHT,
        )
        assert decisions(result) == [Decision.BYPASS_USED]
        assert get_row(engine) == before

    def test_a_held_status_is_dropped_without_a_row(self, repo, engine):
        result = claim(
            repo,
            push(STATUS, PushKind.STATUS),
            quiet=True,
            evaluated_at=NIGHT,
            now=NIGHT,
        )
        assert decisions(result) == [Decision.QUIET_HOURS]
        assert all_rows(engine) == []

    def test_an_urgent_push_bypasses_quiet_hours(self, repo, engine):
        result = claim(
            repo,
            push(kind=PushKind.URGENT, severity=Severity.CRITICAL, episode_start=True),
            quiet=True,
            now=NIGHT,
        )
        assert decisions(result) == [Decision.SEND]
        assert get_row(engine).claim_token == "tok-a"


class TestStatusFreshness:
    def test_a_late_status_is_stale_but_other_pushes_proceed(self, repo, engine):
        late = T0 + minutes(61)
        result = claim(
            repo,
            push(STATUS, PushKind.STATUS),
            push(ISSUE, episode_start=True),
            evaluated_at=T0,
            now=late,
        )
        assert decisions(result) == [Decision.STALE, Decision.SEND]
        assert not result.stale
        assert get_row(engine, STATUS) is None
        assert get_row(engine, ISSUE) is not None

    def test_a_status_exactly_one_interval_old_is_fresh(self, repo):
        result = claim(
            repo, push(STATUS, PushKind.STATUS), evaluated_at=T0, now=T0 + minutes(60)
        )
        assert decisions(result) == [Decision.SEND]

    def test_a_late_report_is_stale(self, repo):
        result = claim(
            repo,
            push("run:run-1", PushKind.REPORT),
            evaluated_at=T0,
            now=T0 + minutes(61),
        )
        assert decisions(result) == [Decision.STALE]

    def test_the_interval_follows_the_policy(self, repo):
        pol = policy(change_scan_minutes=5)
        result = claim(
            repo,
            push(STATUS, PushKind.STATUS),
            evaluated_at=T0,
            now=T0 + minutes(6),
            pol=pol,
        )
        assert decisions(result) == [Decision.STALE]


class TestComplete:
    def test_delivery_is_recorded_and_the_claim_cleared(self, repo, engine):
        claim(repo, push(episode_start=True))
        sent = T0 + minutes(1)
        repo.complete(
            [push()],
            mode="shadow",
            token="tok-a",
            delivered=True,
            now=sent,
            quiet=False,
        )
        row = get_row(engine)
        assert (row.delivered, row.kind, row.tag, row.severity) == (
            True,
            "notice",
            "fl-warning",
            "warning",
        )
        assert (row.first_sent_at, row.last_sent_at, row.last_attempt_at) == (
            sent,
            sent,
            sent,
        )
        assert (row.send_count, row.failed_attempts, row.bypass_used) == (1, 0, False)
        assert (row.claim_token, row.claimed_at, row.updated_at) == (None, None, sent)

    def test_the_first_send_time_is_kept(self, repo, engine):
        put_row(engine, **delivered_row(T0, failed_attempts=2))
        later = T0 + minutes(30)
        urgent = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        claim(repo, urgent, now=later, evaluated_at=later)
        repo.complete(
            [urgent],
            mode="shadow",
            token="tok-a",
            delivered=True,
            now=later,
            quiet=False,
        )
        row = get_row(engine)
        assert (row.first_sent_at, row.last_sent_at) == (T0, later)
        assert (row.send_count, row.failed_attempts) == (2, 0)

    def test_a_quiet_hours_send_uses_the_bypass(self, repo, engine):
        urgent = push(
            kind=PushKind.URGENT, severity=Severity.CRITICAL, episode_start=True
        )
        claim(repo, urgent, quiet=True, now=NIGHT)
        repo.complete(
            [urgent],
            mode="shadow",
            token="tok-a",
            delivered=True,
            now=NIGHT,
            quiet=True,
        )
        assert get_row(engine).bypass_used

    def test_a_daytime_send_leaves_the_bypass_untouched(self, repo, engine):
        put_row(engine, **delivered_row(bypass_used=True))
        urgent = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        claim(repo, urgent)
        repo.complete(
            [urgent], mode="shadow", token="tok-a", delivered=True, now=T0, quiet=False
        )
        assert get_row(engine).bypass_used

    def test_a_failure_counts_and_the_next_dispatch_retries(self, repo, engine):
        claim(repo, push(episode_start=True))
        failed_at = T0 + minutes(1)
        repo.complete(
            [push()],
            mode="shadow",
            token="tok-a",
            delivered=False,
            now=failed_at,
            quiet=False,
        )
        row = get_row(engine)
        assert (row.delivered, row.failed_attempts, row.send_count) == (False, 1, 0)
        assert row.last_attempt_at == failed_at
        assert (row.claim_token, row.claimed_at) == (None, None)
        again = claim(repo, push(), token="tok-b", now=failed_at, evaluated_at=T0)
        assert decisions(again) == [Decision.SEND]

    def test_a_chunk_completes_every_item(self, repo, engine):
        items = [push(ISSUE, episode_start=True), push(ISSUE_B, episode_start=True)]
        claim(repo, *items)
        repo.complete(
            items, mode="shadow", token="tok-a", delivered=True, now=T0, quiet=False
        )
        assert [r.delivered for r in all_rows(engine)] == [True, True]

    def test_a_foreign_token_changes_nothing(self, repo, engine):
        claim(repo, push())
        before = get_row(engine)
        repo.complete(
            [push()], mode="shadow", token="other", delivered=True, now=T0, quiet=False
        )
        assert get_row(engine) == before

    def test_only_the_named_mode_is_completed(self, repo, engine):
        claim(repo, push(), mode="shadow", token="tok-a")
        claim(repo, push(), mode="production", token="tok-a")
        repo.complete(
            [push()], mode="shadow", token="tok-a", delivered=True, now=T0, quiet=False
        )
        assert get_row(engine, mode="shadow").delivered
        assert not get_row(engine, mode="production").delivered


class TestRelease:
    def test_a_release_clears_the_claim_without_counting_a_failure(self, repo, engine):
        claim(repo, push())
        repo.release([push()], mode="shadow", token="tok-a")
        row = get_row(engine)
        assert (row.claim_token, row.claimed_at) == (None, None)
        assert (row.failed_attempts, row.delivered) == (0, False)

    def test_a_released_push_is_claimable_again_at_once(self, repo):
        claim(repo, push())
        repo.release([push()], mode="shadow", token="tok-a")
        result = claim(repo, push(), token="tok-b")
        assert decisions(result) == [Decision.SEND]

    def test_a_foreign_token_releases_nothing(self, repo, engine):
        claim(repo, push())
        repo.release([push()], mode="shadow", token="other")
        assert get_row(engine).claim_token == "tok-a"


class TestClose:
    def test_an_issue_no_longer_open_is_closed(self, repo, engine):
        put_row(engine, **delivered_row(bypass_used=True))
        later = T0 + minutes(5)
        claim(repo, push(ISSUE_B), run_id="run-2", evaluated_at=later, now=later)
        row = get_row(engine)
        assert (row.is_open, row.delivered, row.severity, row.bypass_used) == (
            False,
            False,
            None,
            False,
        )
        assert (row.run_id, row.evaluated_at, row.updated_at) == ("run-2", later, later)
        assert row.last_sent_at == T0

    def test_an_open_issue_stays_open(self, repo, engine):
        put_row(engine, **delivered_row())
        claim(repo, push(), run_id="run-2", evaluated_at=T0 + minutes(5), now=T0)
        row = get_row(engine)
        assert (row.is_open, row.delivered) == (True, True)

    def test_a_failed_run_closes_nothing(self, repo, engine):
        put_row(engine, **delivered_row())
        put_row(engine, subject_key=RUN_FAILED, kind="urgent", tag="fl-urgent")
        later = T0 + minutes(5)
        claim(repo, run_id="run-2", evaluated_at=later, now=later, completed=False)
        assert get_row(engine).is_open
        assert get_row(engine, RUN_FAILED).is_open

    def test_a_completed_run_closes_the_failed_run_episode(self, repo, engine):
        put_row(engine, subject_key=RUN_FAILED, kind="urgent", tag="fl-urgent")
        put_row(engine, subject_key=OPS_FAILED, kind="ops", tag="fl-ops", rule_id=None)
        later = T0 + minutes(5)
        claim(repo, run_id="run-2", evaluated_at=later, now=later)
        assert not get_row(engine, RUN_FAILED).is_open
        assert not get_row(engine, OPS_FAILED).is_open

    def test_an_incomplete_run_keeps_the_incomplete_episode_open(self, repo, engine):
        put_row(
            engine, subject_key=OPS_INCOMPLETE, kind="ops", tag="fl-ops", rule_id=None
        )
        later = T0 + minutes(5)
        claim(repo, run_id="run-2", evaluated_at=later, now=later, incomplete=True)
        assert get_row(engine, OPS_INCOMPLETE).is_open

    def test_a_run_that_is_not_incomplete_closes_it(self, repo, engine):
        put_row(
            engine, subject_key=OPS_INCOMPLETE, kind="ops", tag="fl-ops", rule_id=None
        )
        later = T0 + minutes(5)
        claim(repo, run_id="run-2", evaluated_at=later, now=later)
        assert not get_row(engine, OPS_INCOMPLETE).is_open

    def test_only_the_dispatched_mode_is_closed(self, repo, engine):
        put_row(engine, mode="shadow")
        put_row(engine, mode="production")
        later = T0 + minutes(5)
        claim(repo, run_id="run-2", evaluated_at=later, now=later, mode="shadow")
        assert not get_row(engine, mode="shadow").is_open
        assert get_row(engine, mode="production").is_open

    def test_a_run_status_row_is_never_touched_by_a_close(self, repo, engine):
        put_row(
            engine,
            subject_key=STATUS,
            kind="status",
            tag="fl-status",
            is_open=False,
            rule_id=None,
        )
        before = get_row(engine, STATUS)
        later = T0 + minutes(5)
        claim(repo, run_id="run-2", evaluated_at=later, now=later)
        assert get_row(engine, STATUS) == before

    def test_a_recurrence_after_the_cooldown_notifies_again(self, repo, engine):
        put_row(engine, **delivered_row())
        resolved = T0 + minutes(5)
        claim(repo, run_id="run-2", evaluated_at=resolved, now=resolved)
        back = T0 + RENOTIFY_COOLDOWN + minutes(5)
        result = claim(
            repo,
            push(episode_start=True),
            run_id="run-3",
            evaluated_at=back,
            now=back,
        )
        assert decisions(result) == [Decision.SEND]

    def test_a_close_keeps_the_claim_of_a_send_in_flight(self, repo, engine):
        claim(repo, push(episode_start=True), token="tok-a")
        later = T0 + minutes(5)
        claim(repo, run_id="run-2", evaluated_at=later, now=later, token="tok-b")
        row = get_row(engine)
        assert (row.is_open, row.claim_token) == (False, "tok-a")


class TestLockRetries:
    """T-db-6: a busy database is retried up to ``CLAIM_LOCK_RETRIES`` times."""

    @staticmethod
    @contextmanager
    def held_write_lock(engine):
        holder = sqlite3.connect(
            engine.url.database, isolation_level=None, check_same_thread=False
        )
        holder.execute("BEGIN IMMEDIATE")
        try:
            yield holder
        finally:
            if holder.in_transaction:
                holder.execute("ROLLBACK")
            holder.close()

    def test_a_lock_released_within_the_retries_lets_the_claim_through(self, engine):
        sleeps: list[float] = []
        with self.held_write_lock(engine) as holder:

            def release_then_record(seconds: float) -> None:
                sleeps.append(seconds)
                if holder.in_transaction:
                    holder.execute("ROLLBACK")

            repo = NotificationStateRepository(
                engine, lock_timeout_ms=20, sleep=release_then_record
            )
            result = claim(repo, push(episode_start=True))
        assert decisions(result) == [Decision.SEND]
        assert len(sleeps) == 1
        assert get_row(engine).claim_token == "tok-a"

    def test_a_lock_held_through_every_retry_raises_a_database_error(self, engine):
        sleeps: list[float] = []
        repo = NotificationStateRepository(
            engine, lock_timeout_ms=20, sleep=sleeps.append
        )
        plan = make_plan(push(episode_start=True))
        pol = policy()
        with (
            self.held_write_lock(engine),
            pytest.raises(NotificationStateBusyError) as info,
        ):
            repo.claim(
                plan,
                mode="shadow",
                token="tok-a",
                run_id="run-1",
                evaluated_at=T0,
                now=T0,
                quiet=False,
                policy=pol,
            )
        assert isinstance(info.value, DatabaseError)
        assert len(sleeps) == CLAIM_LOCK_RETRIES
        assert sleeps == sorted(sleeps)
        assert all_rows(engine) == []
        assert get_watermark(engine) is None

    def test_a_permanent_fault_is_not_retried(self, engine):
        sleeps: list[float] = []
        repo = NotificationStateRepository(engine, sleep=sleeps.append)
        plan = make_plan(push())
        pol = policy()

        def fail(_conn, _cursor, statement, parameters, _context, _executemany):
            if statement == "BEGIN IMMEDIATE":
                orig = sqlite3.OperationalError("simulated")
                orig.sqlite_errorcode = sqlite3.SQLITE_READONLY
                raise OperationalError(statement, parameters, orig)

        event.listen(engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(OperationalError):
                repo.claim(
                    plan,
                    mode="shadow",
                    token="tok-a",
                    run_id="run-1",
                    evaluated_at=T0,
                    now=T0,
                    quiet=False,
                    policy=pol,
                )
        finally:
            event.remove(engine, "before_cursor_execute", fail)
        assert sleeps == []

    def test_complete_retries_a_lock_released_within_the_retries(self, engine):
        sleeps: list[float] = []
        claimer = NotificationStateRepository(engine, sleep=lambda _s: None)
        claim(claimer, push(episode_start=True))
        with self.held_write_lock(engine) as holder:

            def release_then_record(seconds: float) -> None:
                sleeps.append(seconds)
                if holder.in_transaction:
                    holder.execute("ROLLBACK")

            repo = NotificationStateRepository(
                engine, lock_timeout_ms=20, sleep=release_then_record
            )
            repo.complete(
                [push()],
                mode="shadow",
                token="tok-a",
                delivered=True,
                now=T0,
                quiet=False,
            )
        assert len(sleeps) == 1
        row = get_row(engine)
        assert (row.delivered, row.claim_token) == (True, None)

    def test_release_retries_a_lock_released_within_the_retries(self, engine):
        sleeps: list[float] = []
        claimer = NotificationStateRepository(engine, sleep=lambda _s: None)
        claim(claimer, push(episode_start=True))
        with self.held_write_lock(engine) as holder:

            def release_then_record(seconds: float) -> None:
                sleeps.append(seconds)
                if holder.in_transaction:
                    holder.execute("ROLLBACK")

            repo = NotificationStateRepository(
                engine, lock_timeout_ms=20, sleep=release_then_record
            )
            repo.release([push()], mode="shadow", token="tok-a")
        assert len(sleeps) == 1
        assert get_row(engine).claim_token is None

    def test_a_non_lock_fault_in_complete_is_not_retried(self, engine):
        sleeps: list[float] = []
        claimer = NotificationStateRepository(engine, sleep=lambda _s: None)
        claim(claimer, push(episode_start=True))
        repo = NotificationStateRepository(engine, sleep=sleeps.append)
        pushes = [push()]

        def fail(_conn, _cursor, statement, parameters, _context, _executemany):
            if statement == "BEGIN IMMEDIATE":
                orig = sqlite3.OperationalError("simulated")
                orig.sqlite_errorcode = sqlite3.SQLITE_READONLY
                raise OperationalError(statement, parameters, orig)

        event.listen(engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(OperationalError):
                repo.complete(
                    pushes,
                    mode="shadow",
                    token="tok-a",
                    delivered=True,
                    now=T0,
                    quiet=False,
                )
        finally:
            event.remove(engine, "before_cursor_execute", fail)
        assert sleeps == []

    def test_complete_refuses_a_naive_clock(self, repo):
        claim(repo, push(episode_start=True))
        pushes = [push()]
        naive = T0.replace(tzinfo=None)
        with pytest.raises(ValidationError, match="timezone-aware"):
            repo.complete(
                pushes,
                mode="shadow",
                token="tok-a",
                delivered=True,
                now=naive,
                quiet=False,
            )

    def test_complete_reports_busy_when_the_lock_stays_held(self, engine):
        repo = NotificationStateRepository(
            engine, lock_timeout_ms=20, sleep=lambda _s: None
        )
        claim(repo, push(episode_start=True))
        pushes = [push()]
        with (
            self.held_write_lock(engine),
            pytest.raises(NotificationStateBusyError),
        ):
            repo.complete(
                pushes,
                mode="shadow",
                token="tok-a",
                delivered=True,
                now=T0,
                quiet=False,
            )
        assert not get_row(engine).delivered


class TestConcurrency:
    """Two connections racing the same claim: exactly one wins (D11)."""

    def test_racing_claims_have_one_winner(self, tmp_path):
        engine_a = make_engine(tmp_path)
        engine_b = create_sqlite_engine(tmp_path / "fl.db")
        try:
            repos = [
                NotificationStateRepository(engine_a),
                NotificationStateRepository(engine_b),
            ]
            barrier = threading.Barrier(2)
            outcomes: dict[str, Decision] = {}
            errors: list[BaseException] = []

            def race(index: int) -> None:
                try:
                    barrier.wait(timeout=5)
                    result = claim(
                        repos[index],
                        push(episode_start=True),
                        token=f"tok-{index}",
                    )
                    outcomes[f"tok-{index}"] = result.decisions[0].decision
                except (DatabaseError, OperationalError) as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=race, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            assert errors == []
            assert sorted(outcomes.values(), key=str) == [
                Decision.IN_FLIGHT,
                Decision.SEND,
            ]
            winner = next(t for t, d in outcomes.items() if d is Decision.SEND)
            assert get_row(engine_a).claim_token == winner
            assert len(all_rows(engine_a)) == 1
        finally:
            engine_a.dispose()
            engine_b.dispose()


def test_effective_push_is_a_copy_not_a_mutation(repo, engine):
    put_row(engine, kind="urgent", tag="fl-urgent")
    planned = push(retry_only=True, kind=PushKind.NOTICE)
    original = replace(planned)
    claim(repo, planned, completed=False)
    assert planned == original


class TestEscalatedFlag:
    """The claim sets ``escalated`` from the stored row, not from the plan."""

    def test_a_claimed_escalation_is_marked(self, repo, engine):
        put_row(engine, **delivered_row())
        urgent = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        result = claim(repo, urgent)
        assert [d.push.escalated for d in result.decisions] == [True]

    def test_a_severity_rise_is_not_kind_only(self, repo, engine):
        put_row(engine, **delivered_row(severity="warning"))
        urgent = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        result = claim(repo, urgent)
        assert [d.push.kind_only_escalation for d in result.decisions] == [False]

    def test_a_kind_only_rise_is_marked_kind_only(self, repo, engine):
        put_row(engine, **delivered_row(severity="critical"))
        urgent = push(kind=PushKind.URGENT, severity=Severity.CRITICAL)
        result = claim(repo, urgent)
        assert [d.push.escalated for d in result.decisions] == [True]
        assert [d.push.kind_only_escalation for d in result.decisions] == [True]

    def test_a_first_notice_is_not_marked(self, repo):
        result = claim(repo, push())
        assert [d.push.escalated for d in result.decisions] == [False]

    def test_an_undelivered_row_is_not_marked(self, repo, engine):
        put_row(engine)
        result = claim(repo, push(kind=PushKind.URGENT, severity=Severity.CRITICAL))
        assert [d.push.escalated for d in result.decisions] == [False]

    def test_a_new_episode_is_not_marked(self, repo, engine):
        put_row(engine, **delivered_row(is_open=False))
        later = T0 + RENOTIFY_COOLDOWN + minutes(1)
        result = claim(
            repo,
            push(kind=PushKind.URGENT, severity=Severity.CRITICAL),
            now=later,
            evaluated_at=later,
        )
        assert decisions(result) == [Decision.SEND]
        assert [d.push.escalated for d in result.decisions] == [False]


class TestWatermarkRunId:
    def test_none_before_the_first_dispatch(self, repo):
        assert repo.watermark_run_id() is None

    def test_the_run_that_holds_the_watermark(self, repo):
        claim(repo, push(), run_id="run-7")
        assert repo.watermark_run_id() == "run-7"


class TestLoadNameRows:
    def test_reads_only_the_name_columns(self, engine):
        seed_registry(engine)
        people, passengers = load_name_rows(engine)
        assert ("sam", "Sam", "child") in people
        assert ("morgan", "Morgan", "parent") in people
        assert any(person_id == "ext-e" for person_id, _ in passengers)
        assert all(len(row) == 3 for row in people)
        assert all(len(row) == 2 for row in passengers)

    def test_an_empty_database_gives_empty_lists(self, engine):
        assert load_name_rows(engine) == ([], [])
