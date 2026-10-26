"""Every finding the acceptance matrix raises has a derivable subject (T-subj-1).

ADR 0008 D7 derives a finding's subject from its entity keys, so a change to
the key grammar would otherwise leave findings unresolved (counted as
imminent) without any test failing. This module replays the engine scenarios
of ``test_acceptance_matrix`` and resolves every finding they produce.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest
import test_acceptance_matrix as matrix
from fl_factories import FRIDAY_END, FRIDAY_START

from family_logistics.domain.context import EngineSettings
from family_logistics.domain.engine import assess
from family_logistics.domain.notification_subjects import (
    Resolution,
    RideIndex,
    resolve_subject,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from family_logistics.domain.engine import AssessmentHistory, AssessmentResult
    from family_logistics.domain.events import AssessmentInput

pytestmark = pytest.mark.unit

Run = tuple["AssessmentInput", "AssessmentHistory | None", "AssessmentResult"]


def _scenarios() -> list[Callable[[], None]]:
    """Return the matrix scenarios that are not skipped."""
    return [
        function
        for name, function in inspect.getmembers(matrix, inspect.isfunction)
        if name.startswith("test_")
        and not any(mark.name == "skip" for mark in getattr(function, "pytestmark", []))
    ]


@pytest.fixture
def engine_runs(monkeypatch: pytest.MonkeyPatch) -> list[Run]:
    """Record every ``assess`` call the matrix scenarios make."""
    runs: list[Run] = []

    def recording(
        inp: AssessmentInput, provider: Any, **kwargs: Any
    ) -> AssessmentResult:
        result = assess(inp, provider, **kwargs)
        runs.append((inp, kwargs.get("history"), result))
        return result

    monkeypatch.setattr(matrix, "assess", recording)
    return runs


def test_the_acceptance_matrix_has_zero_unresolved_findings(engine_runs):
    """T-subj-1: no finding of any matrix scenario is ``UNRESOLVED``."""
    for scenario in _scenarios():
        scenario()
    tolerance = EngineSettings().pickup_tolerance
    unresolved: list[str] = []
    resolved_rules: set[str] = set()
    for inp, history, result in engine_runs:
        previous = history.previous_trips if history is not None else ()
        parked = history.unparsed_trip_keys if history is not None else frozenset()
        rides = RideIndex.build(
            current=inp.trips,
            previous=previous,
            unparsed=[(key, FRIDAY_START, FRIDAY_END) for key in parked],
        )
        for tracked in result.findings:
            resolution, _ = resolve_subject(
                tracked.finding,
                rides=rides,
                timelines=result.timelines,
                pickup_tolerance=tolerance,
            )
            if resolution is Resolution.UNRESOLVED:
                unresolved.append(tracked.finding.rule_id)
            else:
                resolved_rules.add(tracked.finding.rule_id)
    assert engine_runs
    # Not vacuous: ride-level (DRIVER-006) and transition-level (RIDE-005) keys.
    assert {"DRIVER-006", "RIDE-005"} <= resolved_rules
    assert unresolved == []
