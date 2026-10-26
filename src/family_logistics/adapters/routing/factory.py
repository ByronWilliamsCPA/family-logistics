"""Build the configured route provider (``FL_ROUTE_PROVIDER``).

Only the provider selection lives here. Nothing in ``src`` calls the factory
yet; services adopt it when they are wired in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Never
from zoneinfo import ZoneInfo

from family_logistics.adapters.routing.base import StaticRouteProvider
from family_logistics.adapters.routing.ha_relay import HaRelayRouteProvider
from family_logistics.core.exceptions import RouteProviderUnavailableError
from family_logistics.db.route_relay import RouteRelayRepository

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from sqlalchemy.engine import Engine

    from family_logistics.adapters.routing.base import RouteProvider
    from family_logistics.core.config import Settings


def build_route_provider(
    settings: Settings,
    *,
    engine: Engine | None = None,
    static_matrix: Mapping[tuple[str, str], timedelta] | None = None,
) -> RouteProvider:
    """Return the route provider selected by ``settings.route_provider``.

    Args:
        settings: Application settings.
        engine: Database engine at the schema head. Required for ``ha_relay``
            (it holds the route cache and request queue) and ignored for every
            other provider.
        static_matrix: Travel-time table for the ``static`` provider. It is
            required for ``static`` and rejected for every other provider.

    Returns:
        A route provider.

    Raises:
        RouteProviderUnavailableError: For ``google_routes`` (not implemented
            yet, error code ``ROUTE_PROVIDER_NOT_IMPLEMENTED``), for ``static``
            without ``static_matrix`` (``ROUTE_PROVIDER_MATRIX_REQUIRED``), for
            ``ha_relay`` without ``engine`` (``ROUTE_PROVIDER_ENGINE_REQUIRED``)
            and for a provider name that is not one of the known values
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
        if engine is None:
            msg = (
                "FL_ROUTE_PROVIDER=ha_relay needs the database engine that holds"
                " the route cache; it cannot be built without one"
            )
            raise RouteProviderUnavailableError(
                msg, provider=name, error_code="ROUTE_PROVIDER_ENGINE_REQUIRED"
            )
        return HaRelayRouteProvider(
            RouteRelayRepository(engine), ZoneInfo(settings.timezone)
        )
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
