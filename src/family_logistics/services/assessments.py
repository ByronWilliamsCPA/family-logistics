"""Assessment runs (weekly, daily, incremental, on-demand).

One run reads the registry and the collected calendar occurrences, parses the
transportation events, runs the deterministic engine and stores the outcome.

#CRITICAL: Data integrity: a run that raises is stored as failed and keeps the
previous findings open; it is never reported as clear (design rule 4 in
CLAUDE.md; ADR 0001 applies it to notifications and reports). That includes a
history read that raises (for example a corrupt trip snapshot row).
#VERIFY: tests/unit/test_assessment_service.py::TestFailedRuns injects a failing
route provider and a corrupt snapshot row; each must store a failed run.
#ASSUME: External resources: the collector keeps ``source_event`` current. The
freshness pair carries each calendar's last complete collection only, and
``SourceFreshness.is_usable`` (domain/run_status.py) deliberately tolerates a
failed latest attempt while that last success is within the staleness limit
(the stored events are still the last good read, not an empty calendar). A
calendar that was never collected, or whose last success is older than the
limit, is unusable and makes the run incomplete through DATA-011. Only enabled
calendars the collector selects (``FL_CALDAV_CALENDARS`` plus the transport
calendar) are judged; an enabled but unselected calendar is never collected and
is left out of the freshness pair.
#VERIFY: tests/unit/test_assessment_service.py::TestFreshness.
#CRITICAL: External resources: a run with no Home Assistant school snapshot
(ADR 0007: Home Assistant pushes it, so it may never have arrived) always adds
an ``ha_snapshot`` freshness pair with no timestamp. The engine reports that as
DATA-011, so the run is never CLEAR: it ends INCOMPLETE, or ATTENTION_REQUIRED
when a critical finding is open. The engine still receives no school days;
DATA-011 flags that gap (ADR 0007, design rule 4 in CLAUDE.md).
#VERIFY: tests/unit/test_assessment_service.py::TestFreshness (the missing,
stale and fresh snapshot cases).
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
    SchoolSnapshot,
)
from family_logistics.domain.locations import LocationResolver
from family_logistics.domain.notification_subjects import RideIndex
from family_logistics.domain.parser import parse_trip
from family_logistics.domain.recurrence import occurrence_kind
from family_logistics.services.collector import (
    collection_window,
    enabled_selected_sources,
)
from family_logistics.services.wiring import collector_config
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.engine import Engine

    from family_logistics.adapters.routing.base import RouteProvider
    from family_logistics.core.config import Settings
    from family_logistics.core.crypto import AddressCipher
    from family_logistics.db.assessments import StoredHistory
    from family_logistics.db.registry import RegistrySnapshot
    from family_logistics.domain.events import CalendarSource, SourceEvent
    from family_logistics.domain.models import Finding, SourceEventKey, Trip

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
        trigger: What started the run (one of ``TRIGGERS``).
        rides: The run's ride windows for notification imminence: current
            trips, unparsed transport events and ``history.trips`` for a
            completed run; ``history.trips`` alone for a failed run that read
            its history. None means unknown (the history could not be read),
            and the notification policy counts unknown as imminent.
    """

    run_id: str
    result: AssessmentResult
    trigger: str = "on_demand"
    rides: RideIndex | None = None


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
        self._collector_config = collector_config(settings)

    @property
    def repository(self) -> AssessmentRepository:
        """Return the run repository."""
        return self._repo

    def run(
        self,
        trigger: str = "on_demand",
        school: SchoolSnapshot | None = None,
        *,
        run_id: str | None = None,
    ) -> RunOutcome:
        """Run one assessment and store its outcome.

        Args:
            trigger: What started the run (one of ``TRIGGERS``).
            school: School schedules from Home Assistant. ``None`` means no snapshot
                has arrived: DATA-011 is raised and the run is never clear.
            run_id: An ID already handed to a caller (the on-demand API answers
                before the run starts); a new one is made when None.

        Returns:
            The stored run and its result. A run that raised is failed, keeps
            the previous findings open and never reads as clear.

        Raises:
            ValueError: ``trigger`` is not one of ``TRIGGERS`` (no run is
                written).
        """
        # #CRITICAL: Concurrency: ``begin_run``, ``load_history`` and
        # ``complete_run`` are three separate transactions with no database
        # lock across them (issue #22, still open). Within one process the
        # scheduler and the on-demand API serialize runs through the shared
        # ``PipelineCoordinator`` lock (services/pipeline.py), so a caller that
        # skips the lock, or a second process on the same database, can still
        # overlap two runs and let the older overwrite the newer findings.
        # #VERIFY: tests/unit/test_on_demand.py (TestSharedLock) pins the
        # in-process lock; nothing pins the cross-process case.
        if trigger not in TRIGGERS:
            msg = f"unknown trigger {trigger!r}; allowed: {', '.join(TRIGGERS)}"
            raise ValueError(msg)
        now = self._clock()
        zone = ZoneInfo(self._settings.timezone)
        start, end = collection_window(now, zone, 0, self._settings.collect_days_ahead)
        # #CRITICAL: Data integrity: ``begin_run`` stays outside the try. If the
        # placeholder row cannot be written there is no run to mark failed, so
        # the error must propagate loudly instead of being swallowed.
        # #VERIFY: tests/unit/test_assessment_service.py::TestFailedRuns.
        run_id = self._repo.begin_run(
            trigger, start, end, now, rules_version=RULES_VERSION, run_id=run_id
        )
        history: StoredHistory | None = None
        trips: tuple[Trip, ...] = ()
        rides: RideIndex | None = None
        # #EDGE: Data integrity: a permanently corrupt trip snapshot row makes
        # every run fail here, so runs stay FAILED (never clear) until a human
        # decides the snapshot healing policy; undecodable rows are not skipped.
        # #VERIFY: tests/unit/test_assessment_service.py::TestFailedRuns.
        try:
            history = self._repo.load_history()
            result, trips, rides = self._assess(now, (start, end), history, school)
        except Exception as exc:  # noqa: BLE001 - any failure must be recorded as failed
            # Only the class name is kept: messages can carry addresses.
            logger.error(  # noqa: TRY400 - a traceback can carry addresses
                "assessment_failed", run_id=run_id, error=type(exc).__name__
            )
            # The failed result carries the previous open findings on purpose,
            # so a failed run's counts show what is still open, not a clear
            # board. With no readable history there are none to carry.
            previous = history.open_findings if history is not None else ()
            # #ASSUME: Data integrity: with no readable history the ride set is
            # unknown (None), which the notification policy counts as imminent.
            # #VERIFY: tests/unit/test_assessment_service.py::TestRunOutcomeRides.
            rides = (
                RideIndex.build(current=(), previous=history.trips, unparsed=())
                if history is not None
                else None
            )
            result = failed_result(
                now, f"assessment raised {type(exc).__name__}", previous
            )
        self._repo.complete_run(run_id, result, self._clock(), trips)
        logger.info(
            "assessment_finished",
            run_id=run_id,
            trigger=trigger,
            status=result.run_status.value,
            counts=result.counts(),
        )
        return RunOutcome(run_id, result, trigger, rides)

    def _assess(
        self,
        now: datetime,
        window: tuple[datetime, datetime],
        history: StoredHistory,
        school: SchoolSnapshot | None,
    ) -> tuple[AssessmentResult, tuple[Trip, ...], RideIndex]:
        # #EDGE: Timing: the assessment window is [today, today + N days] while
        # the collector window starts ``collect_days_back`` days earlier, so
        # events from the previous days are collected but not assessed; intended.
        # #VERIFY: optional; no test pins the days-back gap.
        start, end = window
        snapshot = load_snapshot(self._engine, self._cipher)
        # #ASSUME: External resources: only enabled calendars the collector
        # selects are collected, so only those can have a freshness; an enabled
        # but unselected calendar would otherwise raise DATA-011 on every run.
        # #VERIFY: tests/unit/test_assessment_service.py::TestFreshness.
        enabled = enabled_selected_sources(
            snapshot.calendar_sources, self._collector_config
        )
        events = self._events.active(start, end, [s.calendar_id for s in enabled])
        by_calendar = {s.calendar_id: s for s in enabled}
        trips, parse_findings, unparsed = self._parse_rides(
            events, by_calendar, snapshot
        )
        activities = self._activities(events, by_calendar, snapshot)
        freshness = [icloud_freshness(enabled)]
        # #CRITICAL: External resources: a missing school snapshot must never let
        # a run read as clear (ADR 0007, design rule 4). The HA freshness pair is
        # always reported, with no timestamp when no snapshot has arrived, so the
        # engine raises DATA-011 for ha_snapshot and the run is never CLEAR
        # (INCOMPLETE, or ATTENTION_REQUIRED when a critical finding is open).
        # School checks still see no school days; DATA-011 is the protection.
        # #VERIFY: tests/unit/test_assessment_service.py::TestFreshness
        # (test_without_a_school_snapshot_the_run_is_incomplete_never_clear).
        # #EDGE: Data integrity: a naive ``SchoolSnapshot.received_at`` makes
        # ``AssessmentInput.__post_init__`` (domain/events.py) raise a
        # ``ValidationError`` when ``inp`` is built below, before ``assess`` is
        # called. The exception escapes ``_assess`` and ``run`` records a failed
        # run, never a clear one.
        # #VERIFY: tests/unit/test_assessment_service.py::TestFailedRuns.
        freshness.append(
            (HA_SNAPSHOT_SOURCE, school.received_at if school is not None else None)
        )
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
            school_days=school.days if school is not None else (),
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
                unparsed_trip_keys=frozenset(key for key, _, _ in unparsed),
                parse_findings=parse_findings,
                resolved_issue_ids=history.resolved_issue_ids,
            ),
        )
        rides = RideIndex.build(
            current=trips, previous=history.trips, unparsed=unparsed
        )
        return result, trips, rides

    @staticmethod
    def _parse_rides(
        events: Sequence[SourceEvent],
        sources: dict[str, CalendarSource],
        snapshot: RegistrySnapshot,
    ) -> tuple[
        tuple[Trip, ...],
        tuple[Finding, ...],
        list[tuple[SourceEventKey, datetime, datetime]],
    ]:
        trips: list[Trip] = []
        findings: list[Finding] = []
        unparsed: list[tuple[SourceEventKey, datetime, datetime]] = []
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
                unparsed.append((event.to_key(), event.start, event.end))
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
            # #EDGE: Data integrity: an enabled PERSON calendar with no
            # person_id is skipped silently; unreachable because the database
            # CHECK ``ck_calendar_source_person_owner`` (db/schema.py) requires
            # an owner for kind person.
            # #VERIFY: optional; fail loudly here if the CHECK is ever relaxed.
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
]
