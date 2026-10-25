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


class TestVersion:
    def test_payload_carries_the_format_version(self):
        payload = trip_to_json(sample())
        assert payload["version"] == 1
        assert json.loads(json.dumps(payload))["version"] == 1

    def test_an_older_payload_without_a_version_is_rejected(self):
        data = json.loads(json.dumps(trip_to_json(sample())))
        del data["version"]
        with pytest.raises(ValidationError, match="Stored trip field version is"):
            trip_from_json(data)

    @pytest.mark.parametrize(
        "version",
        [0, 2, True, "1", 1.0, None],
        ids=["zero", "unknown", "bool", "text", "float", "null"],
    )
    def test_an_unsupported_version_is_rejected(self, version):
        data = json.loads(json.dumps(trip_to_json(sample())))
        data["version"] = version
        with pytest.raises(ValidationError, match="Stored trip field version is"):
            trip_from_json(data)

    @pytest.mark.parametrize("payload", [[], ["version", 1], None, "text", 1], ids=repr)
    def test_a_payload_that_is_not_an_object_is_rejected(self, payload):
        with pytest.raises(ValidationError, match="Stored trip field payload is"):
            trip_from_json(payload)

    def test_the_error_does_not_echo_the_payload(self):
        with pytest.raises(ValidationError) as caught:
            trip_from_json({"version": "secret-street-1"})
        assert "secret-street-1" not in str(caught.value)
        assert "secret-street-1" not in repr(caught.value.details)


class TestRejectsCorruption:
    @pytest.mark.parametrize(
        ("mutate", "field"),
        [
            (lambda d: d.pop("driver_id"), "driver_id"),
            (lambda d: d.update(status="maybe"), "status"),
            (lambda d: d.update(start="2026-10-23T15:30:00"), "start"),
            (lambda d: d.update(start="not a time"), "start"),
            (lambda d: d.update(key=["only", "two"]), "key"),
            (lambda d: d.update(key=[1, 2, None]), "key"),
            (lambda d: d.update(passengers=["x"]), "passengers"),
            (lambda d: d.update(passengers="x"), "passengers"),
            (lambda d: d.update(stop_order=[1]), "stop_order"),
            (lambda d: d.update(confirm_code=5), "confirm_code"),
        ],
    )
    def test_malformed_snapshot_raises_validation_error(self, mutate, field):
        data = json.loads(json.dumps(trip_to_json(sample())))
        mutate(data)
        with pytest.raises(ValidationError, match=f"Stored trip field {field} is"):
            trip_from_json(data)
