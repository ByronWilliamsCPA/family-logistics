"""NullRouteProvider and the route provider factory (ADR-006, PR 1 of 3)."""

from __future__ import annotations

import dataclasses
import threading
from datetime import timedelta

import pytest
from fl_engine import NOW, activity, assessment, at, routes
from fl_factories import notes, trip

from family_logistics.adapters import routing
from family_logistics.adapters.routing import (
    NullRouteProvider,
    RouteConstraints,
    RouteProvider,
    RouteProviderNotImplementedError,
    StaticRouteProvider,
    build_route_provider,
)
from family_logistics.core.config import RouteProviderName, Settings
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.domain.confirmation import confirmation_code
from family_logistics.domain.engine import assess
from family_logistics.domain.models import RunStatus
from family_logistics.domain.stop_order import OrderStatus, Stop

pytestmark = pytest.mark.unit

MATRIX = {
    ("P", "A"): timedelta(minutes=5),
    ("P", "B"): timedelta(minutes=20),
    ("A", "B"): timedelta(minutes=18),
    ("B", "A"): timedelta(minutes=18),
}


@pytest.fixture(autouse=True)
def _fresh_warning_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the warn-once flag so each test sees the first call."""
    monkeypatch.setattr(
        "family_logistics.adapters.routing.factory._ha_relay_warned",
        threading.Event(),
    )


def settings(route_provider: RouteProviderName = "ha_relay") -> Settings:
    return Settings(route_provider=route_provider)


class TestNullProvider:
    def test_estimate_is_always_unknown(self):
        provider = NullRouteProvider()
        assert provider.estimate("P", "A", NOW, "driving") is None
        assert provider.estimate("A", "A", NOW, "walking") is None

    def test_name_is_recorded(self):
        assert NullRouteProvider().name == "null"

    def test_satisfies_the_protocol(self):
        provider: RouteProvider = NullRouteProvider()
        assert provider.name

    def test_a_manual_order_is_still_incomplete_without_travel_times(self):
        # Even a manual order cannot be checked against deadlines without
        # estimates, so it is INCOMPLETE rather than assumed to be fine.
        constraints = RouteConstraints(departure_time=NOW, fixed_order=("b", "a"))
        stops = [Stop("a", "A"), Stop("b", "B")]
        result = NullRouteProvider().optimize("P", stops, None, constraints)
        assert result.status is OrderStatus.INCOMPLETE
        assert result.order == ()

    def test_unlike_a_populated_static_provider_it_never_returns_an_order(self):
        constraints = RouteConstraints(departure_time=NOW, fixed_order=("b", "a"))
        stops = [Stop("a", "A"), Stop("b", "B")]
        populated = StaticRouteProvider(MATRIX).optimize("P", stops, None, constraints)
        assert populated.order == ("b", "a")
        assert (
            NullRouteProvider().optimize("P", stops, None, constraints).status
            is OrderStatus.INCOMPLETE
        )

    def test_manual_order_matches_a_static_provider_with_no_data(self):
        constraints = RouteConstraints(departure_time=NOW, fixed_order=("b", "a"))
        stops = [Stop("a", "A"), Stop("b", "B")]
        assert NullRouteProvider().optimize(
            "P", stops, None, constraints
        ) == StaticRouteProvider({}).optimize("P", stops, None, constraints)

    def test_free_order_with_unknown_times_is_incomplete_never_guessed(self):
        constraints = RouteConstraints(departure_time=NOW)
        result = NullRouteProvider().optimize(
            "P", [Stop("a", "A"), Stop("b", "B")], None, constraints
        )
        assert result.status is OrderStatus.INCOMPLETE

    def test_matches_a_static_provider_with_no_data(self):
        constraints = RouteConstraints(departure_time=NOW)
        stops = [Stop("a", "A"), Stop("b", "B")]
        assert NullRouteProvider().optimize(
            "P", stops, None, constraints
        ) == StaticRouteProvider({}).optimize("P", stops, None, constraints)


class TestFactory:
    def test_default_setting_is_ha_relay(self):
        assert settings().route_provider == "ha_relay"

    def test_ha_relay_yields_null_provider_and_warns(self, caplog):
        with caplog.at_level("WARNING"):
            provider = build_route_provider(settings("ha_relay"))
        assert isinstance(provider, NullRouteProvider)
        assert provider.estimate("a", "b", NOW, "driving") is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "routing.ha_relay_not_implemented" in warnings[0].getMessage()

    def test_the_ha_relay_warning_is_logged_once_per_process(self, caplog):
        with caplog.at_level("WARNING"):
            build_route_provider(settings("ha_relay"))
            build_route_provider(settings("ha_relay"))
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1

    def test_google_routes_is_refused_not_silently_replaced(self):
        with pytest.raises(RouteProviderNotImplementedError) as caught:
            build_route_provider(settings("google_routes"))
        assert "google_routes" in str(caught.value)
        assert "not implemented" in str(caught.value)
        assert caught.value.details["provider"] == "google_routes"

    def test_the_typed_error_belongs_to_the_hierarchy(self):
        assert issubclass(RouteProviderNotImplementedError, ConfigurationError)

    def test_static_without_a_table_is_refused(self):
        with pytest.raises(RouteProviderNotImplementedError) as caught:
            build_route_provider(settings("static"))
        assert "static" in str(caught.value)

    def test_static_with_an_explicit_table_is_built(self):
        provider = build_route_provider(settings("static"), static_matrix=MATRIX)
        assert isinstance(provider, StaticRouteProvider)
        found = provider.estimate("P", "A", NOW, "driving")
        assert found is not None
        assert found.duration == timedelta(minutes=5)

    def test_an_empty_explicit_table_still_counts_as_supplied(self):
        provider = build_route_provider(settings("static"), static_matrix={})
        assert isinstance(provider, StaticRouteProvider)
        assert provider.estimate("P", "A", NOW, "driving") is None

    @pytest.mark.parametrize("name", ["ha_relay", "google_routes"])
    def test_a_table_is_ignored_for_other_providers(self, name):
        # A static table must never turn a real provider into the static one.
        try:
            provider = build_route_provider(settings(name), static_matrix=MATRIX)
        except RouteProviderNotImplementedError:
            return
        assert not isinstance(provider, StaticRouteProvider)

    def test_factory_is_exported_from_the_package(self):
        assert set(routing.__all__) >= {
            "NullRouteProvider",
            "RouteProviderNotImplementedError",
            "build_route_provider",
        }

    def test_the_test_helper_still_builds_a_static_provider(self):
        assert isinstance(routes(), StaticRouteProvider)


def confirmed(t):
    return dataclasses.replace(t, confirm_code=confirmation_code(t))


class TestEngineFailsClosed:
    def test_a_run_on_the_null_provider_is_incomplete_with_route_010(self):
        lesson = activity("lesson", ("sam",), "studio", at(16), at(17))
        to_lesson = confirmed(
            trip(
                notes(
                    pickup="Home",
                    family="Sam",
                    destinations=(("Sam", "Music Studio"),),
                    driver="Riley",
                    status="confirmed",
                ),
                uid="to",
                start=at(15, 30),
                end=at(16),
            )
        )
        from_lesson = confirmed(
            trip(
                notes(
                    pickup="Music Studio",
                    family="Sam",
                    destinations=(("Sam", "HOME"),),
                    driver="Riley",
                    status="confirmed",
                ),
                uid="from",
                start=at(17),
                end=at(17, 30),
            )
        )
        inp = assessment(activities=(lesson,), trips=(to_lesson, from_lesson))

        with_data = assess(inp, routes(), now=NOW)
        assert with_data.run_status is RunStatus.CLEAR

        result = assess(inp, NullRouteProvider(), now=NOW)
        rule_ids = [t.finding.rule_id for t in result.findings]
        assert "ROUTE-010" in rule_ids
        assert result.run_status is RunStatus.INCOMPLETE
        assert result.run_status is not RunStatus.CLEAR
