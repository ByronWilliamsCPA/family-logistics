"""Route provider implementations behind the RouteProvider protocol."""

from family_logistics.adapters.routing.base import (
    RouteConstraints,
    RouteEstimate,
    RouteProvider,
    StaticRouteProvider,
)
from family_logistics.adapters.routing.factory import build_route_provider
from family_logistics.adapters.routing.null import NullRouteProvider
from family_logistics.core.exceptions import RouteProviderUnavailableError

__all__ = [
    "NullRouteProvider",
    "RouteConstraints",
    "RouteEstimate",
    "RouteProvider",
    "RouteProviderUnavailableError",
    "StaticRouteProvider",
    "build_route_provider",
]
