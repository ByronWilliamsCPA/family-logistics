"""Collector service: selection, change detection, persistence, DATA-011."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from caldav_fakes import (
    ALLOWED_HOSTS,
    BASE_URL,
    PARTITION,
    SAM_PATH,
    TRANSPORT_PATH,
    default_server,
    fixture_text,
    register_person_calendar,
)

from family_logistics.adapters.caldav.client import CalDAVClient
from family_logistics.adapters.caldav.ids import calendar_id_for_href
from family_logistics.adapters.caldav.readonly_transport import (
    ReadOnlyTransport,
    build_readonly_client,
)
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.sources import (
    CalendarSourceRepository,
    SourceEventRepository,
    SyncStateRepository,
)
from family_logistics.domain.events import (
    CalendarKind,
    CalendarSource,
    CollectionResult,
)
from family_logistics.services.collector import (
    NOT_EVENTS,
    NOT_VISIBLE,
    Collector,
    CollectorConfig,
    collection_window,
    source_findings,
)

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)  # 11:00 local, Wednesday
SAM_ID = calendar_id_for_href(f"{PARTITION}{SAM_PATH}")
TRANSPORT_ID = calendar_id_for_href(f"{PARTITION}{TRANSPORT_PATH}")
CONFIG = CollectorConfig(
    timezone="America/Los_Angeles",
    days_back=2,
    days_ahead=14,
    transport_calendar="Family Transportation",
    selected=("Sam",),
    max_staleness=timedelta(minutes=120),
)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    register_person_calendar(engine, SAM_ID, "Sam")
    yield engine
    engine.dispose()


def make(engine, server, clock: Clock | None = None) -> Collector:
    transport = ReadOnlyTransport(server.transport(), allowed_hosts=ALLOWED_HOSTS)
    http = build_readonly_client(BASE_URL, transport, auth=("u", "p"))
    return Collector(
        client=CalDAVClient(http, attempts=1, sleep=lambda _s: None),
        sources=CalendarSourceRepository(engine),
        events=SourceEventRepository(engine),
        sync_states=SyncStateRepository(engine),
        config=CONFIG,
        clock=clock or Clock(NOW),
    )


def reports(server) -> list[str]:
    return [path for method, path in server.requests if method == "REPORT"]


@pytest.mark.unit
class TestWindow:
    def test_window_is_aligned_to_local_midnight(self):
        start, end = collection_window(NOW, ZONE, 2, 14)
        assert start == datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
        # 14 days ahead crosses the DST change: midnight is 08:00 UTC again.
        assert end == datetime(2026, 11, 5, 8, 0, tzinfo=UTC)


@pytest.mark.unit
class TestFirstCollection:
    def test_collects_selected_calendars_only(self, engine):
        server = default_server()
        run = make(engine, server).run_once()
        assert run.complete
        assert run.findings == ()
        assert {c.result.source.calendar_id for c in run.calendars} == {
            SAM_ID,
            TRANSPORT_ID,
        }
        sources = {s.calendar_id: s for s in CalendarSourceRepository(engine).all()}
        assert sources[TRANSPORT_ID].kind is CalendarKind.TRANSPORT
        assert sources[SAM_ID].kind is CalendarKind.PERSON
        assert sources[SAM_ID].last_success == NOW
        assert sorted(reports(server)) == sorted([SAM_PATH, TRANSPORT_PATH])

    def test_occurrences_are_stored(self, engine):
        run = make(engine, default_server()).run_once()
        sam = next(c for c in run.calendars if c.result.source.calendar_id == SAM_ID)
        # Lesson Oct 20 (window starts Oct 19), moved Nov 3 occurrence (Nov 4),
        # and the floating-time practice on Oct 22.
        assert len(sam.result.events) == 3
        assert sam.applied.inserted == 3
        stored = SourceEventRepository(engine).active(
            sam.result.window_start, sam.result.window_end
        )
        assert len(stored) == 4

    def test_orphan_overrides_of_one_uid_are_stored_separately(self, engine):
        server = default_server()
        server.calendar(SAM_PATH).objects = {
            "alex.ics": fixture_text("orphan_overrides.ics")
        }
        run = make(engine, server).run_once(only=["Sam"])
        assert run.complete
        assert run.calendars[0].applied.inserted == 2
        stored = SourceEventRepository(engine).active(
            run.window_start, run.window_end, [SAM_ID]
        )
        assert sorted(e.recurrence_id for e in stored) == [
            "2026-10-27T22:00:00Z",
            "2026-11-03T23:00:00Z",
        ]

    def test_only_restricts_the_run(self, engine):
        server = default_server()
        run = make(engine, server).run_once(only=["Sam"])
        assert [c.result.source.calendar_id for c in run.calendars] == [SAM_ID]


@pytest.mark.unit
class TestChangeDetection:
    def test_unchanged_ctag_same_day_reuses_stored(self, engine):
        server = default_server()
        clock = Clock(NOW)
        collector = make(engine, server, clock)
        collector.run_once()
        server.requests.clear()
        clock.now = NOW + timedelta(hours=1)
        run = collector.run_once()
        assert run.complete
        assert reports(server) == []
        assert all(c.reused for c in run.calendars)
        sam = next(c for c in run.calendars if c.result.source.calendar_id == SAM_ID)
        assert len(sam.result.events) == 3

    def test_changed_ctag_queries_again(self, engine):
        server = default_server()
        clock = Clock(NOW)
        collector = make(engine, server, clock)
        collector.run_once()
        server.requests.clear()
        server.calendar(SAM_PATH).ctag = "ctag-2"
        clock.now = NOW + timedelta(hours=1)
        collector.run_once()
        assert reports(server) == [SAM_PATH]

    def test_next_local_day_queries_again(self, engine):
        server = default_server()
        clock = Clock(NOW)
        collector = make(engine, server, clock)
        collector.run_once()
        server.requests.clear()
        clock.now = NOW + timedelta(days=1)
        collector.run_once()
        assert sorted(reports(server)) == sorted([SAM_PATH, TRANSPORT_PATH])

    @pytest.mark.parametrize(
        ("token", "changes", "queried"),
        [
            ("token-1", ((), ()), False),
            ("token-2", ((), ()), False),
            ("token-2", (("lesson.ics",), ()), True),
        ],
    )
    def test_sync_token_when_server_has_no_ctag(self, engine, token, changes, queried):
        server = default_server()
        calendar = server.calendar(SAM_PATH)
        calendar.ctag = None
        clock = Clock(NOW)
        collector = make(engine, server, clock)
        collector.run_once(only=["Sam"])
        server.requests.clear()
        calendar.token = token
        calendar.changes = changes
        clock.now = NOW + timedelta(hours=1)
        collector.run_once(only=["Sam"])
        query_count = sum(
            1 for m, p in server.requests if m == "REPORT" and p == SAM_PATH
        )
        # A moved token always costs one sync-collection REPORT; a change adds a query.
        expected = (1 if token != "token-1" else 0) + (1 if queried else 0)
        assert query_count == expected

    def test_rejected_sync_token_falls_back_to_full_query(self, engine):
        server = default_server()
        calendar = server.calendar(SAM_PATH)
        calendar.ctag = None
        clock = Clock(NOW)
        collector = make(engine, server, clock)
        collector.run_once(only=["Sam"])
        calendar.token = "token-2"
        server.reject_sync = True
        clock.now = NOW + timedelta(hours=1)
        run = collector.run_once(only=["Sam"])
        assert run.complete
        assert not run.calendars[0].reused


@pytest.mark.unit
class TestFailures:
    def test_discovery_failure_is_incomplete_not_empty(self, engine):
        server = default_server()
        collector = make(engine, server)
        collector.run_once()
        server.fail_discovery = True
        run = collector.run_once()
        assert not run.complete
        assert run.discovery_error is not None
        assert {f.rule_id for f in run.findings} == {"DATA-011"}
        assert len(run.findings) == 2
        stored = SourceEventRepository(engine).active(run.window_start, run.window_end)
        # Three Sam occurrences and one ride survive the failed run.
        assert len(stored) == 4

    def test_lost_share_is_reported_and_keeps_history(self, engine):
        server = default_server()
        collector = make(engine, server)
        collector.run_once()
        server.calendars = [c for c in server.calendars if c.path != SAM_PATH]
        run = collector.run_once()
        lost = next(c for c in run.calendars if c.result.source.calendar_id == SAM_ID)
        assert not lost.result.complete
        assert lost.result.error == NOT_VISIBLE
        assert len(run.findings) == 1
        stored = SourceEventRepository(engine).active(
            run.window_start, run.window_end, [SAM_ID]
        )
        assert len(stored) == 3

    def test_query_failure_is_incomplete(self, engine):
        server = default_server()
        server.fail_query = {SAM_PATH}
        run = make(engine, server).run_once()
        sam = next(c for c in run.calendars if c.result.source.calendar_id == SAM_ID)
        assert not sam.result.complete
        assert sam.result.error == "REPORT failed: HTTP 500"
        source = CalendarSourceRepository(engine).get(SAM_ID)
        assert source is not None
        assert source.last_error == "REPORT failed: HTTP 500"

    def test_broken_object_keeps_good_ones_without_deleting(self, engine):
        server = default_server()
        clock = Clock(NOW)
        collector = make(engine, server, clock)
        collector.run_once()
        calendar = server.calendar(SAM_PATH)
        calendar.objects = {
            "lesson.ics": "garbage",
            "practice.ics": fixture_text("floating_time.ics"),
        }
        calendar.ctag = "ctag-2"
        clock.now = NOW + timedelta(hours=1)
        run = collector.run_once()
        sam = next(c for c in run.calendars if c.result.source.calendar_id == SAM_ID)
        assert not sam.result.complete
        assert sam.result.error == "1 calendar object(s) could not be parsed"
        assert len(sam.result.events) == 1
        assert sam.applied.deleted == 0
        assert len(run.findings) == 1

    def test_truncated_query_is_incomplete_and_deletes_nothing(self, engine):
        server = default_server()
        clock = Clock(NOW)
        collector = make(engine, server, clock)
        collector.run_once()
        calendar = server.calendar(SAM_PATH)
        # The server drops the lesson series and flags the result as truncated.
        calendar.objects = {"practice.ics": fixture_text("floating_time.ics")}
        calendar.ctag = "ctag-2"
        server.truncate_query = {SAM_PATH}
        clock.now = NOW + timedelta(hours=1)
        run = collector.run_once()
        assert not run.complete
        sam = next(c for c in run.calendars if c.result.source.calendar_id == SAM_ID)
        assert not sam.result.complete
        assert sam.result.error == (
            "calendar-query returned a non-200 entry (HTTP 507): partial result"
        )
        assert sam.applied.deleted == 0
        assert [f.rule_id for f in run.findings] == ["DATA-011"]
        repo = SourceEventRepository(engine)
        assert repo.deleted(run.window_start, run.window_end, [SAM_ID]) == ()
        stored = repo.active(run.window_start, run.window_end, [SAM_ID])
        assert len(stored) == 3

    def test_selected_calendar_without_events_gets_a_specific_error(self, engine):
        server = default_server()
        collector = make(engine, server)
        collector.run_once()
        server.calendar(SAM_PATH).components = ("VTODO",)
        run = collector.run_once()
        sam = next(c for c in run.calendars if c.result.source.calendar_id == SAM_ID)
        assert sam.result.error == NOT_EVENTS
        assert [f.rule_id for f in run.findings] == ["DATA-011"]

    def test_disabled_source_is_skipped(self, engine):
        server = default_server()
        collector = make(engine, server)
        collector.run_once()
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE calendar_source SET enabled=0")
        server.requests.clear()
        run = collector.run_once()
        assert run.calendars == ()
        assert reports(server) == []


@pytest.mark.unit
class TestSelection:
    def test_deselected_calendar_is_not_reported_as_lost(self, engine):
        server = default_server()
        collector = make(engine, server)
        collector.run_once()
        collector.config = replace(CONFIG, selected=())
        server.requests.clear()
        run = collector.run_once()
        assert run.complete
        assert run.findings == ()
        assert [c.result.source.calendar_id for c in run.calendars] == [TRANSPORT_ID]
        assert SAM_PATH not in reports(server)
        source = CalendarSourceRepository(engine).get(SAM_ID)
        assert source is not None
        assert source.last_error is None

    def test_unlisted_stored_source_is_not_reported(self, engine):
        register_person_calendar(engine, "cal-unlisted", "Jordan", "jordan")
        run = make(engine, default_server()).run_once()
        assert run.complete
        assert "cal-unlisted" not in {
            c.result.source.calendar_id for c in run.calendars
        }

    def test_unregistered_person_calendar_is_incomplete_not_stored(self, engine):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "DELETE FROM calendar_source WHERE calendar_id = ?", (SAM_ID,)
            )
            connection.exec_driver_sql("DELETE FROM person WHERE person_id = 'sam'")
        run = make(engine, default_server()).run_once()
        assert not run.complete
        sam = next(c for c in run.calendars if c.result.source.calendar_id == SAM_ID)
        assert not sam.result.complete
        assert "registry seed" in (sam.result.error or "")
        assert any(f.rule_id == "DATA-011" for f in run.findings)
        assert CalendarSourceRepository(engine).get(SAM_ID) is None
        assert (
            SourceEventRepository(engine).active(
                run.window_start, run.window_end, [SAM_ID]
            )
            == ()
        )

    def test_selected_by_calendar_id_is_still_collected(self, engine):
        server = default_server()
        collector = make(engine, server)
        collector.config = replace(CONFIG, selected=(SAM_ID,))
        run = collector.run_once()
        assert run.complete
        assert SAM_ID in {c.result.source.calendar_id for c in run.calendars}


@pytest.mark.unit
class TestDeletion:
    def test_removed_event_is_marked_deleted(self, engine):
        server = default_server()
        clock = Clock(NOW)
        collector = make(engine, server, clock)
        collector.run_once()
        calendar = server.calendar(TRANSPORT_PATH)
        calendar.objects = {}
        calendar.ctag = "ctag-2"
        clock.now = NOW + timedelta(hours=1)
        run = collector.run_once()
        transport = next(
            c for c in run.calendars if c.result.source.calendar_id == TRANSPORT_ID
        )
        assert transport.applied.deleted == 1
        repo = SourceEventRepository(engine)
        (gone,) = repo.deleted(run.window_start, run.window_end, [TRANSPORT_ID])
        assert gone.uid == "synthetic-ride-friday"


@pytest.mark.unit
class TestSourceFindings:
    def source(self, last_success: datetime | None) -> CalendarSource:
        return CalendarSource(
            "cal-x", "Jordan", CalendarKind.PERSON, last_success=last_success
        )

    def result(
        self, source: CalendarSource, *, complete: bool = True
    ) -> CollectionResult:
        return CollectionResult(
            source, (), NOW, NOW, NOW, complete, None if complete else "x"
        )

    def test_fresh_complete_source_is_fine(self):
        ok = self.result(self.source(NOW - timedelta(minutes=30)))
        assert source_findings([ok], NOW, timedelta(minutes=120)) == ()

    def test_stale_and_never_collected_sources(self):
        stale = self.result(self.source(NOW - timedelta(hours=3)))
        never = self.result(self.source(None))
        findings = source_findings([stale, never], NOW, timedelta(minutes=120))
        assert [f.rule_id for f in findings] == ["DATA-011", "DATA-011"]
        assert "recently enough" in findings[0].message

    def test_incomplete_source_names_the_problem(self):
        broken = self.result(self.source(NOW), complete=False)
        (finding,) = source_findings([broken], NOW, timedelta(minutes=120))
        assert "could not be read completely: x." in finding.message
