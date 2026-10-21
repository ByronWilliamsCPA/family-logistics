"""Passenger-level change-impact analysis."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fl_factories import FRIDAY_END, FRIDAY_START, key, notes, trip

from family_logistics.domain.change_impact import analyze_change_impact
from family_logistics.domain.models import Severity

SHARED = notes(
    family="Sam",
    external="Passenger E",
    destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
)


@pytest.mark.unit
class TestChangeImpact:
    def test_no_change_no_findings(self):
        legs = trip(SHARED).legs()
        impact = analyze_change_impact(legs, legs)
        assert impact.findings == ()

    def test_removed_family_passenger_is_not_pass_003(self):
        before = trip(SHARED).legs()
        after = trip(
            notes(
                family="",
                external="Passenger E",
                destinations=(("Passenger E", "DEFAULT"),),
            )
        ).legs()
        impact = analyze_change_impact(before, after)
        assert impact.findings == ()
        assert [leg.passenger_id for leg in impact.removed_family_legs] == ["sam"]

    def test_tentative_external_loss_is_warning(self):
        before = trip(
            notes(
                status="tentative",
                external="Passenger E",
                destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
            )
        ).legs()
        impact = analyze_change_impact(before, ())
        assert impact.findings[0].rule_id == "PASS-003"
        assert impact.findings[0].severity is Severity.WARNING

    def test_message_has_no_destination_text(self):
        before = trip(
            notes(
                external="Passenger E",
                destinations=(
                    ("Sam", "HOME"),
                    ("Passenger E", "Gym @ Example Sports Center"),
                ),
            )
        ).legs()
        impact = analyze_change_impact(before, ())
        assert "Main St" not in impact.findings[0].message
        assert "Gym" not in impact.findings[0].message

    def test_replacement_in_another_event_clears_obligation(self):
        before = trip(SHARED).legs()
        replacement = trip(
            notes(
                driver="Driver S",
                family="",
                external="Passenger E",
                destinations=(("Passenger E", "DEFAULT"),),
            ),
            uid="evt-2",
            start=FRIDAY_START + timedelta(minutes=30),
            end=FRIDAY_END + timedelta(minutes=30),
        ).legs()
        impact = analyze_change_impact(before, replacement)
        assert impact.findings == ()
        assert len(impact.replaced_legs) == 1

    def test_far_later_ride_is_not_a_replacement(self):
        before = trip(SHARED).legs()
        later = trip(
            notes(
                family="",
                external="Passenger E",
                destinations=(("Passenger E", "DEFAULT"),),
            ),
            uid="evt-3",
            start=FRIDAY_START + timedelta(hours=5),
            end=FRIDAY_END + timedelta(hours=5),
        ).legs()
        impact = analyze_change_impact(before, later)
        assert [f.rule_id for f in impact.findings] == ["PASS-003"]

    def test_unparsed_trip_is_carried_forward_not_lost(self):
        before = trip(SHARED).legs()
        impact = analyze_change_impact(
            before, (), unparsed_trip_keys=frozenset({key()})
        )
        assert impact.findings == ()
        assert len(impact.carried_forward) == 2

    def test_material_change_on_confirmed_leg_is_conf_008_once(self):
        before = trip(SHARED).legs()
        after = trip(
            notes(
                driver="Morgan",
                external="Passenger E",
                destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
            )
        ).legs()
        impact = analyze_change_impact(before, after)
        assert [f.rule_id for f in impact.findings] == ["CONF-008"]

    def test_tentative_change_is_not_conf_008_here(self):
        before = trip(notes(status="tentative")).legs()
        after = trip(notes(status="tentative", driver="Morgan")).legs()
        assert analyze_change_impact(before, after).findings == ()
