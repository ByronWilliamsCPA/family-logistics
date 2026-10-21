"""Coverage, driver and per-ride rules, exercised through ``assess``."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fl_engine import (
    MINUTE,
    NOW,
    PEOPLE,
    THURSDAY,
    activity,
    assessment,
    at,
    default,
    routes,
    school,
)
from fl_factories import notes, trip

from family_logistics.adapters.routing.base import StaticRouteProvider
from family_logistics.domain.confirmation import confirmation_code
from family_logistics.domain.engine import assess
from family_logistics.domain.events import (
    SchoolDayKind,
    TransportDirection,
    TransportMode,
)
from family_logistics.domain.models import RunStatus, Severity

pytestmark = pytest.mark.unit


class RecordingRoutes(StaticRouteProvider):
    """Static provider that records every requested leg."""

    def __init__(self, matrix):
        super().__init__(matrix)
        self.calls = []

    def estimate(self, origin, destination, departure_time, mode):
        self.calls.append((origin, destination))
        return super().estimate(origin, destination, departure_time, mode)


def confirmed(t):
    return dataclasses.replace(t, confirm_code=confirmation_code(t))


def ride(
    uid,
    start,
    end,
    *,
    pickup="Music Studio",
    family="Sam",
    destinations=(("Sam", "HOME"),),
    driver="Riley",
    status="confirmed",
    external="",
    stop_order="AUTO",
):
    t = trip(
        notes(
            pickup=pickup,
            family=family,
            destinations=destinations,
            driver=driver,
            status=status,
            external=external,
            stop_order=stop_order,
        ),
        uid=uid,
        start=start,
        end=end,
    )
    return confirmed(t) if status == "confirmed" else t


def run(inp, provider=None, now=NOW):
    return assess(inp, provider or routes(), now=now)


def rules(result, rule_id=None):
    found = [t.finding for t in result.findings]
    if rule_id is None:
        return sorted(f.rule_id for f in found)
    return [f for f in found if f.rule_id == rule_id]


LESSON = activity("lesson", ("sam",), "studio", at(16), at(17))
TO_LESSON = ride(
    "to", at(15, 30), at(16), pickup="Home", destinations=(("Sam", "Music Studio"),)
)
FROM_LESSON = ride("from", at(17), at(17, 30))


class TestCoverage:
    def test_fully_covered_day_is_clear(self):
        result = run(assessment(activities=(LESSON,), trips=(TO_LESSON, FROM_LESSON)))
        assert rules(result) == []
        assert result.run_status is RunStatus.CLEAR

    def test_missing_return_is_ride_005(self):
        result = run(assessment(activities=(LESSON,), trips=(TO_LESSON,)))
        missing = rules(result, "RIDE-005")
        assert len(missing) == 1
        assert missing[0].severity is Severity.CRITICAL
        assert "to home" in missing[0].message
        assert result.run_status is RunStatus.ATTENTION_REQUIRED

    def test_late_pickup_beyond_tolerance_does_not_cover(self):
        late = ride("from", at(17, 45), at(18, 15))
        result = run(assessment(activities=(LESSON,), trips=(TO_LESSON, late)))
        assert len(rules(result, "RIDE-005")) == 1

    def test_ride_to_wrong_place_does_not_cover(self):
        wrong = ride("from", at(17), at(17, 30), destinations=(("Sam", "XC Practice"),))
        result = run(assessment(activities=(LESSON,), trips=(TO_LESSON, wrong)))
        assert len(rules(result, "RIDE-005")) == 1

    def test_bike_default_covers_school_moves(self):
        inp = assessment(
            school_days=(school("alex", at(8), at(15)),),
            defaults=(
                default("alex", TransportDirection.TO_SCHOOL, TransportMode.BIKE),
                default("alex", TransportDirection.FROM_SCHOOL, TransportMode.BIKE),
            ),
        )
        assert rules(run(inp)) == []

    def test_bus_does_not_cover_a_stay_extended_past_the_bell(self):
        inp = assessment(
            school_days=(school("alex", at(8), at(15)),),
            activities=(activity("xc", ("alex",), "high-school", at(15), at(17, 30)),),
            defaults=(
                default("alex", TransportDirection.TO_SCHOOL, TransportMode.BUS),
                default("alex", TransportDirection.FROM_SCHOOL, TransportMode.BUS),
            ),
        )
        missing = rules(run(inp), "RIDE-005")
        assert len(missing) == 1
        assert "past the bus" in missing[0].message

    def test_bus_on_early_release_asks_for_confirmation(self):
        inp = assessment(
            school_days=(
                school("alex", at(8), at(12, 30), SchoolDayKind.EARLY_RELEASE),
            ),
            defaults=(
                default("alex", TransportDirection.TO_SCHOOL, TransportMode.BUS),
                default("alex", TransportDirection.FROM_SCHOOL, TransportMode.BUS),
            ),
        )
        assert rules(run(inp)) == ["MODE-015", "MODE-015"]

    def test_parent_default_without_driver_is_mode_015(self):
        inp = assessment(
            school_days=(school("jordan", at(8), at(15), location_id="middle-school"),),
            defaults=(
                default(
                    "jordan",
                    TransportDirection.TO_SCHOOL,
                    TransportMode.PARENT,
                    school_id="middle-school",
                ),
                default(
                    "jordan",
                    TransportDirection.FROM_SCHOOL,
                    TransportMode.PARENT,
                    driver_id="riley",
                    school_id="middle-school",
                ),
            ),
        )
        assert rules(run(inp)) == ["MODE-015"]

    def test_parent_default_driver_busy_is_driver_006(self):
        inp = assessment(
            school_days=(school("jordan", at(8), at(15), location_id="middle-school"),),
            activities=(activity("work", ("riley",), "away-meet", at(14), at(16)),),
            defaults=(
                default(
                    "jordan",
                    TransportDirection.FROM_SCHOOL,
                    TransportMode.PARENT,
                    driver_id="riley",
                    school_id="middle-school",
                ),
                default(
                    "jordan",
                    TransportDirection.TO_SCHOOL,
                    TransportMode.WALK,
                    school_id="middle-school",
                ),
            ),
        )
        busy = rules(run(inp), "DRIVER-006")
        assert len(busy) == 1
        assert "Riley" in busy[0].message

    def test_parent_default_driver_at_the_shared_home_is_not_busy(self):
        inp = assessment(
            school_days=(school("jordan", at(8), at(15), location_id="middle-school"),),
            activities=(activity("work", ("riley",), "home", at(14), at(16)),),
            defaults=(
                default(
                    "jordan",
                    TransportDirection.FROM_SCHOOL,
                    TransportMode.PARENT,
                    driver_id="riley",
                    school_id="middle-school",
                ),
                default(
                    "jordan",
                    TransportDirection.TO_SCHOOL,
                    TransportMode.WALK,
                    school_id="middle-school",
                ),
            ),
        )
        assert rules(run(inp), "DRIVER-006") == []

    def test_same_lesson_on_consecutive_days_needs_both_rides(self):
        thursday = activity(
            "lesson-thu",
            ("sam",),
            "studio",
            at(16, day=THURSDAY),
            at(17, day=THURSDAY),
        )
        missing = rules(run(assessment(activities=(thursday, LESSON))), "RIDE-005")
        assert sorted(f.entity_keys[2:] for f in missing) == [
            ("from:home:sam", "to:studio"),
            ("from:studio", "to:home:sam"),
        ]

    def test_unregistered_place_is_incomplete(self):
        park = activity("park", ("sam",), None, at(16), at(17), text="Some Park")
        result = run(assessment(activities=(park,)))
        assert "ROUTE-010" in rules(result)
        assert result.run_status is RunStatus.ATTENTION_REQUIRED

    def test_past_moves_are_not_evaluated(self):
        result = run(assessment(activities=(LESSON,)), now=at(20))
        assert rules(result, "RIDE-005") == []
        assert rules(result) == ["DATA-011"]

    def test_double_booked_child_is_ride_005(self):
        inp = assessment(
            activities=(
                activity("a", ("sam",), "xc", at(15), at(17)),
                activity("b", ("sam",), "studio", at(16), at(18)),
            )
        )
        clash = [f for f in rules(run(inp), "RIDE-005") if "two places" in f.message]
        assert len(clash) == 1


class TestSchoolBaseline:
    def test_default_without_schedule_is_school_009(self):
        inp = assessment(
            defaults=(
                default("alex", TransportDirection.TO_SCHOOL, TransportMode.BIKE),
            )
        )
        assert rules(run(inp)) == ["SCHOOL-009"]

    def test_unknown_schedule_is_school_009(self):
        inp = assessment(
            school_days=(school("alex", at(8), at(15), SchoolDayKind.UNKNOWN),),
            defaults=(
                default("alex", TransportDirection.TO_SCHOOL, TransportMode.BIKE),
                default("alex", TransportDirection.FROM_SCHOOL, TransportMode.BIKE),
            ),
        )
        assert rules(run(inp)) == ["SCHOOL-009"]


class TestDriver:
    def test_unauthorized_driver_flags_every_passenger(self):
        t = ride(
            "kid-driver",
            at(17),
            at(17, 30),
            family="Sam, Jordan",
            destinations=(("Sam", "HOME"), ("Jordan", "HOME")),
        )
        people = tuple(
            dataclasses.replace(p, can_drive=False) if p.person_id == "riley" else p
            for p in PEOPLE
        )
        flagged = rules(run(assessment(trips=(t,), people=people)), "DRIVER-006")
        assert len(flagged) == 2

    def test_driver_own_event_elsewhere_conflicts(self):
        meet = activity("meet", ("riley",), "away-meet", at(16), at(19))
        result = run(
            assessment(activities=(LESSON, meet), trips=(TO_LESSON, FROM_LESSON))
        )
        flagged = rules(result, "DRIVER-006")
        assert [f.entity_keys[0] for f in flagged] == [FROM_LESSON.key.as_str()]
        assert flagged[0].severity is Severity.CRITICAL

    def test_driver_at_the_pickup_place_is_not_a_conflict(self):
        watching = activity("watch", ("riley",), "studio", at(16), at(17, 15))
        result = run(assessment(activities=(LESSON, watching), trips=(FROM_LESSON,)))
        assert rules(result, "DRIVER-006") == []

    def test_driver_at_home_is_not_a_conflict_for_a_home_pickup(self):
        at_home = activity("desk", ("riley",), "home", at(15), at(15, 45))
        result = run(assessment(activities=(LESSON, at_home), trips=(TO_LESSON,)))
        assert rules(result, "DRIVER-006") == []

    def test_driver_at_home_conflicts_with_a_pickup_elsewhere(self):
        at_home = activity("desk", ("riley",), "home", at(16, 50), at(17, 20))
        result = run(assessment(activities=(LESSON, at_home), trips=(FROM_LESSON,)))
        flagged = rules(result, "DRIVER-006")
        assert [f.severity for f in flagged] == [Severity.CRITICAL]

    def test_unregistered_driver_event_is_a_warning(self):
        errand = activity("errand", ("riley",), None, at(17), at(17, 20), text="Errand")
        result = run(assessment(activities=(errand,), trips=(FROM_LESSON,)))
        flagged = rules(result, "DRIVER-006")
        assert [f.severity for f in flagged] == [Severity.WARNING]

    def test_overlapping_rides_from_different_places(self):
        other = ride(
            "other",
            at(17, 10),
            at(17, 40),
            pickup="XC Practice",
            family="Jordan",
            destinations=(("Jordan", "HOME"),),
        )
        flagged = rules(run(assessment(trips=(FROM_LESSON, other))), "DRIVER-006")
        assert {f.entity_keys[1] for f in flagged} == {
            "passenger:sam",
            "passenger:jordan",
        }

    def test_cannot_reach_next_pickup(self):
        first = ride("first", at(16), at(16, 30), pickup="XC Practice")
        second = ride(
            "second",
            at(16, 40),
            at(17, 10),
            family="Jordan",
            destinations=(("Jordan", "HOME"),),
        )
        slow = StaticRouteProvider({("home", "studio"): 40 * MINUTE})
        result = run(assessment(trips=(first, second)), slow)
        assert [f.rule_id for f in rules(result, "DRIVER-006")] == ["DRIVER-006"]

    def test_unknown_travel_time_is_route_010(self):
        first = ride("first", at(16), at(16, 30), pickup="XC Practice")
        second = ride(
            "second",
            at(16, 40),
            at(17, 10),
            family="Jordan",
            destinations=(("Jordan", "HOME"),),
        )
        result = run(assessment(trips=(first, second)), StaticRouteProvider({}))
        assert any("pickup" in f.message for f in rules(result, "ROUTE-010"))

    def test_next_pickup_at_the_same_home_needs_no_route(self):
        dropoff = ride("drop", at(16), at(16, 30))
        outbound = ride(
            "out",
            at(16, 40),
            at(17, 10),
            pickup="Home",
            family="Jordan",
            destinations=(("Jordan", "Music Studio"),),
        )
        provider = RecordingRoutes({})
        result = run(assessment(trips=(dropoff, outbound)), provider)
        assert not any("pickup" in f.message for f in rules(result, "ROUTE-010"))
        assert [c for c in provider.calls if c[0] == c[1]] == []
        assert rules(result, "DRIVER-006") == []


class TestRide:
    def test_unverified_destination_imminent_is_critical(self):
        t = ride("new", at(9), at(9, 30), destinations=(("Sam", "New Place"),))
        flagged = rules(run(assessment(trips=(t,))), "DEST-004")
        assert [f.severity for f in flagged] == [Severity.CRITICAL]

    def test_unverified_destination_later_is_warning(self):
        t = ride("new", at(18), at(18, 30), destinations=(("Sam", "New Place"),))
        flagged = rules(run(assessment(trips=(t,))), "DEST-004")
        assert [f.severity for f in flagged] == [Severity.WARNING]

    def test_deadline_missed_in_every_order_is_time_007(self):
        lesson = activity("lesson", ("sam",), "studio", at(16), at(17))
        late = ride(
            "to",
            at(15, 50),
            at(16, 10),
            pickup="Home",
            destinations=(("Sam", "Music Studio"),),
        )
        result = run(assessment(activities=(lesson,), trips=(late,)))
        assert [f.rule_id for f in rules(result, "TIME-007")] == ["TIME-007"]

    def test_explicit_order_missing_deadline_is_order_012(self):
        lesson = activity("lesson", ("jordan",), "studio", at(16), at(17))
        t = ride(
            "two",
            at(15, 10),
            at(16),
            pickup="Home",
            family="Sam, Jordan",
            destinations=(("Sam", "XC Practice"), ("Jordan", "Music Studio")),
            stop_order="Sam, Jordan",
        )
        result = run(assessment(activities=(lesson,), trips=(t,)), routes(20))
        assert [f.rule_id for f in rules(result, "ORDER-012")] == ["ORDER-012"]

    def test_unknown_travel_time_with_deadline_is_route_010(self):
        result = run(
            assessment(activities=(LESSON,), trips=(TO_LESSON, FROM_LESSON)),
            StaticRouteProvider({}),
        )
        assert rules(result, "ROUTE-010")
        assert result.run_status is RunStatus.INCOMPLETE

    def test_capacity_checked_when_relevant(self):
        five = ride(
            "five",
            at(17),
            at(17, 30),
            family="Sam, Jordan, Alex",
            external="Passenger E",
            destinations=(
                ("Sam", "HOME"),
                ("Jordan", "HOME"),
                ("Alex", "HOME"),
                ("Passenger E", "DEFAULT"),
            ),
        )
        flagged = rules(run(assessment(trips=(five,))), "CAP-014")
        assert [f.severity for f in flagged] == [Severity.INCOMPLETE]

    def test_small_ride_with_unknown_capacity_is_not_flagged(self):
        assert rules(run(assessment(trips=(FROM_LESSON,))), "CAP-014") == []

    def test_tentative_ride_is_conf_008(self):
        t = ride("tentative", at(17), at(17, 30), status="tentative")
        assert [f.rule_id for f in rules(run(assessment(trips=(t,))), "CONF-008")] == [
            "CONF-008"
        ]

    def test_ended_rides_are_skipped(self):
        t = ride("past", at(6), at(6, 30), status="tentative")
        assert rules(run(assessment(trips=(t,))), "CONF-008") == []


def test_settings_change_the_merge_gap():
    from family_logistics.domain.context import EngineSettings

    lessons = (
        activity("l1", ("sam",), "studio", at(13), at(14)),
        activity("l2", ("sam",), "studio", at(16), at(17)),
    )
    wide = EngineSettings(same_location_gap=timedelta(hours=3))
    result = assess(assessment(activities=lessons), routes(), now=NOW, settings=wide)
    assert len(result.timelines[0].stays) == 1
