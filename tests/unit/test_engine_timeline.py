"""Timeline: stays, merging, school overlays and required moves."""

from __future__ import annotations

import pytest
from fl_engine import (
    LOCATIONS,
    MINUTE,
    PEOPLE,
    THURSDAY,
    ZONE,
    activity,
    assessment,
    at,
    school,
)

from family_logistics.domain.events import SchoolDayKind
from family_logistics.domain.locations import LocationResolver
from family_logistics.domain.timeline import build_timelines

pytestmark = pytest.mark.unit
GAP = 60 * MINUTE


def timelines(inp):
    resolver = LocationResolver(LOCATIONS, PEOPLE, inp.external_passengers)
    return build_timelines(inp, resolver, GAP, ZONE)


def only(inp, person_id="sam"):
    found = [t for t in timelines(inp) if t.person_id == person_id]
    assert len(found) == 1
    return found[0]


class TestMerging:
    def test_back_to_back_lessons_form_one_stay(self):
        inp = assessment(
            activities=(
                activity("l1", ("sam",), "studio", at(16), at(17)),
                activity("l2", ("sam",), "studio", at(17, 15), at(18)),
            )
        )
        day = only(inp)
        assert len(day.stays) == 1
        assert day.stays[0].end == at(18)
        assert day.stays[0].source_ids == ("l1", "l2")
        assert [(t.from_key, t.to_key) for t in day.transitions] == [
            ("home:sam", "studio"),
            ("studio", "home:sam"),
        ]

    def test_gap_beyond_merge_gap_keeps_two_stays(self):
        inp = assessment(
            activities=(
                activity("l1", ("sam",), "studio", at(13), at(14)),
                activity("l2", ("sam",), "studio", at(16), at(17)),
            )
        )
        day = only(inp)
        assert len(day.stays) == 2
        assert len(day.transitions) == 2

    def test_unregistered_text_places_merge_only_with_same_text(self):
        inp = assessment(
            activities=(
                activity("a", ("sam",), None, at(13), at(14), text="Park A"),
                activity("b", ("sam",), None, at(14), at(15), text="Park B"),
            )
        )
        day = only(inp)
        assert len(day.stays) == 2
        assert not day.stays[0].resolved


class TestSchool:
    def test_sport_at_school_extends_the_stay_past_the_bell(self):
        inp = assessment(
            school_days=(school("alex", at(8), at(15)),),
            activities=(activity("xc", ("alex",), "high-school", at(15), at(17, 30)),),
        )
        day = only(inp, "alex")
        assert len(day.stays) == 1
        home = [t for t in day.transitions if t.to_key == "home:alex"]
        assert home[0].depart_after == at(17, 30)

    def test_no_school_day_contributes_no_block(self):
        inp = assessment(
            school_days=(school("alex", at(8), at(8), SchoolDayKind.NO_SCHOOL),),
        )
        assert [t for t in timelines(inp) if t.person_id == "alex"] == []


class TestMoves:
    def test_moves_between_places(self):
        inp = assessment(
            activities=(
                activity("p", ("sam",), "xc", at(15), at(16)),
                activity("l", ("sam",), "studio", at(16, 30), at(17, 30)),
            )
        )
        moves = only(inp).transitions
        assert [(t.from_key, t.to_key) for t in moves] == [
            ("home:sam", "xc"),
            ("xc", "studio"),
            ("studio", "home:sam"),
        ]
        assert moves[1].depart_after == at(16)
        assert moves[1].arrive_by == at(16, 30)
        assert moves[0].depart_after is None
        assert moves[-1].arrive_by is None

    def test_overlapping_places_are_a_conflict(self):
        inp = assessment(
            activities=(
                activity("p", ("sam",), "xc", at(15), at(17)),
                activity("l", ("sam",), "studio", at(16), at(17, 30)),
            )
        )
        assert only(inp).transitions[1].is_conflict

    def test_overnight_stay_needs_no_return_or_morning_trip(self):
        inp = assessment(
            activities=(
                activity(
                    "s1", ("sam",), "game", at(19, day=THURSDAY), at(23, day=THURSDAY)
                ),
                activity("s2", ("sam",), "game", at(8), at(10)),
            )
        )
        days = [t for t in timelines(inp) if t.person_id == "sam"]
        moves = [m for d in days for m in d.transitions]
        assert [(m.from_key, m.to_key) for m in moves] == [
            ("home:sam", "game"),
            ("game", "home:sam"),
        ]
        assert moves[0].day == THURSDAY

    def test_activity_at_home_needs_no_move(self):
        inp = assessment(activities=(activity("h", ("sam",), "home", at(16), at(17)),))
        assert only(inp).transitions == ()


class TestMonitoring:
    def test_disabled_child_has_no_timeline(self):
        inp = assessment(
            activities=(activity("c", ("casey",), "studio", at(16), at(17)),)
        )
        assert [t for t in timelines(inp) if t.person_id == "casey"] == []

    def test_parents_are_not_timelined(self):
        inp = assessment(
            activities=(activity("w", ("morgan",), "studio", at(9), at(17)),)
        )
        assert timelines(inp) == ()
