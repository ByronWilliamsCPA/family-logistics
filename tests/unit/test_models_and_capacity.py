"""Registry models, sensitive data handling and capacity."""

from __future__ import annotations

from datetime import date

import pytest
from fl_factories import REGISTRY, notes, trip

from family_logistics.domain.capacity import capacity_findings
from family_logistics.domain.models import (
    Location,
    Person,
    PersonRole,
    SensitiveStr,
    Severity,
    Vehicle,
)
from family_logistics.domain.registry import vehicle_by_id
from family_logistics.domain.rules import RULES, make_finding


@pytest.mark.unit
class TestSensitive:
    def test_address_never_in_repr_or_str(self):
        loc = Location(
            "loc-1",
            "Music Studio",
            address=SensitiveStr("1 Example Way"),
            coordinates=SensitiveStr("0.0,0.0"),
        )
        assert "Example" not in repr(loc)
        assert "0.0" not in repr(loc)
        assert str(SensitiveStr("x")) == "***"
        assert loc.address is not None
        assert loc.address.reveal() == "1 Example Way"

    def test_sensitive_equality_and_hash(self):
        assert SensitiveStr("a") == SensitiveStr("a")
        assert SensitiveStr("a") != "a"
        assert hash(SensitiveStr("a")) == hash(SensitiveStr("a"))


@pytest.mark.unit
class TestMonitoring:
    def test_casey_disabled_by_default(self):
        casey = next(p for p in REGISTRY.people if p.person_id == "casey")
        assert not casey.is_monitored_on(date(2026, 10, 23))

    def test_window_bounds(self):
        person = Person(
            "p",
            "P",
            PersonRole.CHILD,
            monitoring_start=date(2026, 12, 15),
            monitoring_end=date(2027, 1, 5),
        )
        assert not person.is_monitored_on(date(2026, 12, 14))
        assert person.is_monitored_on(date(2026, 12, 15))
        assert person.is_monitored_on(date(2027, 1, 5))
        assert not person.is_monitored_on(date(2027, 1, 6))

    def test_disabled_ignores_dates(self):
        person = Person(
            "p",
            "P",
            PersonRole.CHILD,
            monitoring_enabled=False,
            monitoring_start=date(2026, 1, 1),
        )
        assert not person.is_monitored_on(date(2026, 6, 1))


@pytest.mark.unit
class TestCapacity:
    def test_unknown_capacity_is_unverified_never_sufficient(self):
        found = capacity_findings(trip(notes()), vehicle_by_id(REGISTRY, "family-suv"))
        assert found[0].rule_id == "CAP-014"
        assert found[0].severity is Severity.INCOMPLETE

    def test_no_vehicle_is_unverified(self):
        assert capacity_findings(trip(notes()), None)[0].severity is Severity.INCOMPLETE

    def test_driver_counts_toward_capacity(self):
        two_seater = Vehicle("v", "Coupe", seat_capacity=2)
        one_kid = trip(notes())
        assert capacity_findings(one_kid, two_seater) == []
        two_kids = trip(
            notes(
                family="Sam, Jordan",
                destinations=(("Sam", "HOME"), ("Jordan", "HOME")),
            )
        )
        found = capacity_findings(two_kids, two_seater)
        assert found[0].severity is Severity.CRITICAL

    def test_unknown_vehicle_id(self):
        assert vehicle_by_id(REGISTRY, "nope") is None


@pytest.mark.unit
def test_issue_id_is_order_independent_and_rule_specific():
    a = make_finding("PASS-003", "m", ("x", "y"))
    b = make_finding("PASS-003", "other text", ("y", "x"))
    c = make_finding("CONF-008", "m", ("x", "y"))
    assert a.issue_id == b.issue_id
    assert a.issue_id != c.issue_id


@pytest.mark.unit
def test_catalog_has_all_handoff_rules():
    assert len(RULES) == 15
    with pytest.raises(KeyError):
        make_finding("NOPE-999", "m", ())
