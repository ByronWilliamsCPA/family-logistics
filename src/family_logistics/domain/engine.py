"""Deterministic assessment engine (ADR 0001).

``assess`` turns one ``AssessmentInput`` into findings, a run status and the
per-child day timelines. It performs no I/O: travel times come from the
injected ``RouteProvider``, which may answer None (unknown) for any leg.

The engine is the only authority on findings. Status precedence is in
``domain.run_status``: a critical finding is attention required; a stale or
missing source, or any incomplete finding, makes the run incomplete; a run
that raised is failed. Nothing incomplete or failed is ever reported as clear.

Rule modules:

- ``timeline``: stays and required moves per monitored child.
- ``coverage``: RIDE-005, MODE-015, SCHOOL-009 (and ROUTE-010 for unregistered
  places).
- ``driver``: DRIVER-006 (and ROUTE-010 for unknown driver travel times).
- ``feasibility``: DEST-004, TIME-007, ORDER-012, ROUTE-010, CAP-014, CONF-008.
- ``change_impact``: PASS-003 and CONF-008 against the previous run's rides.
- source freshness: DATA-011.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from family_logistics.domain.change_impact import analyze_change_impact
from family_logistics.domain.context import AssessmentContext, EngineSettings
from family_logistics.domain.coverage import coverage_findings
from family_logistics.domain.driver import driver_findings
from family_logistics.domain.feasibility import trip_findings
from family_logistics.domain.lifecycle import (
    Lifecycle,
    LifecycleReport,
    TrackedFinding,
    track,
)
from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.rules import make_finding
from family_logistics.domain.run_status import SourceFreshness, compute_run_status
from family_logistics.domain.timeline import build_timelines

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from family_logistics.adapters.routing.base import RouteProvider
    from family_logistics.domain.events import AssessmentInput
    from family_logistics.domain.models import Finding, SourceEventKey, Trip
    from family_logistics.domain.timeline import DayTimeline, Stay

RULES_VERSION = "m2.1"
"""Bumped whenever rule semantics change, so stored runs record what judged them."""


@dataclass(frozen=True, slots=True)
class AssessmentHistory:
    """State carried from earlier runs and from parsing.

    Attributes:
        previous_trips: Rides accepted in the previous run (change impact).
        unparsed_trip_keys: Rides whose notes failed to parse this run; their
            previous legs are carried forward, never reported lost.
        parse_findings: Parser findings for this run (PARSE-001, NAME-002, ...).
        resolved_issue_ids: Issue IDs resolved in earlier runs (regressions).
    """

    previous_trips: tuple[Trip, ...] = ()
    unparsed_trip_keys: frozenset[SourceEventKey] = field(default_factory=frozenset)
    parse_findings: tuple[Finding, ...] = ()
    resolved_issue_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    """Outcome of one assessment run.

    Attributes:
        run_status: Clear, attention required, incomplete or failed.
        findings: Open findings with lifecycle, most severe first.
        resolved: Findings open last run and resolved now.
        timelines: Day timelines of monitored children.
        evaluated_at: Evaluation time (UTC).
        rules_version: Version of the rule semantics.
        error: Why the run failed, for failed runs only.
    """

    run_status: RunStatus
    findings: tuple[TrackedFinding, ...]
    resolved: tuple[Finding, ...]
    timelines: tuple[DayTimeline, ...]
    evaluated_at: datetime
    rules_version: str = RULES_VERSION
    error: str | None = None

    def counts(self) -> dict[str, int]:
        """Count open findings per severity.

        Returns:
            Severity value to count, including zeros.
        """
        tally = Counter(t.finding.severity.value for t in self.findings)
        return {s.value: tally.get(s.value, 0) for s in Severity}


def _sources(inp: AssessmentInput, settings: EngineSettings) -> list[SourceFreshness]:
    """Describe every reported source with its staleness limit."""
    return [
        SourceFreshness(name, last, settings.max_age_for(name))
        for name, last in inp.source_freshness
    ]


def freshness_findings(
    inp: AssessmentInput, settings: EngineSettings, now: datetime
) -> list[Finding]:
    """Flag stale, failed or missing sources (DATA-011).

    Args:
        inp: Assessment input.
        settings: Engine thresholds.
        now: Evaluation time (UTC).

    Returns:
        DATA-011 findings (incomplete).
    """
    if not inp.source_freshness:
        return [
            make_finding(
                "DATA-011",
                "No data source reported a successful collection.",
                ("source:none",),
            )
        ]
    return [
        make_finding(
            "DATA-011",
            f"Source {source.name} is stale or has never been collected.",
            (f"source:{source.name}",),
            suggestion="Check the collector and the source's credentials.",
        )
        for source in _sources(inp, settings)
        if not source.is_usable(now)
    ]


def _change_findings(
    ctx: AssessmentContext, history: AssessmentHistory
) -> list[Finding]:
    """Compare previous rides to current rides (PASS-003, CONF-008)."""
    previous = [
        leg
        for trip in history.previous_trips
        if trip.end >= ctx.now
        for leg in trip.legs()
    ]
    current = [leg for trip in ctx.inp.trips for leg in trip.legs()]
    impact = analyze_change_impact(
        previous, current, unparsed_trip_keys=history.unparsed_trip_keys
    )
    return list(impact.findings)


def assess(  # noqa: PLR0913 - settings and history are keyword-only options
    inp: AssessmentInput,
    route_provider: RouteProvider,
    *,
    now: datetime,
    settings: EngineSettings | None = None,
    history: AssessmentHistory | None = None,
) -> AssessmentResult:
    """Run every deterministic rule for one assessment window.

    Args:
        inp: Everything known about the window.
        route_provider: Travel-time source (may answer None).
        now: Evaluation time (UTC). Moves and rides already past are skipped.
        settings: Engine thresholds; defaults when None.
        history: Previous rides, parse results and lifecycle state.

    Returns:
        The assessment result.
    """
    settings = settings or EngineSettings()
    history = history or AssessmentHistory()
    ctx = AssessmentContext.create(inp, settings, route_provider, now)
    timelines = build_timelines(inp, ctx.resolver, settings, ctx.zone)
    stays: dict[str, tuple[Stay, ...]] = {}
    for timeline in timelines:
        stays[timeline.person_id] = (
            *stays.get(timeline.person_id, ()),
            *timeline.stays,
        )
    ctx = dataclasses.replace(ctx, stays=stays)
    findings: list[Finding] = [
        *history.parse_findings,
        *freshness_findings(inp, settings, now),
        *coverage_findings(ctx, timelines),
        *driver_findings(ctx),
        *trip_findings(ctx),
        *_change_findings(ctx, history),
    ]
    report: LifecycleReport = track(
        findings, inp.previous_findings, history.resolved_issue_ids
    )
    status = compute_run_status(
        (t.finding for t in report.open), _sources(inp, settings), now=now
    )
    return AssessmentResult(status, report.open, report.resolved, timelines, now)


def failed_result(
    now: datetime, error: str, previous_open: Iterable[Finding] = ()
) -> AssessmentResult:
    """Build the result of a run that raised before finishing.

    Previous findings stay open (nothing is resolved by a failed run).

    Args:
        now: When the run failed (UTC).
        error: Short error description without secrets or addresses.
        previous_open: Findings open after the previous run.

    Returns:
        A failed result.
    """
    kept = tuple(TrackedFinding(f, Lifecycle.ONGOING) for f in previous_open)
    return AssessmentResult(RunStatus.FAILED, kept, (), (), now, error=error)
