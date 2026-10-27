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
from family_logistics.db.sources import CalendarSourceRepository
from family_logistics.middleware.correlation import CorrelationMiddleware
from family_logistics.middleware.security import SecurityHeadersMiddleware
from family_logistics.services.on_demand import OnDemandService
from family_logistics.services.pipeline import PipelineCoordinator
from family_logistics.services.scheduler import SchedulerService
from family_logistics.services.school_snapshot import SchoolSnapshotIngest
from family_logistics.services.status import StatusService
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.engine import Engine

    from family_logistics.core.config import Settings

logger = get_logger(__name__)


def _scheduler_lifespan(
    settings: Settings,
    engine: Engine,
    coordinator: PipelineCoordinator,
    on_demand: OnDemandService,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan that runs the scheduler when ``FL_SCHEDULER_ENABLED``.

    A configuration error while building it stops startup; a crash in a job
    never does. A failed ``start`` still runs ``stop``, which closes the HTTP
    clients the service opened. Off by default. Shutdown first tells the
    on-demand service to begin stopping (a queued run is abandoned, no new one
    is taken), then stops the scheduler, which waits for its running pipeline
    and so releases the shared lock, and last waits for an executing on-demand
    run, so neither outlives the app and the lock the scheduler releases never
    starts a queued run.

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
            if settings.on_demand_enabled:
                # Logged so an operator can see from the log that the Home
                # Assistant button can start runs (the setting ships off).
                logger.info(
                    "on_demand.enabled", scheduler_enabled=settings.scheduler_enabled
                )
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
                # #CRITICAL: Concurrency: a run queued behind the scheduler's
                # pipeline waits on the shared lock, and ``scheduler.stop()``
                # releases that lock when its pipeline ends. The stop flag must
                # already be set, or the queued run starts during shutdown.
                # #VERIFY: tests/unit/test_app_wiring.py (TestShutdownOrder).
                on_demand.begin_stop()
                scheduler.stop()
        finally:
            # Also reached when the scheduler cannot be built or started.
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
    # #VERIFY: tests/unit/test_app_wiring.py (TestSharedLock).
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
        runs=AssessmentRepository(engine),
        schools=SchoolSnapshotRepository(engine),
        sources=CalendarSourceRepository(engine),
        on_demand=on_demand,
    )
    register_assessment_routes(
        ha_router,
        status_service,
        on_demand,
        on_demand_enabled=settings.on_demand_enabled,
    )
    app.include_router(ha_router)
    return app
