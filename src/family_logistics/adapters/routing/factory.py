"""Build the configured route provider (``FL_ROUTE_PROVIDER``).

Only the provider selection lives here. Nothing in ``src`` calls the factory
yet; services adopt it when they are wired in M3.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Never

from family_logistics.adapters.routing.base import StaticRouteProvider
from family_logistics.adapters.routing.null import NullRouteProvider
from family_logistics.core.exceptions import RouteProviderUnavailableError
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from family_logistics.adapters.routing.base import RouteProvider
    from family_logistics.core.config import Settings

logger = get_logger(__name__)

# #EDGE: Concurrency: the warn-once flag is per process and shared by threads;
# the lock makes the check-and-set atomic, so at most one warning is logged.
# #VERIFY: TestFactory::test_concurrent_builds_log_the_ha_relay_warning_once.
_ha_relay_warned = threading.Event()
"""Set once the ha_relay placeholder warning is logged in this process."""

_warn_lock = threading.Lock()
"""Guards the check-and-set of ``_ha_relay_warned``."""


def build_route_provider(
    settings: Settings,
    *,
    static_matrix: Mapping[tuple[str, str], timedelta] | None = None,
) -> RouteProvider:
    """Return the route provider selected by ``settings.route_provider``.

    Args:
        settings: Application settings.
        static_matrix: Travel-time table for the ``static`` provider. It is
            required for ``static`` and rejected for every other provider.

    Returns:
        A route provider.

    Raises:
        RouteProviderUnavailableError: For ``google_routes`` (not implemented
            yet, error code ``ROUTE_PROVIDER_NOT_IMPLEMENTED``), for ``static``
            without ``static_matrix`` (``ROUTE_PROVIDER_MATRIX_REQUIRED``) and
            for a provider name that is not one of the known values
            (``ROUTE_PROVIDER_UNKNOWN``).
        ValueError: When ``static_matrix`` is passed with a provider other than
            ``static``; the table would be ignored and a simulation would be
            INCOMPLETE with no hint why.
    """
    name = settings.route_provider
    if static_matrix is not None and name != "static":
        msg = (
            f"static_matrix is only used by FL_ROUTE_PROVIDER=static, not {name!r};"
            " refusing to ignore it"
        )
        raise ValueError(msg)
    if name == "ha_relay":
        return _build_ha_relay()
    if name == "static":
        if static_matrix is None:
            msg = (
                "FL_ROUTE_PROVIDER=static needs an explicit travel-time table;"
                " it is for tests and simulation, not for a deployment"
            )
            raise RouteProviderUnavailableError(
                msg, provider=name, error_code="ROUTE_PROVIDER_MATRIX_REQUIRED"
            )
        return StaticRouteProvider(static_matrix)
    if name == "google_routes":
        # Refuse rather than fall back to another provider.
        msg = f"FL_ROUTE_PROVIDER={name} is not implemented yet"
        raise RouteProviderUnavailableError(msg, provider=name)
    unknown = _unknown_provider_name(name)
    msg = f"FL_ROUTE_PROVIDER={unknown!r} is not a known route provider"
    raise RouteProviderUnavailableError(
        msg, provider=unknown, error_code="ROUTE_PROVIDER_UNKNOWN"
    )


def _unknown_provider_name(name: Never) -> str:
    """Return a provider name outside ``RouteProviderName`` as text.

    The ``Never`` parameter makes the dispatch in ``build_route_provider``
    exhaustive for the type checker: adding a member to ``RouteProviderName``
    without a branch there is a type error. At runtime this is reached only
    by a settings object built without validation, and no provider is guessed.

    Args:
        name: The unrecognised provider name.

    Returns:
        The name, for the error message.
    """
    return str(name)


def _build_ha_relay() -> RouteProvider:
    """Return the placeholder for the Home Assistant relay provider.

    Logs one structured warning per process, on the first build only.

    Returns:
        A ``NullRouteProvider``: every travel estimate is unknown.
    """
    # #CRITICAL: External resources: the ha_relay provider does not exist yet,
    # so this returns NullRouteProvider and every travel time is unknown.
    # #VERIFY: PR 2 of the routing series replaces this branch with the real
    # relay; until then the Null fallback must fail closed (ROUTE-010 and an
    # INCOMPLETE run, never a guessed duration), pinned by
    # tests/unit/test_route_provider_factory.py::TestEngineFailsClosed.
    # #ASSUME: External resources: the single warning per process is the only
    # operator signal that the placeholder is active.
    # #VERIFY: the wiring PR (M3) must surface the active provider name and
    # "degraded: null" in run output or /healthz before relying on ha_relay.
    with _warn_lock:
        first = not _ha_relay_warned.is_set()
        _ha_relay_warned.set()
    if first:
        logger.warning(
            "routing.ha_relay_not_implemented",
            provider="ha_relay",
            effect="every travel estimate is unknown (ROUTE-010, run INCOMPLETE)",
        )
    return NullRouteProvider()
