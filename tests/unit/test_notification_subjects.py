"""Notification subjects: findings resolve to a time, people and a driver (ADR 0008 D7).

The resolver is pure. It reads a finding's entity keys, a ``RideIndex`` and the
day timelines, never a message, a suggestion or a raw name.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest
from fl_engine import at
from fl_factories import FRIDAY_END, FRIDAY_START, key, notes, parse, trip

from family_logistics.domain.models import (
    PassengerKind,
    Severity,
    SourceEventKey,
    TripPassenger,
)
from family_logistics.domain.notification_subjects import (
    Resolution,
    RideIndex,
    Subject,
    is_imminent,
    resolve_subject,
    run_imminent,
    transition_window,
)
from family_logistics.domain.rules import RULES, make_finding
from family_logistics.domain.timeline import DayTimeline, Transition

pytestmark = pytest.mark.unit

TOLERANCE = timedelta(minutes=30)
WINDOW = timedelta(minutes=180)
FAMILY = PassengerKind.FAMILY
EXTERNAL = PassengerKind.EXTERNAL
DAY = date(2026, 10, 23)

SHARED = trip(
    notes(
        driver="Driver S",
        family="Sam",
        external="Passenger E",
        destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
    ),
    uid="shared",
)
SHARED_KEY = SHARED.key.as_str()


def index(
    *,
    current=(SHARED,),
    previous=(),
    unparsed=(),
) -> RideIndex:
    return RideIndex.build(current=current, previous=previous, unparsed=unparsed)


def resolve(finding, rides=None, timelines=()):
    rides = rides if rides is not None else index()
    return resolve_subject(
        finding, rides=rides, timelines=timelines, pickup_tolerance=TOLERANCE
    )


def transition(
    person="sam",
    *,
    depart=at(15, 0),
    arrive=at(15, 30),
    from_key="loc:studio",
    to_key="home",
) -> Transition:
    return Transition(person, DAY, from_key, to_key, depart, arrive)


def transition_keys(tr: Transition) -> tuple[str, ...]:
    return (
        f"person:{tr.person_id}",
        f"day:{tr.day.isoformat()}",
        f"from:{tr.from_key}",
        f"to:{tr.to_key}",
    )


def timeline_of(*transitions: Transition) -> tuple[DayTimeline, ...]:
    return (DayTimeline("sam", DAY, (), tuple(transitions)),)


class TestRideIndex:
    def test_build_keys_current_previous_and_unparsed(self):
        other = trip(notes(), uid="other")
        unparsed_key = key("broken")
        rides = RideIndex.build(
            current=(SHARED,),
            previous=(other,),
            unparsed=((unparsed_key, FRIDAY_START, FRIDAY_END),),
        )
        assert set(rides.current) == {SHARED_KEY}
        assert set(rides.previous) == {other.key.as_str()}
        window = rides.unparsed[unparsed_key.as_str()]
        assert (window.start, window.end) == (FRIDAY_START, FRIDAY_END)
        assert window.passengers == ()
        assert window.driver_id is None

    def test_ride_window_carries_driver_and_passenger_kinds(self):
        window = index().current[SHARED_KEY]
        assert window.driver_id == "ext-driver-s"
        assert set(window.passengers) == {("sam", FAMILY), ("ext-e", EXTERNAL)}

    def test_windows_uses_previous_only_when_nothing_else_is_known(self):
        old = trip(notes(), uid="old")
        previous_only = RideIndex.build(current=(), previous=(old,), unparsed=())
        assert [w.start for w in previous_only.windows()] == [old.start]
        both = RideIndex.build(current=(SHARED,), previous=(old,), unparsed=())
        assert [w.driver_id for w in both.windows()] == ["ext-driver-s"]
        only_unparsed = RideIndex.build(
            current=(),
            previous=(old,),
            unparsed=((key("x"), FRIDAY_START, FRIDAY_END),),
        )
        assert len(only_unparsed.windows()) == 1
        assert only_unparsed.windows()[0].driver_id is None


class TestImminence:
    def test_is_imminent_bounds(self):
        due, end = at(15), at(15, 45)
        assert is_imminent(due, end, due - WINDOW, WINDOW)
        assert is_imminent(due, end, due, WINDOW)
        assert is_imminent(due, end, due + timedelta(minutes=20), WINDOW)
        assert is_imminent(due, end, end, WINDOW)
        assert not is_imminent(due, end, due - WINDOW - timedelta(seconds=1), WINDOW)
        assert not is_imminent(due, end, end + timedelta(seconds=1), WINDOW)

    def test_zero_window_is_imminent_only_in_progress(self):
        due, end = at(15), at(15, 45)
        zero = timedelta(0)
        assert not is_imminent(due, end, due - timedelta(seconds=1), zero)
        assert is_imminent(due, end, due, zero)
        assert is_imminent(due, end, end, zero)
        assert not is_imminent(due, end, end + timedelta(seconds=1), zero)

    def test_run_imminent_without_a_ride_set_is_true(self):
        assert run_imminent(
            None, (), now=at(3), window=WINDOW, pickup_tolerance=TOLERANCE
        )

    def test_run_imminent_considers_rides(self):
        rides = index()
        near = SHARED.start - timedelta(hours=2)
        far = SHARED.start - timedelta(hours=5)
        assert run_imminent(
            rides, (), now=near, window=WINDOW, pickup_tolerance=TOLERANCE
        )
        assert not run_imminent(
            rides, (), now=far, window=WINDOW, pickup_tolerance=TOLERANCE
        )

    def test_run_imminent_with_no_rides_and_no_moves_is_false(self):
        empty = RideIndex.build(current=(), previous=(), unparsed=())
        assert not run_imminent(
            empty, (), now=at(12), window=WINDOW, pickup_tolerance=TOLERANCE
        )

    def test_run_imminent_includes_transitions(self):
        empty = RideIndex.build(current=(), previous=(), unparsed=())
        move = transition(depart=at(15), arrive=at(15, 40))
        assert run_imminent(
            empty,
            timeline_of(move),
            now=at(13),
            window=WINDOW,
            pickup_tolerance=TOLERANCE,
        )
        assert not run_imminent(
            empty,
            timeline_of(move),
            now=at(11),
            window=WINDOW,
            pickup_tolerance=TOLERANCE,
        )


class TestTransitionWindow:
    def test_arrive_by_ends_the_window(self):
        tr = transition(depart=at(15), arrive=at(15, 40))
        assert transition_window(tr, TOLERANCE) == (at(15), at(15, 40))

    def test_depart_after_plus_tolerance_when_returning_home(self):
        tr = Transition("sam", DAY, "loc:studio", "home", at(15), None)
        assert transition_window(tr, TOLERANCE) == (at(15), at(15, 30))

    def test_leaving_home_uses_the_arrival_time(self):
        tr = Transition("sam", DAY, "home", "loc:school", None, at(8))
        assert transition_window(tr, TOLERANCE) == (at(8), at(8))

    def test_a_scheduling_conflict_never_ends_before_it_starts(self):
        tr = transition(depart=at(15), arrive=at(14, 30))
        due, end = transition_window(tr, TOLERANCE)
        assert due == at(15)
        assert end >= due


class TestEachRuleKeyShapeResolves:
    """T-subj-2: one case per row of the D7 table."""

    def test_parse_001_on_an_unparsed_event(self):
        broken = key("broken")
        rides = index(unparsed=((broken, at(16), at(16, 30)),))
        found = make_finding(
            "PARSE-001", "Bad notes.", (broken.as_str(), "parse:DRIVER")
        )
        resolution, subject = resolve(found, rides)
        assert resolution is Resolution.RESOLVED
        assert (subject.due_at, subject.due_end) == (at(16), at(16, 30))
        assert subject.passengers == ()
        assert subject.driver_id is None

    def test_parse_001_duplicate_detail_key(self):
        broken = key("broken")
        rides = index(unparsed=((broken, at(16), at(16, 30)),))
        found = make_finding(
            "PARSE-001", "Dup.", (broken.as_str(), "parse:duplicate:sam")
        )
        assert resolve(found, rides)[0] is Resolution.RESOLVED

    def test_name_002_and_ext_013_read_no_raw_names(self):
        broken = key("broken")
        rides = index(unparsed=((broken, at(16), at(16, 30)),))
        for rule, detail in (
            ("NAME-002", "family-name:jordn"),
            ("NAME-002", "external-name:sam"),
            ("EXT-013", "external-unknown:passenger q"),
            ("PARSE-001", "external-ambiguous:passenger j"),
        ):
            resolution, subject = resolve(
                make_finding(rule, "m", (broken.as_str(), detail)), rides
            )
            assert resolution is Resolution.RESOLVED, detail
            assert subject.passengers == ()

    def test_parse_findings_on_a_parsed_event_resolve_against_current(self):
        found = make_finding("EXT-013", "m", (SHARED_KEY, "external-unknown:x"))
        resolution, subject = resolve(found)
        assert resolution is Resolution.RESOLVED
        assert (subject.due_at, subject.due_end) == (SHARED.start, SHARED.end)
        assert subject.passengers == ()

    def test_pass_003_uses_the_previous_ride_passenger_and_driver(self):
        previous = trip(
            notes(
                driver="Driver S",
                family="Sam",
                external="Passenger E",
                destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
            ),
            uid="prev",
            start=at(17),
            end=at(17, 30),
        )
        rides = index(current=(), previous=(previous,))
        found = make_finding(
            "PASS-003",
            "m",
            (previous.key.as_str(), "passenger:ext-e"),
            severity=Severity.CRITICAL,
        )
        resolution, subject = resolve(found, rides)
        assert resolution is Resolution.RESOLVED
        assert (subject.due_at, subject.due_end) == (at(17), at(17, 30))
        assert subject.passengers == (("ext-e", EXTERNAL),)
        assert subject.driver_id == "ext-driver-s"

    def test_conf_008_from_confirmation_uses_the_current_ride(self):
        found = make_finding("CONF-008", "m", (SHARED_KEY,))
        resolution, subject = resolve(found)
        assert resolution is Resolution.RESOLVED
        assert (subject.due_at, subject.due_end) == (SHARED.start, SHARED.end)
        assert subject.passengers == ()
        assert subject.driver_id is None

    def test_conf_008_from_change_impact_prefers_current_else_previous(self):
        moved = dataclasses.replace(
            SHARED, start=SHARED.start + timedelta(hours=1), end=SHARED.end
        )
        previous = trip(notes(), uid="gone", start=at(9), end=at(9, 30))
        rides = index(current=(moved,), previous=(SHARED, previous))
        in_both = make_finding("CONF-008", "m", (SHARED_KEY,))
        assert resolve(in_both, rides)[1].due_at == moved.start
        only_previous = make_finding("CONF-008", "m", (previous.key.as_str(),))
        assert resolve(only_previous, rides)[1].due_at == at(9)

    def test_dest_004_names_that_passenger_only(self):
        found = make_finding(
            "DEST-004",
            "m",
            (SHARED_KEY, "passenger:ext-e", "destination"),
            severity=Severity.CRITICAL,
        )
        resolution, subject = resolve(found)
        assert resolution is Resolution.RESOLVED
        assert subject.passengers == (("ext-e", EXTERNAL),)
        assert subject.driver_id is None
        assert subject.due_at == SHARED.start

    @pytest.mark.parametrize("rule", ["TIME-007", "ORDER-012", "ROUTE-010"])
    def test_route_findings_name_every_passenger_and_the_driver(self, rule):
        found = make_finding(rule, "m", (SHARED_KEY, "route"))
        resolution, subject = resolve(found)
        assert resolution is Resolution.RESOLVED
        assert set(subject.passengers) == {("sam", FAMILY), ("ext-e", EXTERNAL)}
        assert subject.driver_id == "ext-driver-s"

    def test_cap_014_names_the_driver(self):
        found = make_finding(
            "CAP-014", "m", (SHARED_KEY, "capacity"), severity=Severity.INCOMPLETE
        )
        resolution, subject = resolve(found)
        assert resolution is Resolution.RESOLVED
        assert subject.driver_id == "ext-driver-s"
        assert subject.passengers == ()

    def test_driver_006_ride_names_passenger_and_driver_from_the_keys(self):
        found = make_finding(
            "DRIVER-006",
            "m",
            (SHARED_KEY, "passenger:sam", "driver:morgan"),
            severity=Severity.CRITICAL,
        )
        resolution, subject = resolve(found)
        assert resolution is Resolution.RESOLVED
        assert subject.passengers == (("sam", FAMILY),)
        assert subject.driver_id == "morgan"

    def test_route_010_reachability_names_the_driver(self):
        found = make_finding(
            "ROUTE-010", "m", (SHARED_KEY, "driver:morgan", "reachability")
        )
        resolution, subject = resolve(found)
        assert resolution is Resolution.RESOLVED
        assert subject.driver_id == "morgan"
        assert subject.passengers == ()

    @pytest.mark.parametrize(
        ("rule", "extra"),
        [
            ("RIDE-005", ()),
            ("MODE-015", ("bus",)),
            ("MODE-015", ("default-driver",)),
            ("ROUTE-010", ("unresolved",)),
        ],
    )
    def test_transition_rules_resolve_against_the_timeline(self, rule, extra):
        tr = transition()
        found = make_finding(rule, "m", (*transition_keys(tr), *extra))
        resolution, subject = resolve(found, timelines=timeline_of(tr))
        assert resolution is Resolution.RESOLVED
        assert (subject.due_at, subject.due_end) == (at(15), at(15, 30))
        assert subject.passengers == (("sam", FAMILY),)
        assert subject.driver_id is None

    def test_driver_006_default_names_person_and_driver(self):
        tr = transition()
        found = make_finding(
            "DRIVER-006",
            "m",
            (*transition_keys(tr), "driver:riley"),
            severity=Severity.CRITICAL,
        )
        resolution, subject = resolve(found, timelines=timeline_of(tr))
        assert resolution is Resolution.RESOLVED
        assert subject.passengers == (("sam", FAMILY),)
        assert subject.driver_id == "riley"

    def test_school_009_takes_its_day_from_the_key(self):
        found = make_finding(
            "SCHOOL-009", "m", ("person:alex", "day:2026-10-23", "school")
        )
        resolution, subject = resolve(found)
        assert resolution is Resolution.DAY
        assert subject.day == DAY
        assert subject.due_at is None
        assert subject.passengers == (("alex", FAMILY),)

    def test_school_009_with_a_bad_day_is_unresolved(self):
        found = make_finding("SCHOOL-009", "m", ("person:alex", "day:soon", "school"))
        assert resolve(found)[0] is Resolution.UNRESOLVED

    @pytest.mark.parametrize("name", ["icloud", "ha_snapshot", "none"])
    def test_data_011_is_run_wide(self, name):
        found = make_finding("DATA-011", "m", (f"source:{name}",))
        resolution, subject = resolve(found, rides=None)
        assert resolution is Resolution.RUN_WIDE
        assert subject == Subject()

    def test_every_rule_in_the_catalog_has_a_case_above(self):
        covered = {
            "PARSE-001", "NAME-002", "EXT-013", "PASS-003", "CONF-008", "DEST-004",
            "TIME-007", "ORDER-012", "ROUTE-010", "CAP-014", "DRIVER-006",
            "RIDE-005", "MODE-015", "SCHOOL-009", "DATA-011",
        }  # fmt: skip
        assert covered == set(RULES)


class TestResolutionEdges:
    def test_exact_trip_key_wins_over_prefix(self):
        """T-subj-3: a calendar id starting with ``person:`` is still a trip key."""
        odd = SourceEventKey("person:cal", "uid-1", None)
        odd_trip = dataclasses.replace(SHARED, key=odd)
        found = make_finding("TIME-007", "m", (odd.as_str(), "route"))
        resolution, subject = resolve(found, index(current=(odd_trip,)))
        assert resolution is Resolution.RESOLVED
        assert subject.due_at == odd_trip.start
        assert subject.driver_id == "ext-driver-s"

    def test_unregistered_ids_dropped_and_kinds_kept(self):
        """T-subj-4: no ``unregistered:`` id in any subject; kinds preserved."""
        extra = TripPassenger(
            "unregistered:q", EXTERNAL, "Q", SHARED.passengers[0].destination
        )
        with_extra = dataclasses.replace(SHARED, passengers=(*SHARED.passengers, extra))
        rides = index(current=(with_extra,))
        window = rides.current[SHARED_KEY]
        assert all(not pid.startswith("unregistered:") for pid, _ in window.passengers)
        assert set(window.passengers) == {("sam", FAMILY), ("ext-e", EXTERNAL)}
        found = make_finding(
            "DEST-004", "m", (SHARED_KEY, "passenger:unregistered:q", "destination")
        )
        resolution, subject = resolve(found, rides)
        assert resolution is Resolution.RESOLVED
        assert subject.passengers == ()
        driver = make_finding(
            "DRIVER-006",
            "m",
            (SHARED_KEY, "passenger:sam", "driver:unregistered:z"),
        )
        _, subject = resolve(driver, rides)
        assert subject.driver_id is None
        assert subject.passengers == (("sam", FAMILY),)

    def test_a_passenger_not_on_the_ride_is_dropped(self):
        found = make_finding(
            "DEST-004", "m", (SHARED_KEY, "passenger:ghost", "destination")
        )
        resolution, subject = resolve(found)
        assert resolution is Resolution.RESOLVED
        assert subject.passengers == ()

    def test_unresolved_when_the_key_is_unknown(self):
        """T-subj-6 (resolver half): a broken key yields UNRESOLVED."""
        found = make_finding("TIME-007", "m", ("cal|nope|-", "route"))
        assert resolve(found)[0] is Resolution.UNRESOLVED
        assert resolve(found)[1] == Subject()

    def test_unresolved_without_any_ride_set(self):
        found = make_finding("TIME-007", "m", (SHARED_KEY, "route"))
        resolution, _ = resolve_subject(
            found, rides=None, timelines=(), pickup_tolerance=TOLERANCE
        )
        assert resolution is Resolution.UNRESOLVED

    def test_unresolved_when_a_transition_is_not_in_the_timelines(self):
        tr = transition()
        found = make_finding("RIDE-005", "m", transition_keys(tr))
        assert resolve(found, timelines=())[0] is Resolution.UNRESOLVED

    def test_unresolved_with_no_entity_keys(self):
        assert resolve(make_finding("TIME-007", "m", ()))[0] is Resolution.UNRESOLVED

    def test_a_parsed_ride_falls_back_to_previous_when_current_is_empty(self):
        """A failed run carries history rides only."""
        rides = RideIndex.build(current=(), previous=(SHARED,), unparsed=())
        found = make_finding("TIME-007", "m", (SHARED_KEY, "route"))
        resolution, subject = resolve(found, rides)
        assert resolution is Resolution.RESOLVED
        assert subject.due_at == SHARED.start

    def test_parse_helper_findings_come_from_the_real_parser(self):
        """Real parser keys resolve against an unparsed event."""
        result = parse(notes(family="Jordn", destinations=(("Jordn", "HOME"),)))
        assert result.trip is None
        rides = index(current=(), unparsed=((key(), FRIDAY_START, FRIDAY_END),))
        assert result.findings
        for found in result.findings:
            resolution, subject = resolve(found, rides)
            assert resolution is Resolution.RESOLVED
            assert subject.due_at == FRIDAY_START
