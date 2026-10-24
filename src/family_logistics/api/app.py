"""FastAPI application factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from family_logistics import __version__
from family_logistics.api.ha import build_ha_router
from family_logistics.api.health import build_health_router
from family_logistics.core.database import create_sqlite_engine
from family_logistics.middleware.correlation import CorrelationMiddleware
from family_logistics.middleware.security import SecurityHeadersMiddleware

if TYPE_CHECKING:
    from family_logistics.core.config import Settings


def create_app(settings: Settings) -> FastAPI:
    """Build the application.

    Args:
        settings: Application settings.

    Returns:
        The configured FastAPI app (docs disabled; not a public API).
    """
    app = FastAPI(
        title="Family Logistics",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    engine = create_sqlite_engine(settings.database_path)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationMiddleware)
    app.include_router(build_health_router(engine))
    app.include_router(build_ha_router(settings))
    return app
