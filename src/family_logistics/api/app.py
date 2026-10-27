"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from family_logistics import __version__
from family_logistics.api.ha import build_ha_router
from family_logistics.api.health import build_health_router
from family_logistics.api.route_relay import register_route_relay_routes
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db.route_relay import RouteRelayRepository
from family_logistics.middleware.correlation import CorrelationMiddleware
from family_logistics.middleware.security import SecurityHeadersMiddleware
from family_logistics.services.scheduler import SchedulerService
from family_logistics.services.school_snapshot import SchoolSnapshotIngest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.engine import Engine

    from family_logistics.core.config import Settings


def _scheduler_lifespan(
    settings: Settings, engine: Engine
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan that runs the scheduler when ``FL_SCHEDULER_ENABLED``.

    A configuration error while building it stops startup; a crash in a job
    never does. A failed ``start`` still runs ``stop``, which closes the HTTP
    clients the service opened. Off by default.

    Args:
        settings: Application settings.
        engine: The database engine the app already holds.

    Returns:
        A lifespan context manager factory for ``FastAPI``.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        if not settings.scheduler_enabled:
            yield
            return
        scheduler = SchedulerService.from_settings(settings, engine)
        try:
            scheduler.start()
            yield
        finally:
            scheduler.stop()

    return lifespan


def create_app(settings: Settings) -> FastAPI:
    """Build the application.

    Args:
        settings: Application settings.

    Returns:
        The configured FastAPI app (docs disabled; not a public API).
    """
    engine = create_sqlite_engine(settings.database_path)
    app = FastAPI(
        lifespan=_scheduler_lifespan(settings, engine),
        title="Family Logistics",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationMiddleware)
    app.include_router(build_health_router(engine))
    ha_router = build_ha_router(
        settings, school_ingest=SchoolSnapshotIngest(engine, settings)
    )
    register_route_relay_routes(ha_router, settings, RouteRelayRepository(engine))
    app.include_router(ha_router)
    return app
