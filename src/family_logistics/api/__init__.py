"""API package for Family Logistics.

This package contains FastAPI routers and API-related functionality.
"""

from __future__ import annotations

from family_logistics.api.health import build_health_router

__all__ = ["build_health_router"]
