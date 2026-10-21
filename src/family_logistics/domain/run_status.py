"""Overall run status (handoff section 7, "Statuses").

Precedence, highest first:

1. ``failed``: the run itself errored.
2. ``attention_required`` when any critical finding exists: a known critical
   problem is never hidden behind a data gap.
3. ``incomplete``: a required source is stale, failed or missing, or any
   finding has severity ``incomplete`` (routing unavailable, unresolved
   address). Zero known conflicts does not make this green.
4. ``attention_required`` for warnings.
5. ``clear`` only when every source is fresh and no finding is open.

Severity is reported separately from status (the card shows counts), so the
precedence choice never hides a finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_logistics.domain.models import Finding, RunStatus, Severity

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    """Freshness of one input source (iCloud, HA snapshot, routing).

    Attributes:
        name: Source name, e.g. ``icloud``.
        last_success: Time of the last successful fetch, None if never.
        max_age: Oldest acceptable successful fetch.
        last_attempt_failed: Whether the most recent attempt failed.
    """

    name: str
    last_success: datetime | None
    max_age: timedelta
    last_attempt_failed: bool = False

    def is_usable(self, now: datetime) -> bool:
        """Report whether this source can support a green result.

        A failed latest attempt does not by itself make the source unusable
        while the last success is still within ``max_age``; a network failure
        is never read as an empty calendar.

        Args:
            now: Current time (timezone-aware).

        Returns:
            True when a success exists within ``max_age``.
        """
        if self.last_success is None:
            return False
        return now - self.last_success <= self.max_age


def compute_run_status(
    findings: Iterable[Finding],
    sources: Iterable[SourceFreshness],
    *,
    now: datetime,
    runtime_error: bool = False,
) -> RunStatus:
    """Compute the family-wide status for one run.

    Args:
        findings: Open findings after this run.
        sources: Freshness of every required source.
        now: Current time (timezone-aware).
        runtime_error: Whether the run raised an unhandled error.

    Returns:
        The run status.
    """
    if runtime_error:
        return RunStatus.FAILED
    severities = {f.severity for f in findings}
    if Severity.CRITICAL in severities:
        return RunStatus.ATTENTION_REQUIRED
    source_list = list(sources)
    sources_ok = bool(source_list) and all(s.is_usable(now) for s in source_list)
    if not sources_ok or Severity.INCOMPLETE in severities:
        return RunStatus.INCOMPLETE
    if Severity.WARNING in severities:
        return RunStatus.ATTENTION_REQUIRED
    return RunStatus.CLEAR
