"""Command-line interface for Family Logistics.

Subcommands:

- ``serve``: run the API after startup checks (refuses to start without
  ``FL_HA_TOKEN``, with an unusable database, or on a database that is not
  at the packaged schema head unless ``--upgrade-db`` is given).
- ``healthcheck``: one-shot probe of ``/healthz`` for the container healthcheck.
- ``config``: print non-secret configuration.
- ``migrate``: upgrade (or downgrade) the database schema.
- ``verify-db``: integrity and schema-revision check of a database file, for
  example a restored backup.
"""

from __future__ import annotations

import logging
import sys
from http.client import HTTPConnection
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click

from family_logistics import __version__
from family_logistics.core.config import get_settings
from family_logistics.core.database import (
    check_database,
    create_readonly_engine,
    create_sqlite_engine,
    directory_problems,
    integrity_ok,
)
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.db import migrations_api
from family_logistics.utils.logging import setup_logging

if TYPE_CHECKING:
    from family_logistics.core.config import Settings

HEALTHCHECK_TIMEOUT_SECONDS = 3
EXIT_CONFIG = 2
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
SCHEMA_BEHIND_MESSAGE = (
    "database schema is not at head; run `family-logistics migrate` "
    "or start with --upgrade-db"
)


def _fail(message: str) -> NoReturn:
    """Print a message to stderr and exit with the configuration status."""
    click.echo(message, err=True)
    sys.exit(EXIT_CONFIG)


def _settings() -> Settings:
    """Load settings, exiting cleanly (no traceback, no values) when invalid."""
    try:
        return get_settings()
    except ConfigurationError as exc:
        _fail(str(exc))


def startup_problems(settings: Settings, *, upgrade_db: bool) -> list[str]:
    """Check everything ``serve`` needs before it accepts requests.

    Args:
        settings: Application settings.
        upgrade_db: Whether a database behind the schema head may be upgraded.

    Returns:
        Problems that block startup; empty when the service may start. Messages
        never contain secret values.
    """
    problems: list[str] = []
    if settings.ha_token is None:
        problems.append("FL_HA_TOKEN is required to serve")
    if settings.notification_mode == "production" and settings.data_key is None:
        problems.append("FL_DATA_KEY is required when FL_NOTIFICATION_MODE=production")
    directory = directory_problems(settings.database_path)
    if directory:
        return problems + directory
    engine = create_sqlite_engine(settings.database_path)
    try:
        if not check_database(engine):
            problems.append(f"database {settings.database_path} cannot be opened")
        elif not migrations_api.is_at_head(engine):
            if upgrade_db:
                migrations_api.upgrade(engine)
            else:
                problems.append(SCHEMA_BEHIND_MESSAGE)
    finally:
        engine.dispose()
    return problems


def _configure_logging(settings: Settings) -> None:
    """Apply structured logging and route uvicorn's loggers through it."""
    setup_logging(
        level=settings.log_level,
        json_logs=settings.json_logs,
        include_timestamp=settings.include_timestamp,
        include_correlation=True,
    )
    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


@click.group()
@click.version_option(version=__version__, prog_name="family-logistics")
def cli() -> None:
    """Family Logistics: deterministic family transportation assurance."""


@cli.command()
@click.option(
    "--upgrade-db",
    is_flag=True,
    help="Upgrade the database schema to head before serving.",
)
def serve(upgrade_db: bool) -> None:
    """Run the API server (single process, single writer)."""
    settings = _settings()
    _configure_logging(settings)
    problems = startup_problems(settings, upgrade_db=upgrade_db)
    if problems:
        _fail("Refusing to start: " + "; ".join(problems))
    import uvicorn  # noqa: PLC0415 - server import only for this command

    from family_logistics.api.app import create_app  # noqa: PLC0415

    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=None,
        workers=1,
    )


@cli.command()
def healthcheck() -> None:
    """Probe the local /healthz endpoint; exit 0 when healthy."""
    settings = _settings()
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
    settings = _settings()
    click.echo(f"Version: {__version__}")
    click.echo(f"Timezone: {settings.timezone}")
    click.echo(f"Database: {settings.database_path}")
    click.echo(f"Route provider: {settings.route_provider}")
    click.echo(f"Notification mode: {settings.notification_mode}")
    click.echo(f"LLM enabled: {settings.llm_enabled}")
    click.echo(f"HA token configured: {settings.ha_token is not None}")


@cli.command()
@click.option(
    "--revision",
    default="head",
    show_default=True,
    help="Target revision; a revision below the current one downgrades.",
)
@click.option(
    "--downgrade",
    is_flag=True,
    help="Downgrade to --revision (may destroy data, including audit history).",
)
def migrate(revision: str, downgrade: bool) -> None:
    """Upgrade (or explicitly downgrade) the database schema."""
    settings = _settings()
    problems = directory_problems(settings.database_path)
    if problems:
        _fail("; ".join(problems))
    engine = create_sqlite_engine(settings.database_path)
    try:
        if downgrade:
            migrations_api.downgrade(engine, revision)
        else:
            migrations_api.upgrade(engine, revision)
        current = migrations_api.current_revision(engine)
    finally:
        engine.dispose()
    click.echo(f"Schema revision: {current or 'base'}")


@cli.command("verify-db")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def verify_db(path: Path) -> None:
    """Check a database file's integrity and schema revision.

    Use it on a restored backup before starting the service on it. The file is
    opened read-only and is not modified.
    """
    engine = create_readonly_engine(path)
    try:
        intact = integrity_ok(engine)
        current = migrations_api.current_revision(engine) if intact else None
    finally:
        engine.dispose()
    head = migrations_api.head_revision()
    click.echo(f"Integrity: {'ok' if intact else 'FAILED'}")
    click.echo(f"Schema revision: {current or 'none'} (head: {head})")
    sys.exit(0 if intact and current == head else 1)


if __name__ == "__main__":
    cli()
