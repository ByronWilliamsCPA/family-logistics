"""Finding lifecycle across runs: new, ongoing, resolved, regressed.

Findings are matched by their stable ``issue_id`` (rule plus sorted entity
keys), so the same problem keeps its identity and notifications can be
de-duplicated. When several rules or modules raise the same issue in one run,
the most severe copy is kept: critical, then incomplete, then warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from family_logistics.domain.models import Severity

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_logistics.domain.models import Finding

_RANK = {Severity.CRITICAL: 3, Severity.INCOMPLETE: 2, Severity.WARNING: 1}


class Lifecycle(StrEnum):
    """Where an open finding stands relative to earlier runs."""

    NEW = "new"
    ONGOING = "ongoing"
    REGRESSED = "regressed"


@dataclass(frozen=True, slots=True)
class TrackedFinding:
    """An open finding with its lifecycle state.

    Attributes:
        finding: The finding.
        lifecycle: New, ongoing or regressed.
    """

    finding: Finding
    lifecycle: Lifecycle


@dataclass(frozen=True, slots=True)
class LifecycleReport:
    """Open and resolved findings for one run.

    Attributes:
        open: Findings raised this run, most severe first.
        resolved: Findings open last run and absent now.
    """

    open: tuple[TrackedFinding, ...]
    resolved: tuple[Finding, ...]


def deduplicate(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Keep one finding per issue ID, preferring the most severe copy.

    Args:
        findings: Findings in any order, possibly repeated.

    Returns:
        Unique findings, most severe first, then by rule and issue ID.
    """
    best: dict[str, Finding] = {}
    for finding in findings:
        current = best.get(finding.issue_id)
        if current is None or _RANK[finding.severity] > _RANK[current.severity]:
            best[finding.issue_id] = finding
    return tuple(
        sorted(
            best.values(),
            key=lambda f: (-_RANK[f.severity], f.rule_id, f.issue_id),
        )
    )


def track(
    current: Iterable[Finding],
    previous_open: Iterable[Finding],
    previously_resolved_ids: frozenset[str] | None = None,
) -> LifecycleReport:
    """Classify this run's findings against the previous run.

    Args:
        current: Findings raised this run.
        previous_open: Findings open after the previous run.
        previously_resolved_ids: Issue IDs resolved in an earlier run; a
            finding that returns is ``regressed``.

    Returns:
        The lifecycle report.
    """
    now = deduplicate(current)
    before = {f.issue_id: f for f in previous_open}
    resolved_before = previously_resolved_ids or frozenset[str]()
    now_ids = {f.issue_id for f in now}
    tracked = tuple(
        TrackedFinding(
            f,
            Lifecycle.ONGOING
            if f.issue_id in before
            else Lifecycle.REGRESSED
            if f.issue_id in resolved_before
            else Lifecycle.NEW,
        )
        for f in now
    )
    resolved = tuple(f for i, f in before.items() if i not in now_ids)
    return LifecycleReport(tracked, resolved)
