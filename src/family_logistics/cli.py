"""Command-line interface for Family Logistics.

Subcommands:

- ``serve``: run the API after startup checks (refuses to start without
  ``FL_HA_TOKEN``, without ``FL_DATA_KEY`` and ``FL_APPRISE_URL`` in production
  notification mode, with an unusable database, on a schema newer than this
  release, or on a schema behind head unless ``--upgrade-db`` is given).
- ``healthcheck``: one-shot probe of ``/healthz`` for the container healthcheck.
- ``config``: print non-secret configuration.
- ``migrate``: upgrade (or, with confirmation, downgrade) the database schema.
- ``verify-db``: integrity and schema-revision check of a database file, for
  example a restored backup.
"""

from __future__ import annotations

import ipaddress
import logging
import sys
from http.client import HTTPConnection, HTTPException
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


def _config_problems(settings: Settings) -> list[str]:
    """Return configuration that blocks ``serve`` (names only, no values)."""
    problems: list[str] = []
    if settings.ha_token is None:
        problems.append("FL_HA_TOKEN is required to serve")
    if settings.notification_mode == "production":
        if settings.data_key is None:
            problems.append(
                "FL_DATA_KEY is required when FL_NOTIFICATION_MODE=production"
            )
        if settings.apprise_url is None:
            problems.append(
                "FL_APPRISE_URL is required when FL_NOTIFICATION_MODE=production"
            )
    return problems


def _existing_database_verdict(path: Path) -> list[str] | None:
    """Inspect an existing database through a read-only connection.

    Args:
        path: Database file path.

    Returns:
        ``[]`` when it is at head, the blocking problems when it cannot be
        used or records a schema newer than this release, or None when it is
        missing or behind head (an upgrade decides).
    """
    if not path.exists():
        return None
    engine = create_readonly_engine(path)
    try:
        if not check_database(engine):
            return [f"database {path} cannot be opened"]
        state = migrations_api.schema_state(engine)
        if state is migrations_api.SchemaState.AHEAD:
            current = migrations_api.current_revision(engine)
            return [migrations_api.ahead_message(current)]
    finally:
        engine.dispose()
    if state is migrations_api.SchemaState.AT_HEAD:
        return []
    return None


def startup_problems(settings: Settings, *, upgrade_db: bool) -> list[str]:
    """Check everything ``serve`` needs before it accepts requests.

    Configuration and directory problems are reported without opening the
    database. An existing database is then inspected through a read-only
    connection, so a refused start never creates, upgrades or otherwise
    changes the file. Only when every check passed and ``upgrade_db`` is set
    is a missing or behind-head schema upgraded (atomically, under the
    database's write lock). A schema newer than this release is refused, never
    upgraded or downgraded.

    Args:
        settings: Application settings.
        upgrade_db: Whether a database behind the schema head may be upgraded.

    Returns:
        Problems that block startup; empty when the service may start. Messages
        never contain secret values.
    """
    problems = _config_problems(settings) + directory_problems(settings.database_path)
    if problems:
        return problems
    verdict = _existing_database_verdict(settings.database_path)
    if verdict is not None:
        return verdict
    if not upgrade_db:
        return [SCHEMA_BEHIND_MESSAGE]
    engine = create_sqlite_engine(settings.database_path)
    try:
        migrations_api.upgrade(engine)
    except migrations_api.MigrationError as exc:
        return [str(exc)]
    finally:
        engine.dispose()
    return []


def _probe_host(bind_host: str) -> str:
    """Return where the healthcheck should connect for a bind address.

    A wildcard bind address is not a destination, so its loopback address of
    the same family is probed instead; a specific address is probed as is.
    """
    try:
        address = ipaddress.ip_address(bind_host)
    except ValueError:
        return bind_host
    if not address.is_unspecified:
        return bind_host
    return "::1" if address.version == 6 else "127.0.0.1"


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
        _probe_host(settings.bind_host),
        settings.port,
        timeout=HEALTHCHECK_TIMEOUT_SECONDS,
    )
    try:
        connection.request("GET", "/healthz")
        healthy = connection.getresponse().status == 200
    except (OSError, HTTPException):
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
    default=None,
    help=(
        "Target revision id, head or base. Upgrades only move forward (default "
        "head); a target below the current revision needs --downgrade."
    ),
)
@click.option(
    "--downgrade",
    is_flag=True,
    help=(
        "Downgrade to --revision, which is required and must be below the "
        "current revision (may destroy data, including audit history)."
    ),
)
@click.option("--yes", is_flag=True, help="Downgrade without asking to confirm.")
def migrate(revision: str | None, downgrade: bool, yes: bool) -> None:
    """Upgrade (or explicitly downgrade) the database schema.

    Exit status is 0 on success, 1 when a downgrade is not confirmed, and 2
    when the migration is refused or fails (the schema is then unchanged).
    """
    # #CRITICAL: Data integrity: --downgrade below 0001 drops the append-only
    # audit log and its history. It needs the explicit flag, a --revision and a
    # confirmation (or --yes); a lower --revision without the flag is refused.
    # #VERIFY: tests/unit/test_cli_ops.py::TestMigrateAndVerify covers the
    # refused lower revision, the missing --revision and the confirmation.
    settings = _settings()
    problems = directory_problems(settings.database_path)
    if problems:
        _fail("; ".join(problems))
    if downgrade:
        if revision is None:
            _fail("--downgrade needs --revision, for example --revision base")
        if not yes:
            prompt = (
                f"Downgrade the schema to {revision}? This may destroy data, "
                "including audit history; take a backup first"
            )
            click.confirm(prompt, abort=True)
    engine = create_sqlite_engine(settings.database_path)
    try:
        try:
            if downgrade and revision is not None:
                migrations_api.downgrade(engine, revision)
            else:
                migrations_api.upgrade(engine, revision or "head")
        except migrations_api.MigrationError as exc:
            _fail(str(exc))
        current = migrations_api.current_revision(engine)
    finally:
        engine.dispose()
    click.echo(f"Schema revision: {current or 'base'}")


@cli.command("verify-db")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def verify_db(path: Path) -> None:
    """Check a database file's integrity and schema revision.

    Use it on a restored backup before starting the service on it. The file is
    opened through SQLite's read-only mode, so its contents are never changed;
    SQLite may still create ``-wal`` and ``-shm`` files beside a WAL database.

    Exit status is 0 only when the file is intact and at this release's schema
    head; 1 when the integrity check fails or the schema is behind head or
    newer than this release.
    """
    engine = create_readonly_engine(path)
    try:
        intact = integrity_ok(engine)
        current = migrations_api.current_revision(engine) if intact else None
        state = migrations_api.schema_state(engine) if intact else None
    finally:
        engine.dispose()
    head = migrations_api.head_revision()
    click.echo(f"Integrity: {'ok' if intact else 'FAILED'}")
    if state is not None:
        click.echo(f"Schema state: {state.value}")
    click.echo(f"Schema revision: {current or 'none'} (head: {head})")
    sys.exit(0 if intact and current == head else 1)


if __name__ == "__main__":
    cli()
