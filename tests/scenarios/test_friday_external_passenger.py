"""PERMANENT REGRESSION TEST: the Friday external-passenger scenario.

Planning requirement: a change to Sam's transportation must never silently
eliminate an external child's existing ride. This is the handoff's Friday
scenario (section 19, kickoff step 5); the external child is pseudonymized as
"Passenger E" (``ext-e``) because external children's real names never go in
Git.

Story:

1. Friday: Riley collects Sam and Passenger E from music lesson. Confirmed.
2. Sam now stays at school to volunteer, and a teacher drives them home.
   Riley edits the event to drop Sam; Passenger E's leg must remain an
   active obligation with no PASS-003.
3. If the only event carrying Passenger E is deleted (or Passenger E is removed
   from it) with no replacement, the result is PASS-003 Critical.

Do not delete or weaken this test. Change it only with owner approval.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fl_factories import FRIDAY_END, FRIDAY_START, notes, trip

from family_logistics.domain.change_impact import analyze_change_impact
from family_logistics.domain.confirmation import confirmation_code
from family_logistics.domain.models import PassengerKind, Severity

pytestmark = pytest.mark.unit


def friday_shared_ride():
    draft = trip(
        notes(
            driver="Riley",
            family="Sam",
            external="Passenger E",
            pickup="Music Studio",
            destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
        ),
        uid="friday-lesson",
    )
    return dataclasses.replace(draft, confirm_code=confirmation_code(draft))


def sam_removed_from_ride():
    return trip(
        notes(
            driver="Riley",
            family="",
            external="Passenger E",
            pickup="Music Studio",
            destinations=(("Passenger E", "DEFAULT"),),
        ),
        uid="friday-lesson",
    )


def teacher_drives_sam():
    return trip(
        notes(
            driver="Teacher A",
            family="Sam",
            pickup="School",
            destinations=(("Sam", "HOME"),),
        ),
        uid="friday-volunteer",
        start=FRIDAY_START + timedelta(hours=1),
        end=FRIDAY_END + timedelta(hours=1),
    )


def test_sam_change_keeps_passenger_e_obligation():
    before = friday_shared_ride().legs()
    after = (*sam_removed_from_ride().legs(), *teacher_drives_sam().legs())

    impact = analyze_change_impact(before, after)

    assert not [f for f in impact.findings if f.rule_id == "PASS-003"]
    remaining = [
        leg
        for leg in after
        if leg.passenger_id == "ext-e" and leg.passenger_kind is PassengerKind.EXTERNAL
    ]
    assert len(remaining) == 1
    assert remaining[0].trip_key.uid == "friday-lesson"
    assert remaining[0].driver_id == "riley"


def test_sam_change_on_confirmed_ride_flags_reconfirmation_only():
    before = friday_shared_ride().legs()
    after = (*sam_removed_from_ride().legs(), *teacher_drives_sam().legs())

    impact = analyze_change_impact(before, after)

    # Passenger E's own leg is unchanged, so only the removed family leg is
    # reported for continuity checking; no external obligation is lost.
    assert [leg.passenger_id for leg in impact.removed_family_legs] == ["sam"]
    assert {f.rule_id for f in impact.findings} <= {"CONF-008"}


def test_deleting_only_event_carrying_passenger_e_is_critical():
    before = friday_shared_ride().legs()
    after = teacher_drives_sam().legs()

    impact = analyze_change_impact(before, after)

    lost = [f for f in impact.findings if f.rule_id == "PASS-003"]
    assert len(lost) == 1
    assert lost[0].severity is Severity.CRITICAL
    assert "passenger:ext-e" in lost[0].entity_keys


def test_removing_passenger_e_from_the_event_is_critical():
    before = friday_shared_ride().legs()
    only_sam = trip(
        notes(
            driver="Riley",
            family="Sam",
            destinations=(("Sam", "HOME"),),
        ),
        uid="friday-lesson",
    ).legs()

    impact = analyze_change_impact(before, only_sam)

    assert [f.rule_id for f in impact.findings if f.severity is Severity.CRITICAL] == [
        "PASS-003"
    ]


def test_pass_003_issue_id_is_stable_for_deduplication():
    before = friday_shared_ride().legs()
    first = analyze_change_impact(before, ()).findings
    second = analyze_change_impact(before, ()).findings
    assert [f.issue_id for f in first] == [f.issue_id for f in second]
