"""The family-wide status card for ``GET /api/v1/status`` (ADR 0007).

Counts and times only: no child names, addresses, calendar ids or finding text
(privacy spec, section 4). The card answers one question, "may the family trust
that nothing is wrong right now", and answers yes (``clear``) only when the
latest finished run was clear and nothing below makes it untrustworthy.

#CRITICAL: Data integrity: design rule 4 (ADR 0001): a failed or incomplete
analysis is never reported as clear. ``clear`` is downgraded to ``incomplete``
when there is no finished run, the latest run is older than
the iCloud staleness limit (``FL_SOURCE_MAX_STALENESS_MINUTES``: a run older
than the data it may judge is not evidence), the calendar data the run read is
older than that same limit (``calendar_stale``: the OLDEST successful
collection of the calendars the collector reads, measured against the current
time, so a run that finished a minute ago on data collected hours ago is not
clear; no calendar source, or one never collected, counts as stale), or the
school snapshot is missing or older than its limit (26 hours,
``DEFAULT_SOURCE_MAX_AGES``). A run that started and never finished while
no run is executing (the process died) reads as ``failed``. ``attention_required``,
``incomplete`` and ``failed`` are never loosened.
#VERIFY: tests/unit/test_status_api.py (TestNeverClear, TestCalendarStale).
#ASSUME: Timing: ``clock`` and the database agree on UTC; both stamps are
timezone-aware UTC (``UtcDateTime``), so an age is a plain subtraction.
#VERIFY: tests/unit/test_status_api.py (TestStale).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from family_logistics.domain.context import HA_SNAPSHOT_SOURCE, EngineSettings
from family_logistics.domain.models import RunStatus
from family_logistics.services.assessments import icloud_freshness
from family_logistics.services.collector import enabled_selected_sources
from family_logistics.services.wiring import collector_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from family_logistics.core.config import Settings
    from family_logistics.db.assessments import AssessmentRepository, RunRecord
    from family_logistics.db.school_snapshots import SchoolSnapshotRepository
    from family_logistics.db.sources import CalendarSourceRepository
    from family_logistics.services.on_demand import OnDemandService

StatusReason = Literal[
    "no_run",
    "run_interrupted",
    "analysis_stale",
    "calendar_stale",
    "school_snapshot_missing",
    "school_snapshot_stale",
]
"""The closed vocabulary of ``StatusCard.reasons``."""

NO_RUN: Final[StatusReason] = "no_run"
RUN_INTERRUPTED: Final[StatusReason] = "run_interrupted"
ANALYSIS_STALE: Final[StatusReason] = "analysis_stale"
CALENDAR_STALE: Final[StatusReason] = "calendar_stale"
SCHOOL_MISSING: Final[StatusReason] = "school_snapshot_missing"
SCHOOL_STALE: Final[StatusReason] = "school_snapshot_stale"
"""Reasons a card is not clear (``StatusCard.reasons``); a fixed vocabulary."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class StatusCard:
    """What the Home Assistant card shows.

    Attributes:
        status: ``clear``, ``attention_required``, ``incomplete`` or ``failed``,
            after the downgrades in the module docstring.
        reasons: Every condition that limits the card (``NO_RUN`` and the like),
            whatever the status. A clear run with any reason is ``incomplete``.
        counts: Open findings per severity from the latest finished run
            (a failed run carries the still-open findings); None without one.
        lifecycle: Open findings per lifecycle state (new, ongoing, regressed);
            None without a finished run.
        last_analysis_at: When the latest finished run finished.
        last_school_sync_at: The school snapshot's fetch time (its age is
            measured from it).
        analysis_stale: The latest finished run is older than the limit, or
            there is none.
        calendar_stale: The oldest successful collection of the calendars the
            run reads is older than the limit, or none has ever succeeded.
        school_snapshot_stale: The snapshot is older than its limit or missing.
        run_in_flight: A run is queued, or a pipeline holds the pipeline lock
            (a scheduled collect, assess and notify, or a purge).
        generated_at: When the card was built.
    """

    status: RunStatus
    reasons: tuple[StatusReason, ...]
    counts: Mapping[str, int] | None
    lifecycle: Mapping[str, int] | None
    last_analysis_at: datetime | None
    last_school_sync_at: datetime | None
    analysis_stale: bool
    calendar_stale: bool
    school_snapshot_stale: bool
    run_in_flight: bool
    generated_at: datetime


class StatusService:
    """Builds the status card from the stored runs and the school snapshot.

    Args:
        settings: Application settings (the staleness limits).
        runs: The run repository.
        schools: The school snapshot store.
        sources: The calendar sources (when each was last collected).
        on_demand: Says whether a run is in flight in this process.
        clock: The current aware time (replaceable in tests).
    """

    # Keyword-only collaborators are injected so tests can replace each one.
    def __init__(  # noqa: PLR0913
        self,
        settings: Settings,
        *,
        runs: AssessmentRepository,
        schools: SchoolSnapshotRepository,
        sources: CalendarSourceRepository,
        on_demand: OnDemandService,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._runs = runs
        self._schools = schools
        self._sources = sources
        self._collector_config = collector_config(settings)
        self._on_demand = on_demand
        self._clock = clock
        self._analysis_max_age = timedelta(
            minutes=settings.source_max_staleness_minutes
        )
        self._school_max_age = EngineSettings.from_config(settings).max_age_for(
            HA_SNAPSHOT_SOURCE
        )

    def card(self) -> StatusCard:
        """Build the card.

        A store read that fails propagates as ``sqlalchemy.exc.SQLAlchemyError``;
        the caller must answer "unavailable", never a card.

        Returns:
            The card.
        """
        now = self._clock()
        in_flight = self._on_demand.in_flight()
        latest = self._runs.latest_run()
        interrupted = self._interrupted(latest)
        basis = self._basis(latest)
        analysis_stale, analysis_reason = self._analysis(now, basis)
        calendar_stale = self._calendar_stale(now)
        school_at, school_stale, school_reason = self._school(now)
        reasons = _reasons(
            RUN_INTERRUPTED if interrupted else None,
            analysis_reason,
            CALENDAR_STALE if calendar_stale else None,
            school_reason,
        )
        return StatusCard(
            status=_verdict(interrupted=interrupted, basis=basis, reasons=reasons),
            reasons=reasons,
            counts=basis.counts if basis is not None else None,
            lifecycle=self._runs.open_lifecycle_counts() if basis is not None else None,
            last_analysis_at=basis.finished_at if basis is not None else None,
            last_school_sync_at=school_at,
            analysis_stale=analysis_stale,
            calendar_stale=calendar_stale,
            school_snapshot_stale=school_stale,
            run_in_flight=in_flight,
            generated_at=now,
        )

    def _basis(self, latest: RunRecord | None) -> RunRecord | None:
        """Return the run the card judges: the latest, or else the last finished."""
        if latest is None or latest.finished_at is None:
            return self._runs.latest_finished_run()
        return latest

    def _analysis(
        self, now: datetime, basis: RunRecord | None
    ) -> tuple[bool, StatusReason | None]:
        """Say whether the judged run is stale, and the reason that gives."""
        if basis is None:
            return True, NO_RUN
        if self._older(now, basis.finished_at, self._analysis_max_age):
            return True, ANALYSIS_STALE
        return False, None

    def _calendar_stale(self, now: datetime) -> bool:
        """Say whether the calendar data the run read is too old now.

        The same collapse the run uses (oldest success of the calendars the
        collector reads), judged against NOW: the run's own DATA-011 check
        looked at the data as of when the run happened.
        """
        _, collected_at = icloud_freshness(
            enabled_selected_sources(self._sources.all(), self._collector_config)
        )
        return self._older(now, collected_at, self._analysis_max_age)

    def _school(
        self, now: datetime
    ) -> tuple[datetime | None, bool, StatusReason | None]:
        """Return the snapshot's fetch time, whether it is stale, and why."""
        freshness = self._schools.freshness()
        school_at = freshness.fetched_at if freshness is not None else None
        if school_at is None:
            return None, True, SCHOOL_MISSING
        if self._older(now, school_at, self._school_max_age):
            return school_at, True, SCHOOL_STALE
        return school_at, False, None

    def _interrupted(self, latest: RunRecord | None) -> bool:
        """Report whether the latest started run died without finishing."""
        if latest is None or latest.finished_at is not None:
            return False
        # The row is read before the executing mark, which is set before the
        # row exists and cleared after the outcome is stored (PipelineCoordinator),
        # so an unfinished row with no mark is a dead run unless it just finished.
        if self._on_demand.active_run_id() == latest.run_id:
            return False
        again = self._runs.get_run(latest.run_id)
        return again is None or again.finished_at is None

    @staticmethod
    def _older(now: datetime, then: datetime | None, limit: timedelta) -> bool:
        # #EDGE: Timing: a stamp in the future gives a negative age, so it reads
        # as fresh, never stale. This matches ``domain/run_status`` and the
        # snapshot's ``fetched_at``, which is clamped to the server clock when
        # stored (``MAX_FUTURE_SKEW``), so a skewed clock cannot make old data
        # look new through the snapshot; a run's ``finished_at`` is the app's
        # own clock.
        # #VERIFY: tests/unit/test_status_api.py (TestFutureStamps).
        return then is None or now - then > limit


def _reasons(*found: StatusReason | None) -> tuple[StatusReason, ...]:
    """Keep the reasons that apply, in the order the card lists them."""
    return tuple(reason for reason in found if reason is not None)


def _verdict(
    *, interrupted: bool, basis: RunRecord | None, reasons: tuple[StatusReason, ...]
) -> RunStatus:
    """Return the card status: a clear run with any reason is incomplete."""
    if interrupted:
        return RunStatus.FAILED
    run_status = basis.status if basis is not None else RunStatus.INCOMPLETE
    if run_status is RunStatus.CLEAR and reasons:
        return RunStatus.INCOMPLETE
    return run_status
