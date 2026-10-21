"""Health endpoint.

``GET /healthz`` is the only unauthenticated route. It reports whether the
process is up and the database answers; it reveals nothing else. Uptime Kuma
and the container healthcheck both use it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from family_logistics import __version__
from family_logistics.core.database import check_database

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


class HealthStatus(BaseModel):
    """Health response.

    Attributes:
        status: ``ok`` or ``degraded``.
        database: Whether the database answered.
        version: Application version.
    """

    status: str
    database: bool
    version: str = __version__


def build_health_router(engine: Engine) -> APIRouter:
    """Build the health router bound to the application database.

    Args:
        engine: The application database engine.

    Returns:
        A router serving ``GET /healthz``.
    """
    router = APIRouter(tags=["health"])

    @router.get("/healthz", response_model=HealthStatus)
    def healthz(response: Response) -> HealthStatus:  # pyright: ignore[reportUnusedFunction] - registered by decorator
        """Report liveness and database reachability.

        Args:
            response: Response, whose status becomes 503 when degraded.

        Returns:
            The health status.
        """
        database_ok = check_database(engine)
        if not database_ok:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatus(
            status="ok" if database_ok else "degraded", database=database_ok
        )

    return router
