"""Engine run status, history, lifecycle and configuration."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fl_engine import NOW, activity, assessment, at, routes
from fl_factories import notes, trip

from family_logistics.core.config import Settings
from family_logistics.domain.context import EngineSettings
from family_logistics.domain.engine import (
    AssessmentHistory,
    assess,
    failed_result,
    freshness_findings,
)
from family_logistics.domain.lifecycle import Lifecycle, deduplicate, track
from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.rules import make_finding

pytestmark = pytest.mark.unit


def finding(rule_id="RIDE-005", key="k", severity=None):
    return make_finding(rule_id, "msg", (key,), severity=severity)


class TestLifecycle:
    def test_deduplicate_keeps_most_severe(self):
        warning = finding("CAP-014", severity=Severity.WARNING)
        critical = finding("CAP-014", severity=Severity.CRITICAL)
        kept = deduplicate([warning, critical, warning])
        assert [f.severity for f in kept] == [Severity.CRITICAL]

    def test_new_ongoing_regressed_and_resolved(self):
        ongoing, fresh, back, gone = (finding(key=k) for k in "abcd")
        report = track(
            [ongoing, fresh, back],
            [ongoing, gone],
            frozenset({back.issue_id}),
        )
        states = {t.finding.issue_id: t.lifecycle for t in report.open}
        assert states == {
            ongoing.issue_id: Lifecycle.ONGOING,
            fresh.issue_id: Lifecycle.NEW,
            back.issue_id: Lifecycle.REGRESSED,
        }
        assert report.resolved == (gone,)


class TestStatus:
    def test_no_sources_is_incomplete_never_clear(self):
        result = assess(assessment(fresh=False), routes(), now=NOW)
        assert result.run_status is RunStatus.INCOMPLETE
        assert [t.finding.rule_id for t in result.findings] == ["DATA-011"]

    def test_stale_source_is_data_011(self):
        inp = assessment()
        late = NOW + timedelta(hours=3)
        flagged = freshness_findings(inp, EngineSettings(), late)
        assert [f.entity_keys for f in flagged] == [("source:icloud",)]

    def test_failed_run_keeps_previous_findings_open(self):
        previous = finding()
        result = failed_result(NOW, "collector crashed", [previous])
        assert result.run_status is RunStatus.FAILED
        assert [t.finding for t in result.findings] == [previous]
        assert result.error == "collector crashed"

    def test_counts_include_every_severity(self):
        result = assess(assessment(fresh=False), routes(), now=NOW)
        assert result.counts() == {"critical": 0, "warning": 0, "incomplete": 1}


class TestHistory:
    def test_lost_external_ride_is_pass_003(self):
        with_e = trip(
            notes(
                external="Passenger E",
                destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
            ),
            start=at(17),
            end=at(17, 30),
        )
        without_e = trip(notes(), start=at(17), end=at(17, 30))
        history = AssessmentHistory(previous_trips=(with_e,))
        result = assess(
            assessment(trips=(without_e,)), routes(), now=NOW, history=history
        )
        lost = [t.finding for t in result.findings if t.finding.rule_id == "PASS-003"]
        assert [f.severity for f in lost] == [Severity.CRITICAL]

    def test_parse_findings_join_the_run(self):
        parse_error = finding("PARSE-001")
        history = AssessmentHistory(parse_findings=(parse_error,))
        result = assess(assessment(), routes(), now=NOW, history=history)
        assert result.run_status is RunStatus.ATTENTION_REQUIRED
        assert parse_error in [t.finding for t in result.findings]

    def test_previous_findings_resolve_when_fixed(self):
        lesson = activity("lesson", ("sam",), "studio", at(16), at(17))
        first = assess(assessment(activities=(lesson,)), routes(), now=NOW)
        missing = [t.finding for t in first.findings]
        assert {f.rule_id for f in missing} == {"RIDE-005"}
        inp = dataclasses.replace(assessment(), previous_findings=tuple(missing))
        fixed = assess(inp, routes(), now=NOW)
        assert set(fixed.resolved) == set(missing)


def test_engine_settings_from_config():
    settings = Settings(
        same_location_gap_minutes=45,
        travel_buffer_minutes=12,
        stop_buffer_minutes=3,
        imminent_window_minutes=60,
    )
    engine = EngineSettings.from_config(settings)
    assert engine.same_location_gap == timedelta(minutes=45)
    assert engine.travel_buffer == timedelta(minutes=12)
    assert engine.stop_buffer == timedelta(minutes=3)
    assert engine.imminent_window == timedelta(minutes=60)
    assert engine.max_age_for("ha_snapshot") == timedelta(hours=26)
    assert engine.max_age_for("other") == timedelta(hours=2)
