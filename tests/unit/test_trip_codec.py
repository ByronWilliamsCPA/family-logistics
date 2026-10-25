"""Trip snapshot encoding: round trip and rejection of corrupted data."""

from __future__ import annotations

import json

import pytest
from fl_factories import notes, trip

from family_logistics.core.exceptions import ValidationError
from family_logistics.domain.trip_codec import trip_from_json, trip_to_json

pytestmark = pytest.mark.unit


def sample():
    return trip(
        notes(
            family="Sam, Alex",
            external="Passenger E",
            destinations=(
                ("Sam", "HOME"),
                ("Alex", "HOME"),
                ("Passenger E", "DEFAULT"),
            ),
            stop_order="Sam, Alex, Passenger E",
            confirm="A1B2C3",
            free="call from the gate",
        ),
        recurrence_id="2026-10-23T22:30:00Z",
    )


class TestRoundTrip:
    def test_survives_json_and_drops_free_notes(self):
        original = sample()
        decoded = trip_from_json(json.loads(json.dumps(trip_to_json(original))))
        assert decoded.free_notes == ""
        assert decoded.legs() == original.legs()
        assert decoded.key == original.key
        assert decoded.stop_order == original.stop_order
        assert decoded.confirm_code == original.confirm_code
        assert (decoded.start, decoded.end) == (original.start, original.end)

    def test_free_notes_never_reach_the_payload(self):
        assert "call from the gate" not in json.dumps(trip_to_json(sample()))

    def test_automatic_stop_order_and_single_event(self):
        original = trip(notes())
        decoded = trip_from_json(trip_to_json(original))
        assert decoded.stop_order is None
        assert decoded.key.recurrence_id is None


class TestRejectsCorruption:
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.pop("driver_id"),
            lambda d: d.update(status="maybe"),
            lambda d: d.update(start="2026-10-23T15:30:00"),
            lambda d: d.update(start="not a time"),
            lambda d: d.update(key=["only", "two"]),
            lambda d: d.update(key=[1, 2, None]),
            lambda d: d.update(passengers=["x"]),
            lambda d: d.update(passengers="x"),
            lambda d: d.update(stop_order=[1]),
            lambda d: d.update(confirm_code=5),
        ],
    )
    def test_malformed_snapshot_raises_validation_error(self, mutate):
        data = json.loads(json.dumps(trip_to_json(sample())))
        mutate(data)
        with pytest.raises(ValidationError):
            trip_from_json(data)
