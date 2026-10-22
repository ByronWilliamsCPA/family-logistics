"""Construction of service objects from settings (composition root).

Kept separate from the CLI so tests can build the same objects with fakes.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from family_logistics.adapters.caldav.client import CalDAVClient
from family_logistics.adapters.caldav.readonly_transport import build_readonly_client
from family_logistics.db.sources import (
    CalendarSourceRepository,
    SourceEventRepository,
    SyncStateRepository,
)
from family_logistics.services.collector import Collector, CollectorConfig

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.engine import Engine

    from family_logistics.core.config import Settings


def collector_config(settings: Settings) -> CollectorConfig:
    """Build the collector configuration from settings.

    Args:
        settings: Application settings.

    Returns:
        The collector configuration.
    """
    return CollectorConfig(
        timezone=settings.timezone,
        days_back=settings.collect_days_back,
        days_ahead=settings.collect_days_ahead,
        transport_calendar=settings.caldav_transport_calendar,
        selected=tuple(settings.calendar_list),
        max_staleness=timedelta(minutes=settings.source_max_staleness_minutes),
    )


def build_caldav_client(
    settings: Settings, inner: httpx.BaseTransport | None = None
) -> tuple[CalDAVClient, httpx.Client]:
    """Build the read-only CalDAV client from settings.

    Args:
        settings: Application settings (CalDAV credentials required).
        inner: Transport performing I/O (tests pass a mock transport).

    Returns:
        The CalDAV client and its HTTP client (the caller closes it).

    Raises:
        ValueError: When the CalDAV credentials are not configured.
    """
    password = settings.caldav_app_password
    if not settings.caldav_username or password is None:
        msg = "CalDAV credentials are not configured"
        raise ValueError(msg)
    http = build_readonly_client(
        settings.caldav_url,
        auth=(settings.caldav_username, password.get_secret_value()),
        inner=inner,
    )
    return CalDAVClient(http), http


def build_collector(
    settings: Settings,
    engine: Engine,
    inner: httpx.BaseTransport | None = None,
) -> tuple[Collector, httpx.Client]:
    """Build a collector wired to the read-only CalDAV client and the database.

    Missing CalDAV credentials propagate as ``ValueError`` from
    ``build_caldav_client``.

    Args:
        settings: Application settings (CalDAV credentials required).
        engine: Database engine at the schema head.
        inner: Transport performing I/O (tests pass a mock transport).

    Returns:
        The collector and its HTTP client (the caller closes it).
    """
    client, http = build_caldav_client(settings, inner)
    collector = Collector(
        client=client,
        sources=CalendarSourceRepository(engine),
        events=SourceEventRepository(engine),
        sync_states=SyncStateRepository(engine),
        config=collector_config(settings),
    )
    return collector, http
