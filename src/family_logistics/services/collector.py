"""iCloud CalDAV collector: discover, detect changes, expand, persist.

One ``Collector.run_once`` call:

1. discovers the account's calendars (``adapters.caldav.client``);
2. keeps only the calendars named in ``FL_CALDAV_CALENDARS`` plus the shared
   transportation calendar. The transportation calendar is registered here when
   new; an individual's calendar must already be in the registry with its owner
   (a calendar that is not reports DATA-011). The kind and owner of an existing
   source are never changed here; the registry owns them;
3. for each enabled source that is still selected (a stored source dropped from
   the setting is skipped, not reported as a lost share), reads its ctag and
   sync token. When neither changed since a complete collection earlier the
   same local day (so the window is the same), the stored occurrences are
   reused. Otherwise the whole window is
   queried and every object is expanded locally (``domain.recurrence``);
4. stores the result (``db.sources``): complete results may mark missing
   occurrences deleted, incomplete ones never do;
5. reports DATA-011 for every source that failed, is incomplete, or whose last
   complete collection is older than ``FL_SOURCE_MAX_STALENESS_MINUTES``.

#CRITICAL: Data integrity: a network failure, a non-200 or truncated
calendar-query entry, an unparseable object, a lost calendar share, a calendar
that stopped supporting events or a discovery failure all yield an incomplete
``CollectionResult`` and DATA-011, never an empty calendar.
#CRITICAL: Security: calendar hrefs (which embed the account number) are held in
memory only; sources are stored under an opaque hash, and error messages carry
no URLs or credentials.
#ASSUME: Timing: the window is aligned to local midnight, so two collections on
the same local day cover the same window and an unchanged ctag means unchanged
content. A configuration change to the window size takes effect at the next
full collection (at the latest the next local day).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from family_logistics.adapters.caldav.client import (
    CalDAVError,
    SyncTokenInvalidError,
)
from family_logistics.adapters.caldav.ids import calendar_id_for_href
from family_logistics.db.sources import (
    ApplySummary,
    SyncState,
    UnregisteredCalendarError,
)
from family_logistics.domain.events import (
    CalendarKind,
    CalendarSource,
    CollectionResult,
)
from family_logistics.domain.recurrence import RecurrenceError, expand_object
from family_logistics.domain.rules import make_finding
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable

    from family_logistics.adapters.caldav.client import CalDAVClient, CalendarInfo
    from family_logistics.db.sources import (
        CalendarSourceRepository,
        SourceEventRepository,
        SyncStateRepository,
    )
    from family_logistics.domain.events import SourceEvent
    from family_logistics.domain.models import Finding

logger = get_logger(__name__)

NOT_VISIBLE = "calendar is not visible to the collector account (share removed?)"
NOT_EVENTS = "calendar no longer supports events (VEVENT)"


def collection_window(
    now: datetime, zone: ZoneInfo, days_back: int, days_ahead: int
) -> tuple[datetime, datetime]:
    """Return the collection window, aligned to local midnights.

    Args:
        now: Current time (aware).
        zone: Local zone.
        days_back: Local days before today to include.
        days_ahead: Local days after today to include.

    Returns:
        ``(start, end)`` in UTC; ``end`` is exclusive (midnight after the last day).
    """
    today = now.astimezone(zone).date()
    start = datetime.combine(today - timedelta(days=days_back), time.min, tzinfo=zone)
    end = datetime.combine(
        today + timedelta(days=days_ahead + 1), time.min, tzinfo=zone
    )
    return start.astimezone(UTC), end.astimezone(UTC)


def source_findings(
    results: Iterable[CollectionResult],
    now: datetime,
    max_staleness: timedelta,
) -> tuple[Finding, ...]:
    """Raise DATA-011 for failed, incomplete or stale calendar sources.

    Args:
        results: The latest result of each source.
        now: Current time (UTC).
        max_staleness: Oldest acceptable complete collection.

    Returns:
        One DATA-011 finding per affected source.
    """
    findings: list[Finding] = []
    for result in results:
        source = result.source
        name = source.display_name
        if not result.complete:
            problem = result.error or "unknown error"
            message = f"Calendar '{name}' could not be read completely: {problem}."
        elif source.last_success is None or now - source.last_success > max_staleness:
            message = f"Calendar '{name}' was not collected recently enough to rely on."
        else:
            continue
        key = (f"source:{source.calendar_id}",)
        findings.append(make_finding("DATA-011", message, key))
    return tuple(findings)


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Collector settings.

    Attributes:
        timezone: IANA zone for the window and floating times.
        days_back: Local days before today in the window.
        days_ahead: Local days after today in the window.
        transport_calendar: Display name of the shared ride calendar.
        selected: Display names or calendar IDs to collect.
        max_staleness: Oldest acceptable complete collection.
    """

    timezone: str
    days_back: int
    days_ahead: int
    transport_calendar: str
    selected: tuple[str, ...]
    max_staleness: timedelta


def selected_names(config: CollectorConfig) -> set[str]:
    """Return the configured display names or IDs plus the transport calendar.

    Args:
        config: Collector settings.

    Returns:
        The names and IDs the collector collects.
    """
    return {*config.selected, config.transport_calendar}


def enabled_selected_sources(
    sources: Iterable[CalendarSource],
    config: CollectorConfig,
    only: Collection[str] | None = None,
) -> list[CalendarSource]:
    """Return the enabled sources the collector actually collects.

    A stored source dropped from ``FL_CALDAV_CALENDARS`` (or seeded by the
    registry but never listed) is not collected, so it has no collection
    freshness to judge.

    Args:
        sources: Stored calendar sources.
        config: Collector settings.
        only: Limit to these display names or IDs; None keeps all selected.

    Returns:
        Enabled sources that are selected (and in ``only`` when given).
    """
    selected = selected_names(config)
    return [
        s
        for s in sources
        if s.enabled
        and {s.display_name, s.calendar_id} & selected
        and (only is None or {s.display_name, s.calendar_id} & set(only))
    ]


@dataclass(frozen=True, slots=True)
class CalendarRun:
    """Outcome for one calendar.

    Attributes:
        result: The collection result.
        applied: What was stored.
        reused: Whether stored occurrences were reused (ctag unchanged).
    """

    result: CollectionResult
    applied: ApplySummary
    reused: bool


@dataclass(frozen=True, slots=True)
class CollectionRun:
    """Outcome of one collector run.

    Attributes:
        window_start: Window start (UTC).
        window_end: Window end (UTC).
        calendars: Per-calendar outcomes.
        findings: DATA-011 findings for failed, incomplete or stale sources.
        discovery_error: Set when discovery itself failed.
    """

    window_start: datetime
    window_end: datetime
    calendars: tuple[CalendarRun, ...] = ()
    findings: tuple[Finding, ...] = ()
    discovery_error: str | None = None

    @property
    def complete(self) -> bool:
        """Report whether every calendar was collected completely.

        Returns:
            True when discovery worked and every result is complete.
        """
        return self.discovery_error is None and all(
            c.result.complete for c in self.calendars
        )


def _now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Pass:
    """Context shared by every calendar in one run."""

    start: datetime
    end: datetime
    now: datetime
    zone: ZoneInfo

    def result(
        self,
        source: CalendarSource,
        events: Iterable[SourceEvent] = (),
        error: str | None = None,
    ) -> CollectionResult:
        """Build a result for this run's window; ``error`` makes it incomplete."""
        return CollectionResult(
            source=source,
            events=tuple(events),
            window_start=self.start,
            window_end=self.end,
            fetched_at=self.now,
            complete=error is None,
            error=error,
        )


@dataclass
class Collector:
    """Collects the configured calendars into the database.

    Attributes:
        client: Read-only CalDAV client.
        sources: Calendar source repository.
        events: Occurrence repository.
        sync_states: Sync state repository.
        config: Collector settings.
        clock: Current-time function (injectable for tests).
    """

    client: CalDAVClient
    sources: CalendarSourceRepository
    events: SourceEventRepository
    sync_states: SyncStateRepository
    config: CollectorConfig
    clock: Callable[[], datetime] = field(default=_now)

    def run_once(self, only: Collection[str] | None = None) -> CollectionRun:
        """Collect every selected, enabled calendar once.

        Args:
            only: Further restrict to these display names or calendar IDs.

        Returns:
            Per-calendar outcomes and DATA-011 findings.
        """
        now = self.clock()
        zone = ZoneInfo(self.config.timezone)
        start, end = collection_window(
            now, zone, self.config.days_back, self.config.days_ahead
        )
        run = _Pass(start, end, now, zone)
        discovery_error: str | None = None
        runs: list[CalendarRun] = []
        visible: set[str] = set()
        non_event: set[str] = set()
        try:
            discovered = self.client.discover()
        except CalDAVError as exc:
            discovery_error = str(exc)
            logger.warning("collector.discovery_failed", error=discovery_error)
            discovered = ()
        for info in discovered:
            calendar_id = calendar_id_for_href(info.href)
            if not info.supports_events:
                non_event.add(calendar_id)
                continue
            if not self._wanted(info, only):
                continue
            visible.add(calendar_id)
            kind = (
                CalendarKind.TRANSPORT
                if info.display_name == self.config.transport_calendar
                else CalendarKind.PERSON
            )
            try:
                source = self.sources.ensure(calendar_id, info.display_name, kind)
            except UnregisteredCalendarError as exc:
                runs.append(
                    self._failed(
                        CalendarSource(calendar_id, info.display_name, kind),
                        str(exc),
                        run,
                    )
                )
                continue
            if source.enabled:
                runs.append(self._collect(source, info.href, run))
        runs.extend(
            self._failed(source, self._missing(source, non_event, discovery_error), run)
            for source in self._enabled(only)
            if source.calendar_id not in visible
        )
        findings = source_findings(
            [r.result for r in runs], now, self.config.max_staleness
        )
        return CollectionRun(start, end, tuple(runs), findings, discovery_error)

    def _selected(self) -> set[str]:
        """Return the configured display names or IDs plus the transport calendar."""
        return selected_names(self.config)

    def _wanted(self, info: CalendarInfo, only: Collection[str] | None) -> bool:
        """Report whether a discovered calendar is selected for collection."""
        names = {info.display_name, calendar_id_for_href(info.href)}
        if not names & self._selected():
            return False
        return only is None or bool(names & set(only))

    def _enabled(self, only: Collection[str] | None) -> list[CalendarSource]:
        """Return enabled stored sources that are still selected.

        A stored source dropped from ``FL_CALDAV_CALENDARS`` (or seeded by the
        registry but never listed) is not collected, so it must not be
        reported as a lost share on every run either.
        """
        return enabled_selected_sources(self.sources.all(), self.config, only)

    @staticmethod
    def _missing(
        source: CalendarSource, non_event: set[str], discovery_error: str | None
    ) -> str:
        """Explain why a selected, enabled source was not collected."""
        if discovery_error is not None:
            return f"discovery failed: {discovery_error}"
        if source.calendar_id in non_event:
            return NOT_EVENTS
        return NOT_VISIBLE

    def _failed(self, source: CalendarSource, error: str, run: _Pass) -> CalendarRun:
        """Record and return an incomplete result for a source."""
        self.sources.record_failure(source.calendar_id, error)
        stored = self.sources.get(source.calendar_id) or source
        return CalendarRun(
            run.result(stored, error=error), ApplySummary(), reused=False
        )

    def _collect(self, source: CalendarSource, href: str, run: _Pass) -> CalendarRun:
        """Collect one calendar, reusing stored occurrences when unchanged."""
        try:
            ctag, token = self.client.state(href)
            previous = self.sync_states.get(source.calendar_id)
            if self._unchanged(previous, ctag=ctag, token=token, href=href, run=run):
                stored = self.events.active(run.start, run.end, [source.calendar_id])
                state = SyncState(source.calendar_id, token, ctag, run.now)
                return self._complete(source, stored, state, run, reused=True)
            objects = self.client.query_window(href, run.start, run.end)
        except CalDAVError as exc:
            return self._failed(source, str(exc), run)
        events: list[SourceEvent] = []
        broken = 0
        for item in objects:
            try:
                expanded = expand_object(
                    item.ics,
                    calendar_id=source.calendar_id,
                    window_start=run.start,
                    window_end=run.end,
                    zone=run.zone,
                    etag=item.etag,
                    seen_at=run.now,
                )
            except RecurrenceError:
                broken += 1
            else:
                events.extend(expanded)
        if broken:
            error = f"{broken} calendar object(s) could not be parsed"
            self.sources.record_failure(source.calendar_id, error)
            result = run.result(source, events, error=error)
            return CalendarRun(result, self.events.apply(result, run.now), reused=False)
        state = SyncState(source.calendar_id, token, ctag, run.now)
        return self._complete(source, events, state, run, reused=False)

    def _complete(  # noqa: PLR0913 - keyword-only outcome flag
        self,
        source: CalendarSource,
        events: Iterable[SourceEvent],
        state: SyncState,
        run: _Pass,
        *,
        reused: bool,
    ) -> CalendarRun:
        """Store a complete result and record success."""
        self.sources.record_success(source.calendar_id, run.now)
        stored = self.sources.get(source.calendar_id) or source
        result = run.result(stored, events)
        applied = ApplySummary() if reused else self.events.apply(result, run.now)
        self.sync_states.save(state)
        return CalendarRun(result, applied, reused=reused)

    # Keyword-only change-detection inputs; each one is a distinct comparison.
    def _unchanged(  # noqa: PLR0913
        self,
        previous: SyncState | None,
        *,
        ctag: str | None,
        token: str | None,
        href: str,
        run: _Pass,
    ) -> bool:
        """Report whether stored occurrences still describe the calendar.

        Only a complete collection earlier the same local day counts (same
        window). The ctag decides when the server reports one; otherwise the
        sync token, confirmed with a ``sync-collection`` REPORT when it moved.
        """
        last = None if previous is None else previous.last_full_sync
        if previous is None or last is None:
            return False
        if last.astimezone(run.zone).date() != run.now.astimezone(run.zone).date():
            return False
        if ctag is not None:
            return previous.ctag == ctag
        if token is None or previous.sync_token is None:
            return False
        return token == previous.sync_token or self._no_changes(
            href, previous.sync_token
        )

    def _no_changes(self, href: str, sync_token: str) -> bool:
        """Ask the server whether anything changed since a sync token."""
        try:
            changes = self.client.sync_changes(href, sync_token)
        except SyncTokenInvalidError:
            return False
        return not changes.changed and not changes.removed
