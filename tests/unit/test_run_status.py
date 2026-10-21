"""Run status precedence: failure and missing data never look green."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.rules import make_finding
from family_logistics.domain.run_status import SourceFreshness, compute_run_status

NOW = datetime(2026, 10, 25, 22, 0, tzinfo=UTC)
FRESH = SourceFreshness("icloud", NOW - timedelta(minutes=10), timedelta(hours=2))
STALE = SourceFreshness("icloud", NOW - timedelta(hours=3), timedelta(hours=2))
NEVER = SourceFreshness("ha_snapshot", None, timedelta(hours=26))


def finding(severity):
    return make_finding("CONF-008", "x", ("k",), severity=severity)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("findings", "sources", "error", "expected"),
    [
        ([], [FRESH], False, RunStatus.CLEAR),
        ([], [FRESH], True, RunStatus.FAILED),
        ([finding(Severity.CRITICAL)], [FRESH], True, RunStatus.FAILED),
        ([], [STALE], False, RunStatus.INCOMPLETE),
        ([], [FRESH, NEVER], False, RunStatus.INCOMPLETE),
        ([], [], False, RunStatus.INCOMPLETE),
        ([finding(Severity.INCOMPLETE)], [FRESH], False, RunStatus.INCOMPLETE),
        ([finding(Severity.WARNING)], [FRESH], False, RunStatus.ATTENTION_REQUIRED),
        ([finding(Severity.WARNING)], [STALE], False, RunStatus.INCOMPLETE),
        ([finding(Severity.CRITICAL)], [STALE], False, RunStatus.ATTENTION_REQUIRED),
    ],
)
def test_run_status(findings, sources, error, expected):
    assert (
        compute_run_status(findings, sources, now=NOW, runtime_error=error) is expected
    )


@pytest.mark.unit
def test_failed_attempt_within_max_age_is_still_usable():
    source = SourceFreshness(
        "icloud",
        NOW - timedelta(minutes=30),
        timedelta(hours=2),
        last_attempt_failed=True,
    )
    assert source.is_usable(NOW)
