"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from family_logistics import __version__
from family_logistics.api.assessments import register_assessment_routes
from family_logistics.api.ha import build_ha_router
from family_logistics.api.health import build_health_router
from family_logistics.api.route_relay import register_route_relay_routes
from family_logistics.core.database import create_sqlite_engine
from family_logistics.db.assessments import AssessmentRepository
from family_logistics.db.route_relay import RouteRelayRepository
from family_logistics.db.school_snapshots import SchoolSnapshotRepository
from family_logistics.middleware.correlation import CorrelationMiddleware
from family_logistics.middleware.security import SecurityHeadersMiddleware
from family_logistics.services.on_demand import OnDemandService
from family_logistics.services.pipeline import PipelineCoordinator
from family_logistics.services.scheduler import SchedulerService
from family_logistics.services.school_snapshot import SchoolSnapshotIngest
from family_logistics.services.status import StatusService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.engine import Engine

    from family_logistics.core.config import Settings


def _scheduler_lifespan(
    settings: Settings,
    engine: Engine,
    coordinator: PipelineCoordinator,
    on_demand: OnDemandService,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan that runs the scheduler when ``FL_SCHEDULER_ENABLED``.

    A configuration error while building it stops startup; a crash in a job
    never does. A failed ``start`` still runs ``stop``, which closes the HTTP
    clients the service opened. Off by default. Shutdown always stops the
    on-demand runs first (a queued one is abandoned at once, an executing one
    is waited for), then the scheduler, so neither outlives the app.

    Args:
        settings: Application settings.
        engine: The database engine the app already holds.
        coordinator: The pipeline lock shared with the on-demand service.
        on_demand: The on-demand run service to stop at shutdown.

    Returns:
        A lifespan context manager factory for ``FastAPI``.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            if not settings.scheduler_enabled:
                yield
                return
            scheduler = SchedulerService.from_settings(
                settings, engine, coordinator=coordinator
            )
            try:
                scheduler.start()
                yield
            finally:
                scheduler.stop()
        finally:
            on_demand.stop()

    return lifespan


def create_app(settings: Settings) -> FastAPI:
    """Build the application.

    Args:
        settings: Application settings.

    Returns:
        The configured FastAPI app (docs disabled; not a public API).
    """
    engine = create_sqlite_engine(settings.database_path)
    # #CRITICAL: Concurrency: ONE coordinator, and so one pipeline lock, serves
    # the scheduler and the on-demand API whether or not the scheduler is enabled.
    # #VERIFY: tests/unit/test_on_demand.py (TestSharedLock).
    coordinator = PipelineCoordinator()
    on_demand = OnDemandService.from_settings(settings, engine, coordinator)
    app = FastAPI(
        lifespan=_scheduler_lifespan(settings, engine, coordinator, on_demand),
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
    status_service = StatusService(
        settings,
        AssessmentRepository(engine),
        SchoolSnapshotRepository(engine),
        on_demand,
    )
    register_assessment_routes(ha_router, status_service, on_demand)
    app.include_router(ha_router)
    return app
