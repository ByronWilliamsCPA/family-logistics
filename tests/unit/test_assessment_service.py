"""Assessment service: end to end from collected events to stored findings."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from fl_engine import NOW, at, routes
from fl_factories import CALENDAR, notes
from fl_service import PERSON_CALENDAR, collect, event, seed_registry

from family_logistics.core.config import Settings
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.domain.events import CalendarSource
from family_logistics.domain.lifecycle import Lifecycle
from family_logistics.domain.models import OccurrenceKind, RunStatus
from family_logistics.services.assessments import (
    AssessmentService,
    SchoolSnapshot,
    activity_id,
    engine_settings,
    icloud_freshness,
    occurrence_kind,
)
from family_logistics.services.collector import collection_window

pytestmark = pytest.mark.unit

RIDE_START = at(15, 30)
RIDE_END = at(16, 15)


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    seed_registry(engine)
    yield engine
    engine.dispose()


def make_service(engine, *, provider=None, clock=lambda: NOW):
    return AssessmentService(
        engine,
        Settings(),  # type: ignore[call-arg]
        provider or routes(),
        clock=clock,
    )


def ride(uid="evt-1", text=None, **kwargs):
    return event(
        CALENDAR,
        uid,
        RIDE_START,
        RIDE_END,
        summary="Ride",
        description=text if text is not None else notes(pickup="Music Studio"),
        **kwargs,
    )


def collect_all(engine, rides=(), person=(), at_time=NOW):
    collect(engine, CALENDAR, tuple(rides), at_time=at_time)
    collect(engine, PERSON_CALENDAR, tuple(person), at_time=at_time)


class TestRun:
    def test_run_stores_a_finished_run(self, engine):
        collect_all(engine, [ride()])
        outcome = make_service(engine).run("daily")
        record = AssessmentRepository(engine).get_run(outcome.run_id)
        assert record is not None
        assert record.trigger == "daily"
        assert record.status is outcome.result.run_status
        assert record.finished_at is not None
        assert record.status is not RunStatus.FAILED

    def test_window_covers_today_and_the_configured_days_ahead(self, engine):
        collect_all(engine)
        outcome = make_service(engine).run()
        record = AssessmentRepository(engine).get_run(outcome.run_id)
        assert record is not None
        zone = ZoneInfo("America/Los_Angeles")
        assert (record.window_start, record.window_end) == collection_window(
            NOW, zone, 0, 14
        )
        assert record.window_start == at(0, 0)

    def test_findings_are_persisted_and_carried_to_the_next_run(self, engine):
        collect_all(engine, [ride(text="not a template")])
        service = make_service(engine)
        first = service.run()
        assert any(t.finding.rule_id.startswith("PARSE") for t in first.result.findings)
        assert {t.lifecycle for t in first.result.findings} == {Lifecycle.NEW}
        second = service.run()
        parse = [
            t for t in second.result.findings if t.finding.rule_id.startswith("PARSE")
        ]
        assert parse
        assert {t.lifecycle for t in parse} == {Lifecycle.ONGOING}

    def test_a_fixed_ride_resolves_its_finding(self, engine):
        collect_all(engine, [ride(text="not a template")])
        service = make_service(engine)
        service.run()
        collect_all(engine, [ride()])
        outcome = service.run()
        assert any(f.rule_id.startswith("PARSE") for f in outcome.result.resolved)
        history = AssessmentRepository(engine).load_history()
        assert not any(f.rule_id.startswith("PARSE") for f in history.open_findings)

    def test_removing_an_external_passenger_is_caught_against_the_previous_run(
        self, engine
    ):
        both = notes(
            family="Sam",
            external="Passenger E",
            destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
        )
        collect_all(engine, [ride(text=both)])
        service = make_service(engine)
        service.run()
        collect_all(engine, [ride(text=notes(family="Sam"))])
        outcome = service.run()
        assert any(t.finding.rule_id == "PASS-003" for t in outcome.result.findings)


def explode(*args, **kwargs):
    msg = "1 Example Way is unreachable"
    raise RuntimeError(msg)


class TestNeverClearOnFailure:
    def test_a_raising_engine_stores_a_failed_run(self, engine, monkeypatch):
        collect_all(engine, [ride()])
        monkeypatch.setattr("family_logistics.services.assessments.assess", explode)
        outcome = make_service(engine).run()
        assert outcome.result.run_status is RunStatus.FAILED
        record = AssessmentRepository(engine).get_run(outcome.run_id)
        assert record is not None
        assert record.status is RunStatus.FAILED
        assert record.error == "assessment raised RuntimeError"
        assert "Example Way" not in (record.error or "")

    def test_a_failed_run_keeps_earlier_findings_open(self, engine, monkeypatch):
        collect_all(engine, [ride(text="not a template")])
        service = make_service(engine)
        first = service.run()
        assert first.result.findings
        monkeypatch.setattr("family_logistics.services.assessments.assess", explode)
        failed = service.run()
        assert failed.result.run_status is RunStatus.FAILED
        kept = AssessmentRepository(engine).load_history().open_findings
        assert {f.issue_id for f in kept} == {
            t.finding.issue_id for t in first.result.findings
        }

    def test_a_failed_run_does_not_replace_the_trip_snapshot(self, engine, monkeypatch):
        collect_all(engine, [ride()])
        service = make_service(engine)
        service.run()
        before = AssessmentRepository(engine).load_history().trips
        assert before
        monkeypatch.setattr("family_logistics.services.assessments.assess", explode)
        service.run()
        assert AssessmentRepository(engine).load_history().trips == before

    def test_a_run_that_never_finished_reads_as_failed(self, engine):
        repo = AssessmentRepository(engine)
        run_id = repo.begin_run(
            "on_demand", at(0, 0), at(23, 59), NOW, rules_version="x"
        )
        record = repo.get_run(run_id)
        assert record is not None
        assert record.status is RunStatus.FAILED


class TestFreshness:
    def test_never_collected_calendars_make_the_run_incomplete(self, engine):
        outcome = make_service(engine).run()
        assert outcome.result.run_status in {
            RunStatus.INCOMPLETE,
            RunStatus.ATTENTION_REQUIRED,
        }
        assert any(t.finding.rule_id == "DATA-011" for t in outcome.result.findings)

    def test_stale_collection_is_flagged(self, engine):
        collect_all(engine, at_time=NOW - timedelta(hours=5))
        outcome = make_service(engine).run()
        assert any(t.finding.rule_id == "DATA-011" for t in outcome.result.findings)

    def test_fresh_collection_is_not_flagged(self, engine):
        collect_all(engine, at_time=NOW - timedelta(minutes=20))
        outcome = make_service(engine).run()
        assert not any(t.finding.rule_id == "DATA-011" for t in outcome.result.findings)

    def test_incomplete_collection_keeps_the_older_success(self, engine):
        collect_all(engine, at_time=NOW - timedelta(hours=5))
        collect(engine, CALENDAR, (), at_time=NOW, complete=False)
        outcome = make_service(engine).run()
        assert any(t.finding.rule_id == "DATA-011" for t in outcome.result.findings)

    def test_school_snapshot_adds_its_own_freshness(self, engine):
        collect_all(engine, at_time=NOW - timedelta(minutes=20))
        stale = SchoolSnapshot((), NOW - timedelta(days=3))
        outcome = make_service(engine).run(school=stale)
        assert any(
            t.finding.rule_id == "DATA-011"
            and "ha_snapshot" in "".join(t.finding.entity_keys)
            for t in outcome.result.findings
        )

    def test_oldest_calendar_decides(self):
        old, new = NOW - timedelta(hours=3), NOW
        sources = [
            CalendarSource("a", "A", "transport", last_success=old),  # type: ignore[arg-type]
            CalendarSource("b", "B", "person", last_success=new, person_id="sam"),  # type: ignore[arg-type]
        ]
        assert icloud_freshness(sources) == ("icloud", old)
        assert icloud_freshness([]) == ("icloud", None)
        sources.append(CalendarSource("c", "C", "person", person_id="sam"))  # type: ignore[arg-type]
        assert icloud_freshness(sources) == ("icloud", None)

    def test_icloud_limit_follows_the_collector_staleness_setting(self):
        settings = Settings(source_max_staleness_minutes=45)  # type: ignore[call-arg]
        assert engine_settings(settings).max_age_for("icloud") == timedelta(minutes=45)
        assert engine_settings(settings).max_age_for("ha_snapshot") == timedelta(
            hours=26
        )


class TestActivities:
    def test_person_events_become_activities_and_all_day_events_do_not(self, engine):
        timed = event(
            PERSON_CALENDAR, "lesson", at(15, 0), at(16, 0), location="Music Studio"
        )
        holiday = event(PERSON_CALENDAR, "day-off", at(0, 0), at(23, 59), all_day=True)
        cancelled = event(
            PERSON_CALENDAR, "gone", at(17, 0), at(18, 0), status="cancelled"
        )
        collect_all(engine, person=[timed, holiday, cancelled])
        outcome = make_service(engine).run()
        assert outcome.result.timelines
        stays = [s for t in outcome.result.timelines for s in t.stays]
        assert len(stays) == 1

    def test_unknown_location_stays_unresolved(self, engine):
        timed = event(
            PERSON_CALENDAR, "lesson", at(15, 0), at(16, 0), location="Somewhere Else"
        )
        collect_all(engine, person=[timed])
        outcome = make_service(engine).run()
        assert outcome.result.run_status is not RunStatus.CLEAR

    def test_activity_id_hides_the_calendar_identifiers(self):
        item = event(PERSON_CALENDAR, "uid-1", at(15, 0), at(16, 0))
        assert activity_id(item).startswith("act-")
        assert PERSON_CALENDAR not in activity_id(item)
        assert "uid-1" not in activity_id(item)


class TestRideParsing:
    def test_cancelled_rides_are_ignored(self, engine):
        collect_all(engine, [ride(status="cancelled", text="not a template")])
        outcome = make_service(engine).run()
        assert not any(
            t.finding.rule_id.startswith("PARSE") for t in outcome.result.findings
        )

    def test_occurrence_kind_follows_the_collected_flags(self):
        assert occurrence_kind(ride()) is OccurrenceKind.SINGLE
        assert occurrence_kind(ride(has_rrule=True)) is OccurrenceKind.SERIES_INSTANCE
        assert (
            occurrence_kind(ride(has_rrule=True, is_override=True, recurrence_id="r"))
            is OccurrenceKind.OVERRIDE
        )

    def test_disabled_calendars_are_not_read(self, engine):
        collect_all(engine, [ride(text="not a template")])
        with engine.begin() as connection:
            from family_logistics.db.registry import RegistryStore

            RegistryStore(connection).set_calendar_enabled(CALENDAR, enabled=False)
        outcome = make_service(engine).run()
        assert not any(
            t.finding.rule_id.startswith("PARSE") for t in outcome.result.findings
        )
