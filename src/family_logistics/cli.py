"""Command-line interface for Family Logistics.

Subcommands:

- ``serve``: run the API (refuses to start without ``FL_HA_TOKEN``).
- ``healthcheck``: one-shot probe of ``/healthz`` for the container healthcheck.
- ``config``: print non-secret configuration.
"""

from __future__ import annotations

import sys
from http.client import HTTPConnection

import click

from family_logistics import __version__
from family_logistics.core.config import get_settings

HEALTHCHECK_TIMEOUT_SECONDS = 3


@click.group()
@click.version_option(version=__version__, prog_name="family-logistics")
def cli() -> None:
    """Family Logistics: deterministic family transportation assurance."""


@cli.command()
def serve() -> None:
    """Run the API server (single process, single writer)."""
    settings = get_settings()
    if settings.ha_token is None:
        click.echo("FL_HA_TOKEN is required to serve; refusing to start.", err=True)
        sys.exit(2)
    import uvicorn  # noqa: PLC0415 - server import only for this command

    from family_logistics.api.app import create_app  # noqa: PLC0415

    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        workers=1,
    )


@cli.command()
def healthcheck() -> None:
    """Probe the local /healthz endpoint; exit 0 when healthy."""
    settings = get_settings()
    connection = HTTPConnection(
        "127.0.0.1", settings.port, timeout=HEALTHCHECK_TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", "/healthz")
        healthy = connection.getresponse().status == 200
    except OSError:
        healthy = False
    finally:
        connection.close()
    sys.exit(0 if healthy else 1)


@cli.command()
def config() -> None:
    """Display non-secret configuration values."""
    settings = get_settings()
    click.echo(f"Version: {__version__}")
    click.echo(f"Timezone: {settings.timezone}")
    click.echo(f"Database: {settings.database_path}")
    click.echo(f"Route provider: {settings.route_provider}")
    click.echo(f"Notification mode: {settings.notification_mode}")
    click.echo(f"LLM enabled: {settings.llm_enabled}")
    click.echo(f"HA token configured: {settings.ha_token is not None}")


if __name__ == "__main__":
    cli()
