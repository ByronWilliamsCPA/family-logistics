"""Template v1 notes parser (handoff section 4.2)."""

from __future__ import annotations

import pytest
from fl_factories import FRIDAY_END, FRIDAY_START, notes, parse

from family_logistics.domain.models import (
    DestinationKind,
    PassengerKind,
    Severity,
    TripStatus,
    TripType,
)
from family_logistics.domain.parser import read_notes


def rule_ids(result):
    return sorted(f.rule_id for f in result.findings)


@pytest.mark.unit
class TestValidNotes:
    def test_shared_ride_with_external_passenger(self):
        text = notes(
            family="Sam, Jordan",
            external="Passenger E",
            destinations=(
                ("Sam", "HOME"),
                ("Jordan", "HOME"),
                ("Passenger E", "DEFAULT"),
            ),
            free="Bring the instrument cases.",
        )
        result = parse(text)
        assert result.ok
        assert result.findings == ()
        trip = result.trip
        assert trip is not None
        assert trip.trip_type is TripType.PICKUP
        assert trip.status is TripStatus.CONFIRMED
        assert trip.driver_id == "riley"
        assert [p.passenger_id for p in trip.passengers] == [
            "sam",
            "jordan",
            "ext-e",
        ]
        assert trip.passengers[2].kind is PassengerKind.EXTERNAL
        assert trip.passengers[2].destination.kind is DestinationKind.DEFAULT
        assert trip.stop_order is None
        assert trip.free_notes == "Bring the instrument cases."
        assert len(trip.legs()) == 3

    def test_crlf_lowercase_labels_and_enum_variants(self):
        text = notes(trip_type="Drop-Off", status="CONFIRMED", newline="\r\n")
        text = text.replace("TRIP_TYPE", "trip type").replace("DRIVER", "Driver")
        result = parse(text)
        assert result.ok, [f.message for f in result.findings]
        assert result.trip is not None
        assert result.trip.trip_type is TripType.DROP_OFF

    def test_dropoff_without_space_and_driver_alias(self):
        result = parse(notes(trip_type="dropoff", driver="v"))
        assert result.trip is not None
        assert result.trip.driver_id == "riley"

    def test_unicode_nfc_and_nbsp_are_normalized(self):
        text = notes(family="Sam\u00a0", pickup="Café")
        result = parse(text)
        assert result.trip is not None
        assert result.trip.pickup == "Café"

    def test_named_destination_and_explicit_stop_order(self):
        text = notes(
            family="Sam",
            external="Passenger E",
            destinations=(
                ("Sam", "HOME"),
                ("Passenger E", "Sister game @ Gym"),
            ),
            stop_order="Passenger E, Sam",
        )
        result = parse(text)
        assert result.trip is not None
        assert result.trip.passengers[1].destination.kind is DestinationKind.NAMED
        assert result.trip.stop_order == ("Passenger E", "Sam")

    def test_confirm_code_is_uppercased(self):
        result = parse(notes(confirm="abc123"))
        assert result.trip is not None
        assert result.trip.confirm_code == "ABC123"

    def test_free_notes_may_contain_colons_and_labels(self):
        result = parse(notes(free="Gate code: ask Riley\nSTATUS: not parsed"))
        assert result.trip is not None
        assert "STATUS: not parsed" in result.trip.free_notes

    def test_external_only_trip_is_valid(self):
        text = notes(
            family="",
            external="Passenger R",
            destinations=(("Passenger R", "DEFAULT"),),
        )
        assert parse(text).ok

    def test_explicit_id_disambiguates_external(self):
        text = notes(
            family="",
            external="Passenger J [ext-j2]",
            destinations=(("Passenger J [ext-j2]", "DEFAULT"),),
        )
        result = parse(text)
        assert result.trip is not None
        assert result.trip.passengers[0].passenger_id == "ext-j2"


@pytest.mark.unit
class TestNames:
    def test_family_typo_is_critical_with_suggestion_and_no_substitution(self):
        result = parse(notes(family="Jordn", destinations=(("Jordn", "HOME"),)))
        assert result.trip is None
        name = [f for f in result.findings if f.rule_id == "NAME-002"]
        assert len(name) == 1
        assert name[0].severity is Severity.CRITICAL
        assert name[0].suggestion == "Did you mean Jordan?"

    def test_typo_issue_id_is_stable_across_runs(self):
        a = parse(notes(family="Jordn", destinations=(("Jordn", "HOME"),)))
        b = parse(notes(family="Jordn", destinations=(("Jordn", "HOME"),)))
        ids_a = {f.issue_id for f in a.findings if f.rule_id == "NAME-002"}
        ids_b = {f.issue_id for f in b.findings if f.rule_id == "NAME-002"}
        assert ids_a == ids_b

    def test_family_name_in_external_list(self):
        result = parse(
            notes(family="", external="Sam", destinations=(("Sam", "HOME"),))
        )
        assert result.trip is None
        assert "NAME-002" in rule_ids(result)

    def test_external_name_in_family_list(self):
        result = parse(
            notes(family="Passenger E", destinations=(("Passenger E", "DEFAULT"),))
        )
        finding = next(f for f in result.findings if f.rule_id == "NAME-002")
        assert "EXTERNAL_PASSENGERS" in (finding.suggestion or "")

    def test_unknown_family_name_without_close_match(self):
        result = parse(notes(family="Zzyzx", destinations=(("Zzyzx", "HOME"),)))
        finding = next(f for f in result.findings if f.rule_id == "NAME-002")
        assert finding.suggestion is None

    def test_unknown_external_is_warning_and_still_an_obligation(self):
        text = notes(
            external="Passenger Q",
            destinations=(("Sam", "HOME"), ("Passenger Q", "DEFAULT")),
        )
        result = parse(text)
        assert result.trip is not None
        assert rule_ids(result) == ["EXT-013"]
        assert result.findings[0].severity is Severity.WARNING
        assert result.trip.passengers[1].passenger_id == "unregistered:passenger q"

    def test_ambiguous_external_is_rejected_with_options(self):
        text = notes(
            family="",
            external="Passenger J",
            destinations=(("Passenger J", "DEFAULT"),),
        )
        result = parse(text)
        assert result.trip is None
        finding = next(f for f in result.findings if f.rule_id == "PARSE-001")
        assert "ext-j1" in (finding.suggestion or "")
        assert "ext-j2" in (finding.suggestion or "")

    def test_explicit_id_that_does_not_match_name(self):
        text = notes(
            family="",
            external="Passenger E [ext-r]",
            destinations=(("Passenger E", "DEFAULT"),),
        )
        assert parse(text).trip is None

    def test_unknown_driver(self):
        result = parse(notes(driver="Rilee"))
        assert result.trip is None
        finding = next(f for f in result.findings if f.rule_id == "PARSE-001")
        assert finding.suggestion == "Did you mean Riley?"

    def test_child_is_never_a_driver_by_appearing_in_event(self):
        result = parse(notes(driver="Alex"))
        assert result.trip is None
        assert any("not an authorized driver" in f.message for f in result.findings)

    def test_external_driver_contact_resolves(self):
        result = parse(notes(driver="Driver S"))
        assert result.trip is not None
        assert result.trip.driver_id == "ext-driver-s"


@pytest.mark.unit
class TestRejections:
    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            (notes(status="confirmed\nSTATUS: tentative"), "Duplicate key"),
            (notes().replace("FL_VERSION: 1", "FL_VERSION: 2"), "Unsupported"),
            (notes(trip_type="pickup | drop off | transfer"), "alternatives"),
            (notes(status="maybe"), "Unrecognized STATUS"),
            (notes().replace("PICKUP: Music Studio", "PICKUP:"), "PICKUP"),
            (notes(family="", external=""), "At least one passenger"),
            (notes(destinations=()), "No destination row"),
            (
                notes(destinations=(("Sam", "HOME"), ("Jordan", "HOME"))),
                "does not match a passenger",
            ),
            (
                notes(destinations=(("Sam", "HOME"), ("Sam", "HOME"))),
                "Duplicate destination",
            ),
            (notes(destinations=(("Sam", ""),)), "Empty destination"),
            (notes(family="Sam, Sam"), "more than once"),
            (notes(stop_order="Sam, Nowhere"), "STOP_ORDER"),
            (notes(stop_order="Sam, Sam"), "STOP_ORDER"),
            (notes(confirm="12345"), "CONFIRM"),
            ("Pick up kids at 3", "Unrecognized line"),
            (notes().replace("DESTINATIONS:\nSam: HOME\n", ""), "DESTINATIONS"),
        ],
    )
    def test_parse_001(self, text, fragment):
        result = parse(text)
        assert result.trip is None
        assert any(
            f.rule_id == "PARSE-001" and fragment in f.message for f in result.findings
        ), [f.message for f in result.findings]

    def test_invalid_window(self):
        result = parse(notes(), start=FRIDAY_END, end=FRIDAY_START)
        assert result.trip is None

    def test_naive_datetime_rejected(self):
        result = parse(notes(), start=FRIDAY_START.replace(tzinfo=None))
        assert result.trip is None


@pytest.mark.unit
def test_read_notes_keeps_destination_rows_in_order():
    raw = read_notes("DESTINATIONS:\nB: HOME\nA: DEFAULT\nNOTES:")
    assert raw.destination_rows == [("B", "HOME"), ("A", "DEFAULT")]
    assert raw.problems == []
