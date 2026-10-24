"""Property-based tests (Hypothesis) for the notes parser and confirmation codes."""

from __future__ import annotations

import pytest
from fl_factories import notes, parse, trip
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from family_logistics.domain.confirmation import confirmation_code
from family_logistics.domain.models import Severity
from family_logistics.domain.parser import read_notes

LABELS = st.sampled_from(
    [
        "FL_VERSION",
        "TRIP_TYPE",
        "STATUS",
        "DRIVER",
        "FAMILY_PASSENGERS",
        "EXTERNAL_PASSENGERS",
        "PICKUP",
        "DESTINATIONS",
        "STOP_ORDER",
        "CONFIRM",
        "NOTES",
        "Sam",
        "Passenger E",
    ]
)
LINES = st.one_of(
    st.text(max_size=80),
    st.builds(lambda label, value: f"{label}: {value}", LABELS, st.text(max_size=40)),
)
NOTE_BLOCKS = st.lists(LINES, max_size=20).map("\n".join)
# Pinned from the current implementation: a change to the canonical form is a
# change to every code already written into calendars and must be deliberate.
GOLDEN_DEFAULT_CODE = "5E81DB"
FAST = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])


@pytest.mark.unit
class TestParserProperties:
    @FAST
    @given(NOTE_BLOCKS)
    def test_parser_never_raises_and_a_trip_exists_exactly_when_unblocked(self, text):
        result = parse(text)
        blocked = any(f.severity is Severity.CRITICAL for f in result.findings)
        assert (result.trip is None) is blocked

    @FAST
    @given(st.text(max_size=400))
    def test_read_notes_never_raises(self, text):
        raw = read_notes(text)
        assert all(isinstance(problem, str) for problem in raw.problems)

    @FAST
    @given(
        st.sampled_from(["\n", "\r\n"]), st.sampled_from(["pickup", "PICKUP", "Pickup"])
    )
    def test_line_endings_and_case_do_not_change_the_trip(self, newline, trip_type):
        baseline = trip(notes())
        variant = trip(notes(newline=newline, trip_type=trip_type))
        assert variant.trip_type is baseline.trip_type
        assert variant.passengers == baseline.passengers


@pytest.mark.unit
class TestConfirmationProperties:
    @FAST
    @given(st.text(max_size=60).filter(lambda s: "\n" not in s and "\r" not in s))
    def test_code_is_deterministic_and_ignores_free_text(self, free):
        base = confirmation_code(trip(notes()))
        assert confirmation_code(trip(notes())) == base
        assert confirmation_code(trip(notes(free=free))) == base

    @FAST
    @given(
        st.sampled_from(["Riley", "Morgan"]),
        st.sampled_from(["Music Studio", "School"]),
    )
    def test_code_shape(self, driver, pickup):
        code = confirmation_code(trip(notes(driver=driver, pickup=pickup)))
        assert len(code) == 6
        assert code == code.upper()
        int(code, 16)

    def test_code_changes_when_trip_content_changes(self):
        codes = {
            confirmation_code(trip(notes())),
            confirmation_code(trip(notes(driver="Morgan"))),
            confirmation_code(trip(notes(pickup="School"))),
            confirmation_code(trip(notes(destinations=(("Sam", "School"),)))),
        }
        assert len(codes) == 4

    def test_trip_type_is_not_material_to_the_code(self):
        assert confirmation_code(trip(notes(trip_type="dropoff"))) == (
            confirmation_code(trip(notes()))
        )

    def test_golden_code_for_the_default_trip(self):
        assert confirmation_code(trip(notes())) == GOLDEN_DEFAULT_CODE
