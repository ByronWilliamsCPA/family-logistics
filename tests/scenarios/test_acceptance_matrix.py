"""Acceptance scenario matrix (handoff section 16).

Scenarios the current domain modules and the M2 engine can prove run here.
The rest are explicit skips naming their milestone, so the matrix stays visible
and nothing is silently dropped. External children are pseudonymized ("Passenger E",
"Passenger R"); the "two children with the same first name" case uses
"Passenger J".
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fl_engine import NOW as ENGINE_NOW
from fl_engine import PEOPLE, activity, assessment, at, default, routes, school
from fl_factories import FRIDAY_END, FRIDAY_START, key, notes, parse, trip

from family_logistics.adapters.routing.base import RouteConstraints, StaticRouteProvider
from family_logistics.domain.change_impact import analyze_change_impact
from family_logistics.domain.confirmation import (
    RecordedConfirmation,
    confirmation_code,
    evaluate_confirmation,
)
from family_logistics.domain.engine import AssessmentHistory, assess
from family_logistics.domain.events import (
    SchoolDayKind,
    TransportDirection,
    TransportMode,
)
from family_logistics.domain.models import Person, PersonRole, RunStatus, Severity
from family_logistics.domain.rules import make_finding
from family_logistics.domain.run_status import SourceFreshness, compute_run_status
from family_logistics.domain.stop_order import OrderStatus, Stop

pytestmark = pytest.mark.unit
NOW = FRIDAY_START - timedelta(hours=6)


def test_shared_ride_projects_to_each_passenger():  # scenario 1
    """External Driver S collects Sam and Jordan Tuesday from the music studio."""
    t = trip(
        notes(
            driver="Driver S",
            family="Sam, Jordan",
            destinations=(("Sam", "HOME"), ("Jordan", "HOME")),
        )
    )
    legs = t.legs()
    assert {leg.passenger_id for leg in legs} == {"sam", "jordan"}
    assert {leg.trip_key for leg in legs} == {t.key}


def test_one_passenger_absent_from_one_occurrence():
    """Jordan absent one Tuesday: only Jordan removed; Sam remains."""
    both = trip(
        notes(
            family="Sam, Jordan",
            destinations=(("Sam", "HOME"), ("Jordan", "HOME")),
        ),
        recurrence_id="20261020T223000Z",
    ).legs()
    sam_only = trip(notes(), recurrence_id="20261020T223000Z").legs()
    impact = analyze_change_impact(both, sam_only)
    assert [leg.passenger_id for leg in impact.removed_family_legs] == ["jordan"]
    assert impact.findings == ()


def test_typo_is_critical_with_suggestion():
    """'Jordn' typed in family list."""
    result = parse(notes(family="Jordn", destinations=(("Jordn", "HOME"),)))
    finding = next(f for f in result.findings if f.rule_id == "NAME-002")
    assert finding.severity is Severity.CRITICAL
    assert finding.suggestion == "Did you mean Jordan?"
    assert result.trip is None


def test_sam_and_passenger_r_outbound_only_sam_returns():
    """Outbound two passengers; separate return only for Sam."""
    outbound = trip(
        notes(
            trip_type="transfer",
            family="Sam",
            external="Passenger R",
            pickup="XC Practice",
            destinations=(("Sam", "Music Studio"), ("Passenger R", "Music Studio")),
        ),
        uid="outbound",
    )
    home = trip(
        notes(pickup="Music Studio"),
        uid="return",
        start=FRIDAY_END,
        end=FRIDAY_END + timedelta(minutes=30),
    )
    returning = {leg.passenger_id for leg in home.legs()}
    assert returning == {"sam"}
    assert "ext-r" in {leg.passenger_id for leg in outbound.legs()}


def test_two_registered_children_with_same_first_name():
    """Ambiguous lookup prompts explicit selection; no unsafe guess."""
    result = parse(
        notes(
            family="",
            external="Passenger J",
            destinations=(("Passenger J", "DEFAULT"),),
        )
    )
    assert result.trip is None


def test_driver_changed_on_confirmed_trip_requires_reconfirmation():
    original = trip(notes())
    original = dataclasses.replace(original, confirm_code=confirmation_code(original))
    changed = dataclasses.replace(
        trip(notes(driver="Morgan")), confirm_code=original.confirm_code
    )
    state = evaluate_confirmation(changed, now=NOW)
    assert state.recorded is RecordedConfirmation.STALE_CODE
    impact = analyze_change_impact(original.legs(), changed.legs())
    assert [f.rule_id for f in impact.findings] == ["CONF-008"]


def test_icloud_unavailable_is_incomplete_not_green():
    icloud = SourceFreshness(
        "icloud", None, timedelta(hours=2), last_attempt_failed=True
    )
    assert compute_run_status([], [icloud], now=NOW) is RunStatus.INCOMPLETE


def test_route_service_unavailable_is_incomplete():
    provider = StaticRouteProvider({})
    result = provider.optimize(
        "P", [Stop("x", "X")], None, RouteConstraints(departure_time=FRIDAY_START)
    )
    assert result.status is OrderStatus.INCOMPLETE
    route_gap = make_finding("ROUTE-010", "Routing unavailable.", ("k",))
    fresh = SourceFreshness("icloud", NOW, timedelta(hours=2))
    assert compute_run_status([route_gap], [fresh], now=NOW) is RunStatus.INCOMPLETE


def test_deadline_beats_shorter_route():
    """Trip arrival violates deadline despite shorter route: reject it."""
    t0 = datetime(2026, 10, 23, 22, 30, tzinfo=UTC)
    m = timedelta(minutes=1)
    provider = StaticRouteProvider(
        {("P", "A"): 5 * m, ("P", "B"): 20 * m, ("A", "B"): 18 * m, ("B", "A"): 18 * m}
    )
    result = provider.optimize(
        "P",
        [Stop("a", "A"), Stop("b", "B", deadline=t0 + 21 * m)],
        None,
        RouteConstraints(departure_time=t0, dwell=0 * m),
    )
    assert result.order == ("b", "a")


def test_external_passenger_removed_unintentionally():
    before = trip(
        notes(
            external="Passenger E",
            destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
        )
    ).legs()
    after = trip(notes()).legs()
    impact = analyze_change_impact(before, after)
    assert impact.findings[0].rule_id == "PASS-003"
    assert impact.findings[0].severity is Severity.CRITICAL


def test_event_moved_to_new_time_single_occurrence_no_duplicate():
    """Moved occurrence keeps its RECURRENCE-ID; one leg, no duplicate ride."""
    rid = "20261023T223000Z"
    before = trip(notes(), recurrence_id=rid).legs()
    moved = trip(
        notes(),
        recurrence_id=rid,
        start=FRIDAY_START + timedelta(minutes=30),
        end=FRIDAY_END + timedelta(minutes=30),
    ).legs()
    assert [leg.leg_key for leg in moved] == [(key(recurrence_id=rid), "sam")]
    assert analyze_change_impact(before, moved).removed_family_legs == ()


M4 = "M4: needs HA endpoints"


@pytest.mark.skip(
    reason="M5: LLM unavailable -> deterministic report without narrative"
)
def test_llm_unavailable(): ...


@pytest.mark.skip(reason=f"{M4}: double on-demand press coalesced or idempotent")
def test_on_demand_idempotent(): ...


# --- M2 engine scenarios (handoff matrix 4-10, 12 and the Friday rule) -----


def engine_rules(inp, *, provider=None, history=None):
    result = assess(inp, provider or routes(10), now=ENGINE_NOW, history=history)
    return result, sorted(t.finding.rule_id for t in result.findings)


def confirmed(t):
    return dataclasses.replace(t, confirm_code=confirmation_code(t))


def test_consecutive_same_location_is_one_stay():  # scenario 4
    lessons = (
        activity("l1", ("sam",), "studio", at(16), at(17)),
        activity("l2", ("sam",), "studio", at(17), at(18)),
    )
    home = confirmed(trip(notes(), uid="home", start=at(18), end=at(18, 30)))
    there = confirmed(
        trip(
            notes(pickup="Home", destinations=(("Sam", "Music Studio"),)),
            uid="there",
            start=at(15, 30),
            end=at(16),
        )
    )
    result, found = engine_rules(assessment(activities=lessons, trips=(there, home)))
    assert len(result.timelines[0].stays) == 1
    assert found == []


def test_school_sports_extends_stay():  # scenario 5
    inp = assessment(
        school_days=(school("alex", at(8), at(15)),),
        activities=(activity("xc", ("alex",), "high-school", at(15), at(17, 30)),),
        defaults=(default("alex", TransportDirection.TO_SCHOOL, TransportMode.BIKE),),
    )
    at_bell = confirmed(
        trip(
            notes(
                family="Alex", pickup="High School", destinations=(("Alex", "HOME"),)
            ),
            uid="bell",
            start=at(15),
            end=at(15, 20),
        )
    )
    _, found = engine_rules(dataclasses.replace(inp, trips=(at_bell,)))
    assert found == ["RIDE-005"]
    at_end = dataclasses.replace(
        at_bell, start=at(17, 30), end=at(17, 50), confirm_code=None
    )
    _, found = engine_rules(dataclasses.replace(inp, trips=(confirmed(at_end),)))
    assert found == []


def test_school_specific_bell_overlay():  # scenario 6
    release = school("alex", at(8), at(12, 30), SchoolDayKind.EARLY_RELEASE)
    pickup = confirmed(
        trip(
            notes(
                family="Alex", pickup="High School", destinations=(("Alex", "HOME"),)
            ),
            uid="early",
            start=at(15),
            end=at(15, 20),
        )
    )
    inp = assessment(
        school_days=(release,),
        trips=(pickup,),
        defaults=(default("alex", TransportDirection.TO_SCHOOL, TransportMode.BIKE),),
    )
    result, found = engine_rules(inp)
    assert found == ["RIDE-005"]
    assert "12:30" in result.findings[0].finding.message


def test_no_school_day():  # scenario 7
    lesson = activity("lesson", ("alex",), "studio", at(10), at(11))
    rides = (
        confirmed(
            trip(
                notes(
                    family="Alex",
                    pickup="Home",
                    destinations=(("Alex", "Music Studio"),),
                ),
                uid="to",
                start=at(9, 30),
                end=at(9, 50),
            )
        ),
        confirmed(
            trip(
                notes(family="Alex", destinations=(("Alex", "HOME"),)),
                uid="back",
                start=at(11),
                end=at(11, 20),
            )
        ),
    )
    inp = assessment(
        school_days=(school("alex", at(8), at(8), SchoolDayKind.NO_SCHOOL),),
        activities=(lesson,),
        trips=rides,
        defaults=(
            default("alex", TransportDirection.TO_SCHOOL, TransportMode.BUS),
            default("alex", TransportDirection.FROM_SCHOOL, TransportMode.BUS),
        ),
    )
    result, found = engine_rules(inp)
    assert found == []
    assert [s.key for s in result.timelines[0].stays] == ["studio"]


def test_bike_baseline_exception():  # scenario 8
    bike = (
        default("alex", TransportDirection.TO_SCHOOL, TransportMode.BIKE),
        default("alex", TransportDirection.FROM_SCHOOL, TransportMode.BIKE),
    )
    meet_day = assessment(
        school_days=(school("alex", at(8), at(15)),),
        trips=(
            confirmed(
                trip(
                    notes(
                        family="Alex",
                        driver="Morgan",
                        pickup="High School",
                        destinations=(("Alex", "HOME"),),
                    ),
                    uid="drive",
                    start=at(15),
                    end=at(15, 20),
                )
            ),
        ),
        defaults=bike,
    )
    _, found = engine_rules(meet_day)
    assert found == []
    assert all(d.mode is TransportMode.BIKE for d in meet_day.transport_defaults)
    _, plain = engine_rules(dataclasses.replace(meet_day, trips=()))
    assert plain == []


TEAM_BUS = Person("team-bus", "Team Bus", PersonRole.CONTACT, can_drive=True)


def _meet_day(*returns):
    outbound = confirmed(
        dataclasses.replace(
            trip(
                notes(
                    family="Alex",
                    pickup="High School",
                    destinations=(("Alex", "Away Meet Park"),),
                ),
                uid="team-bus",
                start=at(15),
                end=at(15, 30),
            ),
            driver_id="team-bus",
        )
    )
    return assessment(
        people=(*PEOPLE, TEAM_BUS),
        school_days=(school("alex", at(8), at(15)),),
        activities=(activity("meet", ("alex",), "away-meet", at(15, 40), at(18)),),
        trips=(outbound, *returns),
        defaults=(default("alex", TransportDirection.TO_SCHOOL, TransportMode.BIKE),),
    )


def test_away_meet_team_bus():  # scenario 9
    _, without_return = engine_rules(_meet_day())
    assert without_return == ["RIDE-005"]
    tentative_return = trip(
        notes(
            family="Alex",
            driver="Morgan",
            status="tentative",
            pickup="Away Meet Park",
            destinations=(("Alex", "HOME"),),
        ),
        uid="return",
        start=at(18),
        end=at(18, 30),
    )
    _, with_return = engine_rules(_meet_day(tentative_return))
    assert with_return == ["CONF-008"]


def test_driver_unavailable_flags_every_passenger():  # scenario 10
    spectator = activity("watch", ("riley",), "away-meet", at(15), at(19))
    lesson = activity("lesson", ("sam",), "studio", at(16), at(17))
    shared = confirmed(
        trip(
            notes(
                external="Passenger E",
                destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
            ),
            uid="shared",
            start=at(17),
            end=at(17, 30),
        )
    )
    to_lesson = confirmed(
        trip(
            notes(
                driver="Morgan", pickup="Home", destinations=(("Sam", "Music Studio"),)
            ),
            uid="to",
            start=at(15, 30),
            end=at(15, 50),
        )
    )
    result, _ = engine_rules(
        assessment(activities=(spectator, lesson), trips=(to_lesson, shared))
    )
    flagged = {
        t.finding.entity_keys[1]
        for t in result.findings
        if t.finding.rule_id == "DRIVER-006"
    }
    assert flagged == {"passenger:sam", "passenger:ext-e"}


def test_destination_override_resolved_per_passenger():  # scenario 12
    def ride_to(place):
        return confirmed(
            trip(
                notes(
                    external="Passenger E",
                    destinations=(("Sam", "HOME"), ("Passenger E", place)),
                ),
                uid="game",
                start=at(17),
                end=at(17, 45),
            )
        )

    _, found = engine_rules(assessment(trips=(ride_to("Sibling Game"),)))
    assert "DEST-004" not in found
    _, unverified = engine_rules(assessment(trips=(ride_to("New Place"),)))
    assert unverified == ["DEST-004"]


def test_friday_rule_through_the_engine():  # scenario 11 (engine view)
    before = confirmed(
        trip(
            notes(
                family="Sam",
                external="Passenger E",
                pickup="High School",
                destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
            ),
            uid="friday",
            start=at(15),
            end=at(15, 30),
        )
    )
    e_only = confirmed(
        trip(
            notes(
                family="",
                external="Passenger E",
                pickup="High School",
                destinations=(("Passenger E", "DEFAULT"),),
            ),
            uid="friday",
            start=at(15),
            end=at(15, 30),
        )
    )
    teacher = confirmed(
        trip(
            notes(driver="Teacher A", pickup="High School"),
            uid="teacher",
            start=at(17),
            end=at(17, 30),
        )
    )
    inp = assessment(
        school_days=(school("sam", at(8), at(15)),),
        activities=(activity("volunteer", ("sam",), "high-school", at(15), at(17)),),
        trips=(e_only, teacher),
        defaults=(default("sam", TransportDirection.TO_SCHOOL, TransportMode.BIKE),),
    )
    history = AssessmentHistory(previous_trips=(before,))
    _, found = engine_rules(inp, history=history)
    assert "PASS-003" not in found
    assert "RIDE-005" not in found
    assert "ext-e" in {leg.passenger_id for leg in e_only.legs()}
