"""NullRouteProvider and the route provider factory (ADR 0006, PR 1 of 3)."""

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
    RouteProviderUnavailableError,
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

    def test_usable_through_the_protocol(self):
        provider: RouteProvider = NullRouteProvider()
        constraints = RouteConstraints(departure_time=NOW)
        assert provider.estimate("P", "A", NOW, "driving") is None
        result = provider.optimize("P", [Stop("a", "A")], None, constraints)
        assert result.status is OrderStatus.INCOMPLETE

    def test_a_manual_order_is_incomplete_without_travel_times(self):
        # Even a manual order cannot be checked against deadlines without
        # estimates, so it is INCOMPLETE rather than assumed to be fine. A
        # populated static provider accepts the same order; the Null provider
        # behaves exactly like a static provider that has no data.
        constraints = RouteConstraints(departure_time=NOW, fixed_order=("b", "a"))
        stops = [Stop("a", "A"), Stop("b", "B")]
        result = NullRouteProvider().optimize("P", stops, None, constraints)
        assert result.status is OrderStatus.INCOMPLETE
        assert result.order == ()
        assert result.reason.startswith("A travel estimate is unavailable")
        assert result == StaticRouteProvider({}).optimize("P", stops, None, constraints)
        populated = StaticRouteProvider(MATRIX).optimize("P", stops, None, constraints)
        assert populated.order == ("b", "a")

    def test_a_manual_order_missing_a_stop_is_refused_for_that_reason(self):
        # fixed_order reaches choose_stop_order: the coverage check runs before
        # any travel time is asked for, so the reason is the STOP_ORDER one.
        constraints = RouteConstraints(departure_time=NOW, fixed_order=("a",))
        stops = [Stop("a", "A"), Stop("b", "B")]
        result = NullRouteProvider().optimize("P", stops, None, constraints)
        assert result.status is OrderStatus.INCOMPLETE
        assert result.reason.startswith("STOP_ORDER")

    def test_no_stops_is_vacuously_feasible(self):
        constraints = RouteConstraints(departure_time=NOW)
        result = NullRouteProvider().optimize("P", [], None, constraints)
        assert result.status is OrderStatus.FEASIBLE
        assert result.order == ()

    def test_no_stops_with_an_end_is_incomplete(self):
        constraints = RouteConstraints(departure_time=NOW)
        result = NullRouteProvider().optimize("P", [], "H", constraints)
        assert result.status is OrderStatus.INCOMPLETE

    def test_an_end_location_makes_the_final_leg_unknown(self):
        constraints = RouteConstraints(departure_time=NOW, fixed_order=("a",))
        result = NullRouteProvider().optimize("P", [Stop("a", "A")], "H", constraints)
        assert result.status is OrderStatus.INCOMPLETE
        assert result.reason.startswith("A travel estimate is unavailable")

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
    def test_default_setting_is_ha_relay(self, monkeypatch):
        # Built without route_provider, and isolated from FL_ROUTE_PROVIDER and
        # from .env, so a change to the shipped default fails this test.
        monkeypatch.delenv("FL_ROUTE_PROVIDER", raising=False)
        assert Settings(_env_file=None).route_provider == "ha_relay"

    def test_the_environment_selects_the_provider(self, monkeypatch):
        monkeypatch.setenv("FL_ROUTE_PROVIDER", "google_routes")
        assert Settings(_env_file=None).route_provider == "google_routes"

    def test_ha_relay_yields_null_provider_and_warns(self, caplog):
        with caplog.at_level("WARNING"):
            provider = build_route_provider(settings("ha_relay"))
        assert isinstance(provider, NullRouteProvider)
        assert provider.estimate("a", "b", NOW, "driving") is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "routing.ha_relay_not_implemented" in message
        assert "ha_relay" in message
        assert "ROUTE-010" in message

    def test_the_ha_relay_warning_is_logged_once_per_process(self, caplog):
        with caplog.at_level("WARNING"):
            build_route_provider(settings("ha_relay"))
            build_route_provider(settings("ha_relay"))
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1

    def test_concurrent_builds_log_the_ha_relay_warning_once(self, caplog):
        # Probabilistic evidence only: the barrier releases all threads at
        # once, but a missing lock would not always lose the race.
        threads_count = 8
        barrier = threading.Barrier(threads_count)
        cfg = settings("ha_relay")

        def build() -> None:
            barrier.wait()
            build_route_provider(cfg)

        threads = [threading.Thread(target=build) for _ in range(threads_count)]
        with caplog.at_level("WARNING"):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1

    def test_google_routes_is_refused_not_silently_replaced(self):
        cfg = settings("google_routes")
        with pytest.raises(RouteProviderUnavailableError) as caught:
            build_route_provider(cfg)
        assert "google_routes" in str(caught.value)
        assert "not implemented" in str(caught.value)
        assert caught.value.provider == "google_routes"
        assert caught.value.details["provider"] == "google_routes"
        assert caught.value.error_code == "ROUTE_PROVIDER_NOT_IMPLEMENTED"

    def test_the_typed_error_belongs_to_the_hierarchy(self):
        assert issubclass(RouteProviderUnavailableError, ConfigurationError)

    def test_the_typed_error_reports_provider_and_code(self):
        error = RouteProviderUnavailableError(
            "boom",
            provider="static",
            details={"provider": "other", "extra": 1},
            error_code="CUSTOM",
        )
        assert error.provider == "static"
        assert error.details == {"provider": "static", "extra": 1}
        assert error.error_code == "CUSTOM"
        default = RouteProviderUnavailableError("boom", provider="static")
        assert default.error_code == "ROUTE_PROVIDER_NOT_IMPLEMENTED"

    def test_static_without_a_table_is_refused(self):
        cfg = settings("static")
        with pytest.raises(RouteProviderUnavailableError) as caught:
            build_route_provider(cfg)
        assert "static" in str(caught.value)
        assert caught.value.details["provider"] == "static"
        assert caught.value.error_code == "ROUTE_PROVIDER_MATRIX_REQUIRED"

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
    def test_a_table_with_a_non_static_provider_is_rejected(self, name, caplog):
        cfg = settings(name)
        caplog.set_level("WARNING")
        with pytest.raises(ValueError, match="static_matrix"):
            build_route_provider(cfg, static_matrix=MATRIX)
        # Rejected before any provider is chosen: no placeholder warning.
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_an_unknown_provider_name_fails_closed(self):
        unvalidated = Settings.model_construct(route_provider="bogus")
        with pytest.raises(RouteProviderUnavailableError) as caught:
            build_route_provider(unvalidated)
        assert caught.value.details["provider"] == "bogus"
        assert caught.value.error_code == "ROUTE_PROVIDER_UNKNOWN"

    def test_factory_is_exported_from_the_package(self):
        assert set(routing.__all__) >= {
            "NullRouteProvider",
            "RouteProviderUnavailableError",
            "build_route_provider",
        }


def confirmed(t):
    return dataclasses.replace(t, confirm_code=confirmation_code(t))


def ride(uid, start, end, *, pickup, destinations):
    return confirmed(
        trip(
            notes(
                pickup=pickup,
                family="Sam",
                destinations=destinations,
                driver="Riley",
                status="confirmed",
            ),
            uid=uid,
            start=start,
            end=end,
        )
    )


def default_provider() -> RouteProvider:
    """The provider an unconfigured deployment gets (the factory's output)."""
    return build_route_provider(settings("ha_relay"))


class TestEngineFailsClosed:
    def test_a_run_on_the_null_provider_is_incomplete_with_route_010(self):
        lesson = activity("lesson", ("sam",), "studio", at(16), at(17))
        to_lesson = ride(
            "to",
            at(15, 30),
            at(16),
            pickup="Home",
            destinations=(("Sam", "Music Studio"),),
        )
        from_lesson = ride(
            "from",
            at(17),
            at(17, 30),
            pickup="Music Studio",
            destinations=(("Sam", "HOME"),),
        )
        inp = assessment(activities=(lesson,), trips=(to_lesson, from_lesson))

        with_data = assess(inp, routes(), now=NOW)
        assert with_data.run_status is RunStatus.CLEAR

        result = assess(inp, default_provider(), now=NOW)
        rule_ids = [t.finding.rule_id for t in result.findings]
        assert "ROUTE-010" in rule_ids
        assert result.run_status is RunStatus.INCOMPLETE

    def test_the_driver_path_with_the_null_provider_is_incomplete(self):
        # Riley drops Sam at home at 16:30, then must reach the studio pickup
        # at 16:50 with a 10 minute buffer. With a 5 minute drive that is
        # judged normally; with no travel time at all the check cannot pass,
        # so it is ROUTE-010 and never a pass.
        dropoff = ride(
            "drop",
            at(16),
            at(16, 30),
            pickup="Music Studio",
            destinations=(("Sam", "HOME"),),
        )
        pickup = ride(
            "pick",
            at(16, 50),
            at(17, 20),
            pickup="Music Studio",
            destinations=(("Sam", "HOME"),),
        )
        inp = assessment(trips=(dropoff, pickup))

        judged = assess(inp, routes(5), now=NOW)
        assert not [t for t in judged.findings if t.finding.rule_id == "ROUTE-010"]
        assert judged.run_status is RunStatus.CLEAR

        slow = assess(inp, routes(30), now=NOW)
        assert [t.finding.rule_id for t in slow.findings] == ["DRIVER-006"]

        result = assess(inp, default_provider(), now=NOW)
        route_gaps = [
            t.finding for t in result.findings if t.finding.rule_id == "ROUTE-010"
        ]
        assert [f.message for f in route_gaps] == [
            "Travel time to a ride pickup is unavailable; timing is unchecked."
        ]
        assert "DRIVER-006" not in [t.finding.rule_id for t in result.findings]
        assert result.run_status is RunStatus.INCOMPLETE
