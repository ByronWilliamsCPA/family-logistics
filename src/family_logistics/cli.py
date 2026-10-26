"""Command-line interface for Family Logistics.

Subcommands:

- ``serve``: run the API after startup checks (refuses to start without
  ``FL_HA_TOKEN``, without ``FL_DATA_KEY``, ``FL_AUDIT_KEY`` and
  ``FL_APPRISE_URL`` in production notification mode, with an unusable database, on a schema newer than this
  release, or on a schema behind head unless ``--upgrade-db`` is given).
- ``healthcheck``: one-shot probe of ``/healthz`` for the container healthcheck.
- ``config``: print non-secret configuration.
- ``migrate``: upgrade (or, with confirmation, downgrade) the database schema.
- ``verify-db``: integrity and schema-revision check of a database file, for
  example a restored backup.
- ``registry load``: validate the private registry seed and apply it
  atomically (``--dry-run`` shows the plan only).
- ``registry show``: list the registry without addresses or coordinates.
- ``collect --once``: read the configured iCloud calendars once and store their
  occurrences (prints calendar IDs and counts only).
"""

from __future__ import annotations

import ipaddress
import logging
import sys
from datetime import datetime
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from zoneinfo import ZoneInfo

import click

from family_logistics import __version__
from family_logistics.core.config import get_settings
from family_logistics.core.crypto import AddressCipher, ProtectedDataError
from family_logistics.core.database import (
    check_database,
    create_readonly_engine,
    create_sqlite_engine,
    directory_problems,
    integrity_ok,
)
from family_logistics.core.exceptions import ConfigurationError, ValidationError
from family_logistics.db import migrations_api
from family_logistics.db.registry import RegistryError, RegistryStore
from family_logistics.services.registry_seed import (
    SeedError,
    apply_seed,
    load_seed_file,
)
from family_logistics.services.scheduler import scheduler_problems
from family_logistics.utils.logging import setup_logging

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import Engine

    from family_logistics.core.config import Settings
    from family_logistics.domain.events import CalendarSource
    from family_logistics.domain.models import Location, Vehicle

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
        if settings.audit_key is None:
            problems.append(
                "FL_AUDIT_KEY is required when FL_NOTIFICATION_MODE=production"
            )
        if settings.apprise_url is None:
            problems.append(
                "FL_APPRISE_URL is required when FL_NOTIFICATION_MODE=production"
            )
    if settings.scheduler_enabled:
        problems += _credential_problems(settings) + scheduler_problems(settings)
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
    """Run the API server (single process, single writer).

    The scheduler runs inside it when FL_SCHEDULER_ENABLED is true.
    """
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
        # #CRITICAL: Security: uvicorn trusts X-Forwarded-For from
        # FORWARDED_ALLOW_IPS (default 127.0.0.1) and rewrites request.client,
        # which would let a client pick its own HA throttle key. Keep it off:
        # request.client must be the TCP peer, and FL_PROXY_TRUSTED_CIDRS (the
        # app's own proxy trust) is matched against that peer.
        # #VERIFY: tests/unit/test_app_cli_config.py asserts proxy_headers is
        # False for serve.
        proxy_headers=False,
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


def _engine_at_head(settings: Settings) -> Engine:
    """Open the application database, exiting unless it is at the schema head."""
    problems = directory_problems(settings.database_path)
    if problems:
        _fail("; ".join(problems))
    engine = create_sqlite_engine(settings.database_path)
    if not migrations_api.is_at_head(engine):
        engine.dispose()
        _fail(SCHEMA_BEHIND_MESSAGE)
    return engine


def _cipher(settings: Settings) -> AddressCipher | None:
    """Return the address cipher when ``FL_DATA_KEY`` is configured."""
    return None if settings.data_key is None else AddressCipher(settings.data_key)


@cli.group()
def registry() -> None:
    """Manage the household registry (people, places, vehicles, calendars)."""


@registry.command("load")
@click.argument("path", required=False, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Show the plan; write nothing.")
@click.option(
    "--allow-repo-path",
    is_flag=True,
    help="Permit a seed inside a git working tree (fictional examples only).",
)
@click.option(
    "--actor", default="registry-seed", show_default=True, help="Audit actor."
)
def registry_load(
    path: Path | None, dry_run: bool, allow_repo_path: bool, actor: str
) -> None:
    """Validate the registry seed and apply it in one transaction.

    PATH defaults to FL_REGISTRY_SEED. Keep the real seed outside the
    repository; it holds names and (encrypted on load) addresses.
    """
    settings = _settings()
    seed_path = path or settings.registry_seed
    if seed_path is None:
        _fail("no registry seed: pass PATH or set FL_REGISTRY_SEED")
    engine = _engine_at_head(settings)
    try:
        seed = load_seed_file(seed_path, allow_repo_path=allow_repo_path)
        plan = apply_seed(
            engine,
            seed,
            cipher=_cipher(settings),
            audit_key=settings.audit_key,
            audit_key_version=settings.audit_key_version,
            actor=actor,
            dry_run=dry_run,
            today=datetime.now(ZoneInfo(settings.timezone)).date(),
        )
    except (
        SeedError,
        RegistryError,
        ConfigurationError,
        ValidationError,
        ProtectedDataError,
    ) as exc:
        _fail(str(exc))
    finally:
        engine.dispose()
    summary = ", ".join(f"{name} {count}" for name, count in plan.counts().items())
    click.echo(("Dry run: " if dry_run else "Applied: ") + summary)
    for change in plan.changes:
        click.echo(f"  {change.action:<10} {change.object_ref}")


def _row(*columns: str) -> None:
    """Print one indented listing row."""
    click.echo("  " + " ".join(columns))


def _yes(flag: bool) -> str:
    """Render a flag as yes or no."""
    return "yes" if flag else "no"


def _print_locations(
    locations: Sequence[Location], presence: Mapping[str, tuple[bool, bool]]
) -> None:
    """Print the locations section; addresses and coordinates only as flags."""
    click.echo(f"Locations ({len(locations)}):")
    for x in locations:
        has_address, has_coordinates = presence.get(x.location_id, (False, False))
        _row(
            f"{x.location_id:<16}",
            f"{x.name:<24}",
            f"verified={_yes(x.verified)}",
            f"address={'stored' if has_address else 'none'}",
            f"coordinates={'stored' if has_coordinates else 'none'}",
        )


def _print_vehicles(vehicles: Sequence[Vehicle]) -> None:
    """Print the vehicles section."""
    click.echo(f"Vehicles ({len(vehicles)}):")
    for v in vehicles:
        seats = "unverified" if v.seat_capacity is None else str(v.seat_capacity)
        _row(f"{v.vehicle_id:<16}", f"{v.display_name:<24}", f"seats={seats}")


def _print_calendars(calendars: Sequence[CalendarSource]) -> None:
    """Print the calendars section."""
    click.echo(f"Calendars ({len(calendars)}):")
    for c in calendars:
        last = c.last_success.isoformat() if c.last_success else "never"
        _row(
            f"{c.calendar_id:<16}",
            f"{c.kind.value:<10}",
            f"owner={c.person_id or '-'}",
            f"enabled={_yes(c.enabled)}",
            f"last_success={last}",
        )


@registry.command("show")
def registry_show() -> None:
    """List the registry. Addresses and coordinates are never printed."""
    settings = _settings()
    engine = _engine_at_head(settings)
    try:
        with engine.connect() as connection:
            store = RegistryStore(connection)
            people = store.people()
            passengers = store.external_passengers()
            locations = store.locations()
            presence = store.protected_presence()
            vehicles = store.vehicles()
            calendars = store.calendar_sources()
            defaults = store.transport_defaults()
    finally:
        engine.dispose()
    click.echo(f"People ({len(people)}):")
    for p in people:
        _row(
            f"{p.person_id:<16}",
            f"{p.display_name:<16}",
            f"{p.role.value:<8}",
            f"active={_yes(p.active)}",
            f"driver={_yes(p.can_drive)}",
            f"monitored={_yes(p.monitoring_enabled)}",
        )
    click.echo(f"External passengers ({len(passengers)}):")
    for e in passengers:
        _row(
            f"{e.passenger_id:<16}",
            f"{e.display_first_name:<16}",
            f"household={e.household_id}",
            f"active={_yes(e.active)}",
        )
    _print_locations(locations, presence)
    _print_vehicles(vehicles)
    _print_calendars(calendars)
    per_person: dict[str, int] = {}
    for t in defaults:
        per_person[t.person_id] = per_person.get(t.person_id, 0) + 1
    click.echo(f"Transport defaults ({len(defaults)}):")
    for pid, count in sorted(per_person.items()):
        click.echo(f"  {pid:<16} {count}")


def _credential_problems(settings: Settings) -> list[str]:
    """Return what prevents talking to CalDAV (no secret values)."""
    if not settings.caldav_username or settings.caldav_app_password is None:
        return ["FL_CALDAV_USERNAME and FL_CALDAV_APP_PASSWORD are required"]
    return []


@cli.group(invoke_without_command=True)
@click.option(
    "--once",
    is_flag=True,
    help="Collect once and exit (the scheduler collects on its own interval).",
)
@click.option(
    "--calendar",
    "calendars",
    multiple=True,
    help="Restrict to this calendar (display name or ID); repeatable.",
)
@click.pass_context
def collect(ctx: click.Context, once: bool, calendars: tuple[str, ...]) -> None:
    """Read the configured iCloud calendars and store their occurrences.

    Exit status is 0 when every calendar was read completely, 1 when any was
    incomplete (DATA-011), and 2 for configuration problems. Prints calendar
    IDs and counts only.
    """
    if ctx.invoked_subcommand is not None:
        return
    if not once:
        _fail("Use `collect --once` or `collect discover` (scheduling arrives in M3).")
    settings = _settings()
    _configure_logging(settings)
    problems = _credential_problems(settings) + directory_problems(
        settings.database_path
    )
    if problems:
        _fail("; ".join(problems))
    from family_logistics.services import wiring  # noqa: PLC0415

    engine = create_sqlite_engine(settings.database_path)
    try:
        if not migrations_api.is_at_head(engine):
            _fail(SCHEMA_BEHIND_MESSAGE)
        collector, http = wiring.build_collector(settings, engine)
        try:
            run = collector.run_once(only=calendars or None)
        finally:
            http.close()
    finally:
        engine.dispose()
    if run.discovery_error:
        click.echo(f"Discovery failed: {run.discovery_error}")
    for item in run.calendars:
        result = item.result
        state = "complete" if result.complete else "INCOMPLETE"
        applied = item.applied
        changes = f"+{applied.inserted} ~{applied.updated} -{applied.deleted}"
        detail = "reused" if item.reused else changes
        label = f"{result.source.calendar_id} {result.source.kind.value}"
        count = len(result.events)
        click.echo(f"{label}: {count} occurrences, {state} ({detail})")
    click.echo(f"DATA-011 findings: {len(run.findings)}")
    sys.exit(0 if run.complete else 1)


def _yes_no(value: bool | None) -> str:
    """Render an optional flag for the discovery table."""
    if value is None:
        return "unknown"
    return "yes" if value else "no"


@collect.command("discover")
def collect_discover() -> None:
    """List the account's calendars: ID, events, shared, writable, name.

    Copy the IDs of the calendars to collect into the private registry seed.
    URLs and account identifiers are never printed. A calendar shown as
    writable means the collector account holds write access: share it View
    Only instead.
    """
    settings = _settings()
    problems = _credential_problems(settings)
    if problems:
        _fail("; ".join(problems))
    from family_logistics.adapters.caldav.client import CalDAVError  # noqa: PLC0415
    from family_logistics.adapters.caldav.ids import calendar_id_for_href  # noqa: PLC0415
    from family_logistics.services import wiring  # noqa: PLC0415

    client, http = wiring.build_caldav_client(settings)
    try:
        calendars = client.discover()
    except CalDAVError as exc:
        click.echo(f"Discovery failed: {exc}", err=True)
        sys.exit(1)
    finally:
        http.close()
    click.echo("calendar_id\tevents\tshared\twritable\tname")
    for info in calendars:
        cells = (
            calendar_id_for_href(info.href),
            _yes_no(info.supports_events),
            _yes_no(info.shared),
            _yes_no(info.writable),
            info.display_name,
        )
        click.echo("\t".join(cells))


if __name__ == "__main__":
    cli()
