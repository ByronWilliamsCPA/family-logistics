"""Build the configured route provider (``FL_ROUTE_PROVIDER``).

Only the provider selection lives here. Nothing in ``src`` calls the factory
yet; services adopt it when they are wired in M3.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from family_logistics.adapters.routing.base import StaticRouteProvider
from family_logistics.adapters.routing.null import NullRouteProvider
from family_logistics.core.exceptions import RouteProviderNotImplementedError
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from family_logistics.adapters.routing.base import RouteProvider
    from family_logistics.core.config import Settings

logger = get_logger(__name__)

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
            required for ``static`` and ignored for every other provider.

    Returns:
        A route provider.

    Raises:
        RouteProviderNotImplementedError: For ``google_routes`` (not
            implemented yet) and for ``static`` without ``static_matrix``.
    """
    name = settings.route_provider
    if name == "ha_relay":
        return _build_ha_relay()
    if name == "static":
        if static_matrix is None:
            msg = (
                "FL_ROUTE_PROVIDER=static needs an explicit travel-time table;"
                " it is for tests and simulation, not for a deployment"
            )
            raise RouteProviderNotImplementedError(msg, provider=name)
        return StaticRouteProvider(static_matrix)
    if name == "google_routes":
        # Refuse rather than fall back to another provider.
        msg = f"FL_ROUTE_PROVIDER={name} is not implemented yet"
        raise RouteProviderNotImplementedError(msg, provider=name)
    # A name outside the Literal (a settings object built without validation)
    # fails closed: no provider is guessed.
    msg = f"FL_ROUTE_PROVIDER={name!r} is not a known route provider"
    raise RouteProviderNotImplementedError(msg, provider=str(name))


def _build_ha_relay() -> RouteProvider:
    """Return the placeholder for the Home Assistant relay provider."""
    # #CRITICAL: External resources: the ha_relay provider does not exist yet,
    # so this returns NullRouteProvider and every travel time is unknown.
    # #VERIFY: PR 2 of the routing series replaces this branch with the real
    # relay; until then the Null fallback must fail closed (ROUTE-010 and an
    # INCOMPLETE run, never a guessed duration), pinned by
    # tests/unit/test_route_provider_factory.py::TestEngineFailsClosed.
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
