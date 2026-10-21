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
FAST = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])


@pytest.mark.unit
class TestParserProperties:
    @FAST
    @given(NOTE_BLOCKS)
    def test_parser_never_raises_and_blocks_on_critical(self, text):
        result = parse(text)
        blocked = any(f.severity is Severity.CRITICAL for f in result.findings)
        if blocked:
            assert result.trip is None
        else:
            assert all(f.severity is not Severity.CRITICAL for f in result.findings)

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
        base = trip(notes())
        with_free = trip(notes(free=free))
        assert confirmation_code(base) == confirmation_code(base)
        assert confirmation_code(with_free) == confirmation_code(base)

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
