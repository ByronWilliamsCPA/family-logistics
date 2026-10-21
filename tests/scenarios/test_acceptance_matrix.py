"""Acceptance scenario matrix (handoff section 16).

Scenarios the current domain modules can prove run here. The rest are
explicit skips naming their milestone, so the matrix stays visible and nothing
is silently dropped. External children are pseudonymized ("Passenger E",
"Passenger R"); the "two children with the same first name" case uses
"Passenger J".
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fl_factories import FRIDAY_END, FRIDAY_START, key, notes, parse, trip

from family_logistics.adapters.routing.base import RouteConstraints, StaticRouteProvider
from family_logistics.domain.change_impact import analyze_change_impact
from family_logistics.domain.confirmation import (
    RecordedConfirmation,
    confirmation_code,
    evaluate_confirmation,
)
from family_logistics.domain.models import RunStatus, Severity
from family_logistics.domain.rules import make_finding
from family_logistics.domain.run_status import SourceFreshness, compute_run_status
from family_logistics.domain.stop_order import OrderStatus, Stop

pytestmark = pytest.mark.unit
NOW = FRIDAY_START - timedelta(hours=6)


def test_shared_ride_projects_to_each_passenger():
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


M2 = "M2: needs location timeline, school overlays and rule engine"
M3 = "M3: needs routing adapter, notifications or scheduler"
M4 = "M4: needs HA endpoints"


@pytest.mark.skip(reason=f"{M2}: consecutive classes at one studio form one stay")
def test_consecutive_same_location_is_one_stay(): ...


@pytest.mark.skip(reason=f"{M2}: pickup at sports end, not bell end")
def test_school_sports_extends_stay(): ...


@pytest.mark.skip(reason=f"{M2}: early release / late start bell overlay from HA")
def test_school_specific_bell_overlay(): ...


@pytest.mark.skip(reason=f"{M2}: district no-school day suppresses school baseline")
def test_no_school_day(): ...


@pytest.mark.skip(reason=f"{M2}: one-day exception overrides bike baseline")
def test_bike_baseline_exception(): ...


@pytest.mark.skip(reason=f"{M2}: team bus outbound, parent return only if confirmed")
def test_away_meet_team_bus(): ...


@pytest.mark.skip(
    reason=f"{M2}: DRIVER-006 for all passengers of an unavailable driver"
)
def test_driver_unavailable_flags_every_passenger(): ...


@pytest.mark.skip(reason=f"{M3}: per-passenger destination override re-routed")
def test_destination_override_rerouted(): ...


@pytest.mark.skip(
    reason="M5: LLM unavailable -> deterministic report without narrative"
)
def test_llm_unavailable(): ...


@pytest.mark.skip(reason=f"{M4}: double on-demand press coalesced or idempotent")
def test_on_demand_idempotent(): ...
