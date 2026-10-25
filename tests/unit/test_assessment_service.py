"""Assessment service: end to end from collected events to stored findings."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from fl_engine import NOW, at, routes
from fl_factories import CALENDAR, notes
from fl_service import PERSON_CALENDAR, collect, event, make_settings, seed_registry
from sqlalchemy import delete, insert, select

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.db.registry import RegistryStore
from family_logistics.db.schema import finding, trip_snapshot
from family_logistics.db.sources import CalendarSourceRepository
from family_logistics.domain.events import CalendarKind, CalendarSource
from family_logistics.domain.lifecycle import Lifecycle
from family_logistics.domain.models import OccurrenceKind, RunStatus
from family_logistics.domain.recurrence import occurrence_kind
from family_logistics.services.assessments import (
    AssessmentService,
    RunOutcome,
    SchoolSnapshot,
    activity_id,
    engine_settings,
    icloud_freshness,
)
from family_logistics.services.collector import collection_window

pytestmark = pytest.mark.unit

RIDE_START = at(15, 30)
RIDE_END = at(16, 15)

ROUTED_NOTES = notes(destinations=(("Sam", "Middle School"),), stop_order="Sam")
"""A ride whose explicit stop order makes the engine ask the route provider."""


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    seed_registry(engine)
    yield engine
    engine.dispose()


FRESH_SCHOOL = SchoolSnapshot((), NOW - timedelta(minutes=20))
"""A fresh Home Assistant snapshot with no school days (ADR 0007).

Without a snapshot a run is incomplete (DATA-011), so tests that are not about
the snapshot supply this one through ``make_service``.
"""


class SnapshotAssessmentService(AssessmentService):
    """The service under test, with a fresh school snapshot unless told otherwise.

    Pass ``school=None`` to run without a snapshot; any other value replaces it.
    """

    def run(
        self,
        trigger: str = "on_demand",
        school: SchoolSnapshot | None = FRESH_SCHOOL,
    ) -> RunOutcome:
        return super().run(trigger, school)


def make_service(engine, *, provider=None, clock=lambda: NOW, **settings):
    return SnapshotAssessmentService(
        engine,
        make_settings(**settings),
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


def explode(*args, **kwargs):
    msg = "1 Example Way is unreachable"
    raise RuntimeError(msg)


class UnreachableRoutes:
    """A route provider that fails on every call."""

    name = "unreachable"
    estimate = explode
    optimize = explode


def store_snapshot_row(engine, payload):
    """Insert a trip_snapshot row as it would sit in a damaged database."""
    with engine.begin() as connection:
        connection.execute(
            insert(trip_snapshot).values(
                trip_key="damaged-row", payload=payload, run_id=None, updated_at=NOW
            )
        )


def stored_state(engine):
    """Return the finding rows and the snapshot rows exactly as stored."""
    with engine.connect() as connection:
        findings = [
            dict(row)
            for row in connection.execute(
                select(finding).order_by(finding.c.issue_id)
            ).mappings()
        ]
        trips = [
            dict(row)
            for row in connection.execute(
                select(trip_snapshot).order_by(trip_snapshot.c.trip_key)
            ).mappings()
        ]
    return findings, trips


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
        expected_start, expected_end = collection_window(NOW, zone, 0, 14)
        assert record.window_start == expected_start
        assert record.window_end == expected_end
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


class TestRunSequences:
    def test_a_failed_run_between_two_good_runs_changes_nothing(self, engine):
        collect_all(
            engine,
            [ride("evt-1", text=ROUTED_NOTES), ride("evt-2", text="not a template")],
        )
        first = make_service(engine).run()
        assert first.result.run_status is not RunStatus.FAILED
        before = stored_state(engine)
        assert before[0]
        assert before[1]

        failed = make_service(engine, provider=UnreachableRoutes()).run()
        assert failed.result.run_status is RunStatus.FAILED
        assert stored_state(engine) == before

        third = make_service(engine).run()
        assert third.result.run_status is not RunStatus.FAILED
        parse = [
            t for t in third.result.findings if t.finding.rule_id.startswith("PARSE")
        ]
        assert parse
        assert {t.lifecycle for t in parse} == {Lifecycle.ONGOING}

    def test_a_finding_that_returns_after_resolution_is_regressed(self, engine):
        service = make_service(engine)
        collect_all(engine, [ride(text="not a template")])
        service.run()
        collect_all(engine, [ride()])
        fixed = service.run()
        resolved = {
            f.issue_id for f in fixed.result.resolved if f.rule_id.startswith("PARSE")
        }
        assert resolved

        collect_all(engine, [ride(text="not a template")])
        again = service.run()
        parse = [
            t for t in again.result.findings if t.finding.rule_id.startswith("PARSE")
        ]
        assert {t.finding.issue_id for t in parse} == resolved
        assert {t.lifecycle for t in parse} == {Lifecycle.REGRESSED}
        history = AssessmentRepository(engine).load_history()
        assert resolved <= {f.issue_id for f in history.open_findings}
        assert not resolved & history.resolved_issue_ids


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


class TestFailedRuns:
    def test_a_failing_route_provider_stores_a_failed_run_and_keeps_findings(
        self, engine
    ):
        collect_all(
            engine,
            [ride("evt-1", text=ROUTED_NOTES), ride("evt-2", text="not a template")],
        )
        first = make_service(engine).run()
        assert first.result.run_status is not RunStatus.FAILED
        assert first.result.findings

        failed = make_service(engine, provider=UnreachableRoutes()).run()

        assert failed.result.run_status is RunStatus.FAILED
        record = AssessmentRepository(engine).get_run(failed.run_id)
        assert record is not None
        assert record.status is RunStatus.FAILED
        assert record.finished_at is not None
        assert record.error == "assessment raised RuntimeError"
        kept = AssessmentRepository(engine).load_history().open_findings
        assert {f.issue_id for f in kept} == {
            t.finding.issue_id for t in first.result.findings
        }

    @pytest.mark.parametrize(
        "payload",
        [{"version": 99}, [1, 2]],
        ids=["unknown-version", "not-an-object"],
    )
    def test_a_corrupt_snapshot_row_stores_a_failed_run(self, engine, payload):
        collect_all(engine, [ride()])
        store_snapshot_row(engine, payload)

        outcome = make_service(engine).run()

        assert outcome.result.run_status is RunStatus.FAILED
        assert outcome.result.findings == ()
        record = AssessmentRepository(engine).get_run(outcome.run_id)
        assert record is not None
        assert record.status is RunStatus.FAILED
        assert record.finished_at == NOW
        assert record.error == "assessment raised ValidationError"
        with engine.connect() as connection:
            keys = connection.execute(select(trip_snapshot.c.trip_key)).scalars().all()
        assert keys == ["damaged-row"]

    def test_a_run_completes_once_the_corrupt_row_is_removed(self, engine):
        collect_all(engine, [ride()])
        store_snapshot_row(engine, {"version": 99})
        service = make_service(engine)
        assert service.run().result.run_status is RunStatus.FAILED

        with engine.begin() as connection:
            connection.execute(delete(trip_snapshot))
        outcome = service.run()

        assert outcome.result.run_status is not RunStatus.FAILED
        assert len(AssessmentRepository(engine).load_history().trips) == 1

    def test_a_begin_run_failure_propagates_and_writes_no_run(
        self, engine, monkeypatch
    ):
        collect_all(engine, [ride()])

        def broken(self, *args, **kwargs):
            msg = "database is locked"
            raise RuntimeError(msg)

        monkeypatch.setattr(AssessmentRepository, "begin_run", broken)
        with pytest.raises(RuntimeError, match="database is locked"):
            make_service(engine).run()
        assert AssessmentRepository(engine).recent_runs() == ()

    def test_an_unknown_trigger_is_rejected_before_any_run_is_written(self, engine):
        with pytest.raises(ValueError, match="unknown trigger"):
            make_service(engine).run("hourly")
        assert AssessmentRepository(engine).recent_runs() == ()

    def test_a_naive_school_snapshot_time_stores_a_failed_run(self, engine):
        collect_all(engine, [ride()])
        naive = SchoolSnapshot((), NOW.replace(tzinfo=None))

        outcome = make_service(engine).run(school=naive)

        assert outcome.result.run_status is RunStatus.FAILED
        record = AssessmentRepository(engine).get_run(outcome.run_id)
        assert record is not None
        assert record.finished_at is not None
        assert record.error == "assessment raised ValidationError"


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
        old = NOW - timedelta(hours=5)
        collect_all(engine, at_time=old)
        collect(engine, CALENDAR, (), at_time=NOW, complete=False)
        collect(engine, PERSON_CALENDAR, (), at_time=NOW)

        stored = CalendarSourceRepository(engine).get(CALENDAR)
        assert stored is not None
        assert stored.last_success == old
        outcome = make_service(engine).run()

        # The person calendar is fresh, so only the transport calendar's older
        # success can keep the source stale: the oldest calendar decides.
        assert outcome.result.run_status is RunStatus.INCOMPLETE
        stale = [t for t in outcome.result.findings if t.finding.rule_id == "DATA-011"]
        assert [t.finding.entity_keys for t in stale] == [("source:icloud",)]

    def test_an_unselected_calendar_that_was_never_collected_is_not_judged(
        self, engine
    ):
        collect(engine, CALENDAR, (), at_time=NOW - timedelta(minutes=20))
        outcome = make_service(engine, caldav_calendars="").run()
        assert not any(t.finding.rule_id == "DATA-011" for t in outcome.result.findings)
        assert outcome.result.run_status is RunStatus.CLEAR

    @pytest.mark.parametrize("selection", [PERSON_CALENDAR, "Sam"])
    def test_a_selected_calendar_that_was_never_collected_is_flagged(
        self, engine, selection
    ):
        collect(engine, CALENDAR, (), at_time=NOW - timedelta(minutes=20))
        outcome = make_service(engine, caldav_calendars=selection).run()
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

    def test_without_a_school_snapshot_the_run_is_incomplete_never_clear(self, engine):
        # ADR 0007: a missing snapshot is never read as "no school". It raises
        # DATA-011 for ha_snapshot and makes the run incomplete (design rule 4).
        collect_all(engine, at_time=NOW - timedelta(minutes=20))
        outcome = make_service(engine).run(school=None)
        assert outcome.result.run_status is RunStatus.INCOMPLETE
        assert [
            (t.finding.rule_id, t.finding.entity_keys) for t in outcome.result.findings
        ] == [("DATA-011", ("source:ha_snapshot",))]

    def test_a_missing_snapshot_and_a_never_collected_calendar_are_both_flagged(
        self, engine
    ):
        # Two distinct sources are unusable, so there are two DATA-011 findings
        # (one per source key); neither hides the other. No event is collected, so
        # no critical finding can lift the status above incomplete.
        outcome = make_service(engine).run(school=None)
        assert outcome.result.run_status is RunStatus.INCOMPLETE
        assert sorted(
            (t.finding.rule_id, t.finding.entity_keys) for t in outcome.result.findings
        ) == [
            ("DATA-011", ("source:ha_snapshot",)),
            ("DATA-011", ("source:icloud",)),
        ]

    def test_the_service_default_is_no_snapshot_and_so_incomplete(self, engine):
        # The unwrapped service is what a future production caller gets when it
        # omits ``school``: the default is ``None``, which means no snapshot.
        collect_all(engine, at_time=NOW - timedelta(minutes=20))
        service = AssessmentService(
            engine, make_settings(), routes(), clock=lambda: NOW
        )
        outcome = service.run()
        assert outcome.result.run_status is RunStatus.INCOMPLETE
        assert [
            (t.finding.rule_id, t.finding.entity_keys) for t in outcome.result.findings
        ] == [("DATA-011", ("source:ha_snapshot",))]

    def test_a_stale_school_snapshot_makes_the_run_incomplete_never_clear(self, engine):
        # The Home Assistant limit is 26 hours (domain/context.py); a snapshot
        # three days old is far past it, while the calendars are fresh.
        collect_all(engine, at_time=NOW - timedelta(minutes=20))
        stale = SchoolSnapshot((), NOW - timedelta(days=3))
        outcome = make_service(engine).run(school=stale)
        assert outcome.result.run_status is RunStatus.INCOMPLETE
        assert [
            (t.finding.rule_id, t.finding.entity_keys) for t in outcome.result.findings
        ] == [("DATA-011", ("source:ha_snapshot",))]

    def test_with_a_fresh_school_snapshot_a_finding_free_run_is_clear(self, engine):
        collect_all(engine, at_time=NOW - timedelta(minutes=20))
        outcome = make_service(engine).run(
            school=SchoolSnapshot((), NOW - timedelta(minutes=5))
        )
        assert outcome.result.findings == ()
        assert outcome.result.run_status is RunStatus.CLEAR

    def test_oldest_calendar_decides(self):
        old, new = NOW - timedelta(hours=3), NOW
        sources = [
            CalendarSource("a", "A", CalendarKind.TRANSPORT, last_success=old),
            CalendarSource(
                "b", "B", CalendarKind.PERSON, last_success=new, person_id="sam"
            ),
        ]
        assert icloud_freshness(sources) == ("icloud", old)
        assert icloud_freshness([]) == ("icloud", None)
        sources.append(CalendarSource("c", "C", CalendarKind.PERSON, person_id="sam"))
        assert icloud_freshness(sources) == ("icloud", None)

    def test_icloud_limit_follows_the_collector_staleness_setting(self):
        settings = make_settings(source_max_staleness_minutes=45)
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

    @pytest.mark.parametrize(
        ("place", "unregistered"),
        [("Somewhere Else", True), ("Music Studio", False)],
        ids=["unregistered", "registered"],
    )
    def test_only_an_unregistered_location_is_reported_unresolved(
        self, engine, place, unregistered
    ):
        timed = event(PERSON_CALENDAR, "lesson", at(15, 0), at(16, 0), location=place)
        collect_all(engine, person=[timed])
        outcome = make_service(engine).run()
        rules = {t.finding.rule_id for t in outcome.result.findings}
        # An unregistered place is matched by time only (ROUTE-010); a
        # registered one is not. Neither is covered, so the child has no ride.
        assert ("ROUTE-010" in rules) is unregistered
        assert "RIDE-005" in rules
        assert outcome.result.run_status is RunStatus.ATTENTION_REQUIRED

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
            RegistryStore(connection).set_calendar_enabled(CALENDAR, enabled=False)
        outcome = make_service(engine).run()
        assert not any(
            t.finding.rule_id.startswith("PARSE") for t in outcome.result.findings
        )
