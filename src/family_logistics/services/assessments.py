"""Assessment runs (weekly, daily, incremental, on-demand).

One run reads the registry and the collected calendar occurrences, parses the
transportation events, runs the deterministic engine and stores the outcome.

#CRITICAL: Data integrity: a run that raises is stored as failed and keeps the
previous findings open; it is never reported as clear (ADR 0001, rule 4).
#VERIFY: tests/unit/test_assessment_service.py injects a failing route provider
and a failing repository read.
#ASSUME: External resources: the collector keeps ``source_event`` current; a
calendar that failed to collect makes the run incomplete through DATA-011, not
empty, because the freshness pair reports its last complete collection.
#VERIFY: tests/unit/test_assessment_service.py::TestFreshness.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from family_logistics.db.assessments import AssessmentRepository
from family_logistics.db.registry import load_snapshot
from family_logistics.db.sources import SourceEventRepository
from family_logistics.domain.context import (
    HA_SNAPSHOT_SOURCE,
    ICLOUD_SOURCE,
    EngineSettings,
)
from family_logistics.domain.engine import (
    RULES_VERSION,
    AssessmentHistory,
    AssessmentResult,
    assess,
    failed_result,
)
from family_logistics.domain.events import (
    Activity,
    ActivityClassification,
    AssessmentInput,
    CalendarKind,
)
from family_logistics.domain.locations import LocationResolver
from family_logistics.domain.models import (
    Finding,
    OccurrenceKind,
    SourceEventKey,
    Trip,
)
from family_logistics.domain.parser import parse_trip
from family_logistics.services.collector import collection_window
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.engine import Engine

    from family_logistics.adapters.routing.base import RouteProvider
    from family_logistics.core.config import Settings
    from family_logistics.core.crypto import AddressCipher
    from family_logistics.db.assessments import StoredHistory
    from family_logistics.db.registry import RegistrySnapshot
    from family_logistics.domain.events import CalendarSource, SchoolDay, SourceEvent

logger = get_logger(__name__)

TRIGGERS = ("weekly", "daily", "incremental", "on_demand")
"""Values of ``analysis_run.trigger``."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """A stored run and its engine result.

    Attributes:
        run_id: Stored run identifier.
        result: The engine result (findings, run status, timelines).
    """

    run_id: str
    result: AssessmentResult


@dataclass(frozen=True, slots=True)
class SchoolSnapshot:
    """School schedules supplied by Home Assistant (M4).

    Attributes:
        days: School days inside the window.
        received_at: When the snapshot arrived (UTC).
    """

    days: tuple[SchoolDay, ...]
    received_at: datetime


def engine_settings(settings: Settings) -> EngineSettings:
    """Build the engine thresholds, including the iCloud staleness limit.

    Args:
        settings: Application settings.

    Returns:
        Engine settings; the iCloud source ages out after
        ``source_max_staleness_minutes`` like the collector's own check.
    """
    base = EngineSettings.from_config(settings)
    ages = tuple(
        (name, limit) for name, limit in base.source_max_ages if name != ICLOUD_SOURCE
    )
    icloud = timedelta(minutes=settings.source_max_staleness_minutes)
    return replace(base, source_max_ages=((ICLOUD_SOURCE, icloud), *ages))


def activity_id(event: SourceEvent) -> str:
    """Return a stable activity ID without exposing the calendar identifiers.

    Args:
        event: Source occurrence.

    Returns:
        ``act-`` plus 16 hex characters of a hash of the occurrence key.
    """
    material = "\x1f".join(event.occurrence_key).encode("utf-8")
    return f"act-{hashlib.sha256(material).hexdigest()[:16]}"


def occurrence_kind(event: SourceEvent) -> OccurrenceKind:
    """Classify how an occurrence relates to a recurring series.

    Args:
        event: Source occurrence.

    Returns:
        Override, series instance or single.
    """
    if event.is_override:
        return OccurrenceKind.OVERRIDE
    if event.series_uid_has_rrule:
        return OccurrenceKind.SERIES_INSTANCE
    return OccurrenceKind.SINGLE


def icloud_freshness(
    sources: Sequence[CalendarSource],
) -> tuple[str, datetime | None]:
    """Collapse the enabled calendars into one freshness pair.

    The oldest complete collection decides, and a calendar never collected
    makes the whole source unusable.

    Args:
        sources: Enabled calendar sources.

    Returns:
        ``(source name, oldest last success or None)``.
    """
    if not sources or any(s.last_success is None for s in sources):
        return (ICLOUD_SOURCE, None)
    return (ICLOUD_SOURCE, min(s.last_success for s in sources if s.last_success))


class AssessmentService:
    """Runs and stores assessments.

    Args:
        engine: Database engine at the schema head.
        settings: Application settings.
        route_provider: Travel-time source (unknown answers keep a run incomplete).
        cipher: Address cipher for protected registry fields.
        clock: Returns the current time (UTC); tests inject a fixed clock.
    """

    def __init__(  # noqa: PLR0913 - keyword-only collaborators
        self,
        engine: Engine,
        settings: Settings,
        route_provider: RouteProvider,
        *,
        cipher: AddressCipher | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._route_provider = route_provider
        self._cipher = cipher
        self._clock = clock
        self._repo = AssessmentRepository(engine)
        self._events = SourceEventRepository(engine)
        self._engine_settings = engine_settings(settings)

    @property
    def repository(self) -> AssessmentRepository:
        """Return the run repository."""
        return self._repo

    def run(
        self,
        trigger: str = "on_demand",
        school: SchoolSnapshot | None = None,
    ) -> RunOutcome:
        """Run one assessment and store its outcome.

        Args:
            trigger: What started the run (one of ``TRIGGERS``).
            school: School schedules from Home Assistant, when available.

        Returns:
            The stored run and its result. A run that raised is failed, keeps
            the previous findings open and never reads as clear.
        """
        now = self._clock()
        zone = ZoneInfo(self._settings.timezone)
        start, end = collection_window(now, zone, 0, self._settings.collect_days_ahead)
        run_id = self._repo.begin_run(
            trigger, start, end, now, rules_version=RULES_VERSION
        )
        history = self._repo.load_history()
        trips: tuple[Trip, ...] = ()
        try:
            result, trips = self._assess(now, (start, end), history, school)
        except Exception as exc:  # noqa: BLE001 - any failure must be recorded as failed
            # Only the class name is kept: messages can carry addresses.
            logger.error(  # noqa: TRY400 - a traceback can carry addresses
                "assessment_failed", run_id=run_id, error=type(exc).__name__
            )
            result = failed_result(
                now, f"assessment raised {type(exc).__name__}", history.open_findings
            )
        self._repo.complete_run(run_id, result, self._clock(), trips)
        logger.info(
            "assessment_finished",
            run_id=run_id,
            trigger=trigger,
            status=result.run_status.value,
            counts=result.counts(),
        )
        return RunOutcome(run_id, result)

    def _assess(
        self,
        now: datetime,
        window: tuple[datetime, datetime],
        history: StoredHistory,
        school: SchoolSnapshot | None,
    ) -> tuple[AssessmentResult, tuple[Trip, ...]]:
        start, end = window
        snapshot = load_snapshot(self._engine, self._cipher)
        enabled = [s for s in snapshot.calendar_sources if s.enabled]
        events = self._events.active(start, end, [s.calendar_id for s in enabled])
        by_calendar = {s.calendar_id: s for s in enabled}
        trips, parse_findings, unparsed = self._parse_rides(
            events, by_calendar, snapshot
        )
        activities = self._activities(events, by_calendar, snapshot)
        freshness = [icloud_freshness(enabled)]
        if school is not None:
            freshness.append((HA_SNAPSHOT_SOURCE, school.received_at))
        inp = AssessmentInput(
            window_start=start,
            window_end=end,
            people=snapshot.registry.people,
            external_passengers=snapshot.registry.external_passengers,
            locations=snapshot.locations,
            vehicles=snapshot.registry.vehicles,
            transport_defaults=snapshot.transport_defaults,
            activities=activities,
            trips=trips,
            school_days=school.days if school else (),
            source_freshness=tuple(freshness),
            previous_findings=history.open_findings,
            timezone=self._settings.timezone,
        )
        result = assess(
            inp,
            self._route_provider,
            now=now,
            settings=self._engine_settings,
            history=AssessmentHistory(
                previous_trips=history.trips,
                unparsed_trip_keys=frozenset(unparsed),
                parse_findings=parse_findings,
                resolved_issue_ids=history.resolved_issue_ids,
            ),
        )
        return result, trips

    @staticmethod
    def _parse_rides(
        events: Sequence[SourceEvent],
        sources: dict[str, CalendarSource],
        snapshot: RegistrySnapshot,
    ) -> tuple[tuple[Trip, ...], tuple[Finding, ...], list[SourceEventKey]]:
        trips: list[Trip] = []
        findings: list[Finding] = []
        unparsed: list[SourceEventKey] = []
        for event in events:
            source = sources.get(event.calendar_id)
            if (
                source is None
                or source.kind is not CalendarKind.TRANSPORT
                or event.status == "cancelled"
            ):
                continue
            parsed = parse_trip(
                event.description or "",
                key=event.to_key(),
                start=event.start,
                end=event.end,
                registry=snapshot.registry,
                occurrence=occurrence_kind(event),
            )
            findings.extend(parsed.findings)
            if parsed.trip is not None:
                trips.append(parsed.trip)
            else:
                unparsed.append(event.to_key())
        return tuple(trips), tuple(findings), unparsed

    @staticmethod
    def _activities(
        events: Sequence[SourceEvent],
        sources: dict[str, CalendarSource],
        snapshot: RegistrySnapshot,
    ) -> tuple[Activity, ...]:
        resolver = LocationResolver(
            snapshot.locations,
            snapshot.registry.people,
            snapshot.registry.external_passengers,
        )
        activities: list[Activity] = []
        for event in events:
            source = sources.get(event.calendar_id)
            # #ASSUME: Data integrity: an all-day person event has no time of
            # day, so it places nobody anywhere and is not an activity.
            # #VERIFY: tests/unit/test_assessment_service.py::TestActivities.
            if (
                source is None
                or source.kind is not CalendarKind.PERSON
                or source.person_id is None
                or event.status == "cancelled"
                or event.all_day
            ):
                continue
            activities.append(
                Activity(
                    activity_id=activity_id(event),
                    participant_ids=(source.person_id,),
                    location_id=resolver.resolve_text(event.location),
                    location_text=event.location,
                    start=event.start,
                    end=event.end,
                    source_key=event.occurrence_key,
                    classification=ActivityClassification.OTHER,
                )
            )
        return tuple(activities)


__all__ = [
    "TRIGGERS",
    "AssessmentService",
    "RunOutcome",
    "SchoolSnapshot",
    "activity_id",
    "engine_settings",
    "icloud_freshness",
    "occurrence_kind",
]
