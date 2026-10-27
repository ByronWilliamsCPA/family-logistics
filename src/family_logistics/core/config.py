"""Configuration settings for Family Logistics.

Settings are loaded from environment variables with the prefix ``FL_`` (for
example ``FL_TIMEZONE``). Secrets are typed as ``SecretStr`` so they never
appear in ``repr()`` output or structured logs, and validation errors never
echo the rejected input (``hide_input_in_errors``); ``load_settings`` turns a
validation failure into a ``ConfigurationError`` that names fields only.

#CRITICAL: Security: secrets (CalDAV app password, HA token, routing and LLM
keys) arrive through the process environment, injected by the deployment's
secret store. A ``.env`` file is a local-development convenience only; it is
gitignored and excluded from the image, and nothing here reads a secret from a
file in the image or from Git.
#VERIFY: tests/unit/test_config_env.py::TestSecretsNeverShipped asserts that
``.env`` is listed in ``.gitignore`` and ``.dockerignore``; the process
environment always overrides ``.env`` (same module).
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.fernet import Fernet
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    SettingsConfigDict,
)

from family_logistics.core.exceptions import ConfigurationError
from family_logistics.core.urls import (
    HTTP_OR_HTTPS,
    HTTPS_ONLY,
    host_allowed,
    host_allowlist_problem,
    normalize_host,
    url_problem,
)

if TYPE_CHECKING:
    from pydantic import ValidationInfo

HA_TOKEN_MIN_LENGTH = 32
"""Minimum length of the bearer token Home Assistant presents (ADR-0007)."""

HA_TOKEN_CHARSET = re.compile(r"[\x21-\x7e]+")
"""Printable ASCII without spaces: the token travels in an HTTP header that the
server decodes as Latin-1, so anything else could never match."""

DEDICATED_TAG_PATTERN = re.compile(r"fl-[a-z0-9]+(?:-[a-z0-9]+)*")
"""Shape of a dedicated Apprise tag: ``fl-`` plus lower-case words joined by
hyphens. No comma, space, slash, dot or query character can appear, so a tag
can never change how the Apprise API reads the tag selector or the URL."""

MAX_TAG_LENGTH = 64

APPRISE_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
"""Shape of an Apprise API configuration key (the ``{KEY}`` URL segment)."""

AUDIT_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}=")
"""Shape of ``FL_AUDIT_KEY``: 32 bytes as url-safe base64 with padding (44
characters). Anything else, including the standard-alphabet ``+`` and ``/``,
whitespace and non-ASCII text, is refused before decoding."""

AUDIT_KEY_BYTES = 32
"""Length of the decoded audit HMAC key."""


def decode_audit_key(text: str) -> bytes:
    """Decode an audit key, accepting only its canonical 44-character form.

    Args:
        text: The key as configured (url-safe base64 of exactly 32 bytes).

    Returns:
        The 32 raw key bytes.

    Raises:
        ValueError: If the text is not the canonical encoding of 32 bytes. The
            message is fixed and never echoes the text.
    """
    msg = "audit key must be 32 random bytes as url-safe base64 (44 characters)"
    if AUDIT_KEY_PATTERN.fullmatch(text) is None:
        raise ValueError(msg)
    raw = base64.urlsafe_b64decode(text)
    # Re-encoding rejects a non-canonical final character (spare padding bits),
    # so one key has exactly one accepted spelling.
    if len(raw) != AUDIT_KEY_BYTES or base64.urlsafe_b64encode(raw).decode() != text:
        raise ValueError(msg)
    return raw


DEDICATED_TAG_FIELDS: tuple[str, ...] = (
    "apprise_tag_report",
    "apprise_tag_status",
    "apprise_tag_warning",
    "apprise_tag_urgent",
    "apprise_tag_ops",
    "apprise_tag_shadow",
)
"""The settings that name the application's own Apprise tags."""


def is_dedicated_tag(tag: str) -> bool:
    """Return whether ``tag`` is a well-formed dedicated ``fl-*`` tag.

    This is the single definition of an allowed tag. ``Settings`` applies it to
    every ``FL_APPRISE_TAG_*`` value and the Apprise client applies it again at
    send time, so neither a settings value nor a caller can name a shared
    severity tag such as ``all`` or ``urgent``.

    Args:
        tag: Candidate tag.

    Returns:
        True when the tag matches ``fl-<word>[-<word>...]`` in lower case and
        is at most 64 characters.
    """
    return (
        len(tag) <= MAX_TAG_LENGTH and DEDICATED_TAG_PATTERN.fullmatch(tag) is not None
    )


RouteProviderName = Literal["ha_relay", "google_routes", "static"]

CommaList = Annotated[list[str], NoDecode]
"""A list read from a comma-separated environment value (not JSON)."""

DEFAULT_PROXY_TRUSTED_CIDRS: tuple[str, ...] = ()
"""No proxy is trusted by default: identity headers are ignored until the
deployment sets ``FL_PROXY_TRUSTED_CIDRS`` to its reverse proxy's address."""


HA_CALENDAR_ENTITY_PATTERN = r"^calendar\.[a-z0-9_]{1,200}$"
"""A Home Assistant calendar entity ID. Only the ``calendar`` domain matches,
so a person tracker, device tracker or sensor can never be configured as a
school calendar (privacy spec, HA snapshot row)."""

REGISTRY_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
"""Shape of a registry person or location ID in ``FL_HA_SCHOOLS``."""

MAX_HA_SCHOOLS = 8
MAX_SCHOOL_PEOPLE = 16
MAX_SCHOOL_CALENDARS = 8

CalendarEntityId = Annotated[str, StringConstraints(pattern=HA_CALENDAR_ENTITY_PATTERN)]
RegistryId = Annotated[str, StringConstraints(pattern=REGISTRY_ID_PATTERN)]


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing a key that appears twice.

    ``json.loads`` keeps only the last of two equal keys, which would silently
    drop an earlier ``person_ids`` list from ``FL_HA_SCHOOLS``.

    Args:
        pairs: The key and value pairs of one JSON object, in document order.

    Returns:
        The object as a dict.

    Raises:
        ValueError: If a key repeats. The message never echoes the key.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = "FL_HA_SCHOOLS repeats a key within one JSON object"
            raise ValueError(msg)
        result[key] = value
    return result


class HaSchool(BaseModel):
    """One school whose schedule Home Assistant delivers (``FL_HA_SCHOOLS``).

    Bell calendars carry the school's timed bell schedule (the only source of
    start and end times); district calendars carry closures and short days
    (classification only). A district calendar may serve several schools.

    Attributes:
        model_config: Unknown keys are refused; instances are immutable.
        location_id: Registry location of the school.
        person_ids: Registry people who attend it (each in one school only).
        bell_calendars: Calendar entity IDs with the bell schedule.
        district_calendars: Calendar entity IDs with closures and short days.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    location_id: RegistryId
    person_ids: tuple[RegistryId, ...] = Field(
        min_length=1, max_length=MAX_SCHOOL_PEOPLE
    )
    bell_calendars: tuple[CalendarEntityId, ...] = Field(
        default=(), max_length=MAX_SCHOOL_CALENDARS
    )
    district_calendars: tuple[CalendarEntityId, ...] = Field(
        default=(), max_length=MAX_SCHOOL_CALENDARS
    )

    @model_validator(mode="after")
    def _calendars_are_coherent(self) -> Self:
        """Require a calendar, one role per calendar and distinct people.

        Returns:
            The validated school.

        Raises:
            ValueError: If the school has no calendar, a calendar is listed
                twice or in both roles, or a person is listed twice. The
                message never echoes a value.
        """
        calendars = [*self.bell_calendars, *self.district_calendars]
        if not calendars:
            msg = "a school needs at least one bell or district calendar"
            raise ValueError(msg)
        if len(set(calendars)) != len(calendars):
            msg = "a calendar is listed twice, or as both bell and district"
            raise ValueError(msg)
        if len(set(self.person_ids)) != len(self.person_ids):
            msg = "a person is listed twice for one school"
            raise ValueError(msg)
        return self

    def entities(self) -> dict[str, str]:
        """Return every calendar entity of the school with its role.

        Returns:
            Entity ID to ``"bell"`` or ``"district"``.
        """
        roles = dict.fromkeys(self.bell_calendars, "bell")
        roles.update(dict.fromkeys(self.district_calendars, "district"))
        return roles


CONSUMED = "consumed"
M1, M2, M3, M4, M5 = "M1", "M2", "M3", "M4", "M5"

FIELD_STATUS: dict[str, str] = {
    "log_level": CONSUMED,
    "json_logs": CONSUMED,
    "include_timestamp": CONSUMED,
    "bind_host": CONSUMED,
    "port": CONSUMED,
    "timezone": CONSUMED,
    "database_path": CONSUMED,
    "registry_seed": CONSUMED,
    "caldav_url": CONSUMED,
    "caldav_username": CONSUMED,
    "caldav_app_password": CONSUMED,
    "caldav_calendars": CONSUMED,
    "caldav_allowed_hosts": CONSUMED,
    "caldav_transport_calendar": CONSUMED,
    "collect_days_back": CONSUMED,
    "collect_days_ahead": CONSUMED,
    "source_max_staleness_minutes": CONSUMED,
    "ha_token": CONSUMED,
    "ha_schools": CONSUMED,
    "apprise_url": CONSUMED,
    "apprise_config_key": CONSUMED,
    "apprise_tag_report": CONSUMED,
    "apprise_tag_status": CONSUMED,
    "apprise_tag_warning": CONSUMED,
    "apprise_tag_urgent": CONSUMED,
    "apprise_tag_ops": CONSUMED,
    "apprise_tag_shadow": CONSUMED,
    "proxy_trusted_cidrs": M4,
    "ui_allowed_users": M4,
    "data_key": CONSUMED,
    "audit_key": CONSUMED,
    "audit_key_version": CONSUMED,
    "route_provider": CONSUMED,
    "google_routes_api_key": M3,
    "routes_monthly_budget_usd": M3,
    "routes_budget_warn_usd": M3,
    "llm_enabled": M5,
    "llm_base_url": M5,
    "llm_api_key": M5,
    "llm_model": M5,
    "weekly_cron": CONSUMED,
    "daily_cron": CONSUMED,
    "change_scan_minutes": CONSUMED,
    "scheduler_enabled": CONSUMED,
    "on_demand_enabled": CONSUMED,
    "notification_mode": CONSUMED,
    "quiet_hours_start": CONSUMED,
    "quiet_hours_end": CONSUMED,
    "imminent_window_minutes": CONSUMED,
    "same_location_gap_minutes": CONSUMED,
    "overnight_gap_hours": CONSUMED,
    "travel_buffer_minutes": CONSUMED,
    "stop_buffer_minutes": CONSUMED,
}
"""Whether each setting is read by running code or waits for a milestone.

``consumed`` means code under ``src/`` reads the field from a settings object
today (a startup check counts; an adapter nobody calls yet does not).
Settings marked with a milestone are defined ahead of the code that reads them
so the deployment contract is stable. ``tests/unit/test_config_fields.py``
fails when a field is added without an entry here, when an entry names a field
that no longer exists, and when a ``consumed`` field is never read, so no
setting can become silently dead.
"""


class Settings(BaseSettings):
    """Application settings, loaded from ``FL_``-prefixed environment variables.

    Every field has a safe default so the settings object can be built in tests
    and for one-shot CLI commands. Commands that need a credential (for example
    ``serve`` needs ``ha_token``) check for it explicitly and refuse to start.

    Attributes:
        model_config: pydantic-settings configuration (``FL_`` prefix).
        log_level: Logging level for the application.
        json_logs: Emit JSON formatted logs.
        include_timestamp: Include timestamps in logs.
        bind_host: Listen address; the image and compose set ``0.0.0.0``.
        port: Listen port.
        timezone: IANA zone used for all human-facing times; storage is UTC.
        database_path: SQLite database file (WAL mode, single writer).
        registry_seed: Private registry seed YAML, kept outside the repository;
            ``registry load`` reads it when no path is given.
        caldav_url: iCloud CalDAV base URL.
        caldav_username: Apple ID used by the read-only collector.
        caldav_app_password: App-specific password for that Apple ID.
        caldav_calendars: Calendars to collect, by display name or calendar
            ID (comma-separated). Discovered calendars not listed are ignored.
        caldav_allowed_hosts: Registrable domains the CalDAV collector may reach,
            for the first request and every redirect hop (default ``icloud.com``;
            a host matches the domain itself or any subdomain). Closes
            redirect-to-LAN request forgery. ``caldav_url`` must be inside it.
        caldav_transport_calendar: Display name of the shared transportation
            calendar; it is registered with the TRANSPORT kind.
        collect_days_back: Local days before today included in a collection.
        collect_days_ahead: Local days after today included in a collection.
        source_max_staleness_minutes: A calendar whose last complete
            collection is older than this raises DATA-011 (incomplete).
        ha_token: Bearer token Home Assistant presents to the HA-only routes.
        ha_schools: Schools whose schedules Home Assistant pushes (JSON list
            of ``HaSchool``): each maps calendar entities to a registry
            location and the people who attend it. Only these calendars are
            accepted by the snapshot ingest; empty, the default, accepts none.
        apprise_url: Apprise API base URL (http or https, no embedded credentials).
        apprise_config_key: Key of the saved Apprise configuration that holds the
            app's notification services; sent as ``POST /notify/{key}``.
        apprise_tag_report: Weekly run summary.
        apprise_tag_status: Completed-assessment short push.
        apprise_tag_warning: Non-urgent findings.
        apprise_tag_urgent: Critical or unverifiable imminent ride; one quiet-hours
            push per episode.
        apprise_tag_ops: Service-health alerts (meant for Morgan only; the
            recipients are set in the Apprise configuration, not enforced here).
        apprise_tag_shadow: Shadow-mode output (meant for Morgan only; the
            recipients are set in the Apprise configuration, not enforced here).
        proxy_trusted_cidrs: TCP peers whose Authentik identity headers are
            believed (the reverse proxy only); empty, the default, trusts none.
        ui_allowed_users: Authentik usernames allowed into the registry UI;
            empty denies everyone.
        data_key: Fernet key encrypting protected addresses at rest; required
            before any protected address is stored.
        audit_key: HMAC-SHA256 key for the audit-log state hashes (32 random
            bytes as url-safe base64, 44 characters); required before any audit
            row is written and in production mode. Must differ from ``data_key``.
        audit_key_version: Version recorded with every audit row so a future key
            rotation can tell which key produced a hash (at least 1).
        route_provider: Which RouteProvider implementation to use.
        google_routes_api_key: API key for Google Routes (optional).
        routes_monthly_budget_usd: Hard monthly cap for paid routing; at the cap
            discretionary calls stop and feasibility without a recent estimate
            is reported incomplete.
        routes_budget_warn_usd: Spend at which Morgan gets a budget warning.
        llm_enabled: Whether the narrative LLM call is attempted (M5).
        llm_base_url: OpenAI-compatible base URL of the local inference server route.
        llm_api_key: Bearer key for the inference route.
        llm_model: Model identifier, discovered during commissioning.
        weekly_cron: Cron expression for the weekly review (local time).
        daily_cron: Cron expression for the daily review (local time).
        change_scan_minutes: Interval of the incremental change scan. When the
            scheduler is enabled it must exceed the notification claim lifetime
            (15 minutes; ``services.scheduler.scheduler_problems``).
        scheduler_enabled: Run the in-process scheduler inside ``serve``
            (collect, assess and notify on the scan interval, the daily and
            weekly jobs and the retention purges). Off by default: the M3
            blockers in docs/project/roadmap.md must close before a scheduler
            becomes the first production caller of ``AssessmentService.run``.
        on_demand_enabled: Let ``POST /api/v1/assessments`` start on-demand
            runs. Off by default, for the same reason as the scheduler: it makes
            the process a production caller of ``AssessmentService.run`` before
            the M3 blockers (issues #19 to #23) close. ``GET /api/v1/status``
            and ``GET /api/v1/assessments/{run_id}`` are read-only and stay
            available either way.
        notification_mode: ``shadow`` (default) is the safe mode: every push,
            ops alerts included, goes to the shadow tag only (ADR 0008, D1).
            ``production`` is a deliberate operator decision taken jointly
            after the M3 shadow gate (docs/project/roadmap.md). Recipient
            routing is configured in Apprise; the scheduler's dispatch reads
            this value.
        quiet_hours_start: Start of quiet hours (local time). Only an urgent
            push bypasses them, once per episode; CAP-014 never does.
        quiet_hours_end: End of quiet hours (local time).
        imminent_window_minutes: A ride is imminent from this long before it
            starts until it ends; its critical or unverifiable findings are
            urgent (so they may bypass quiet hours, once per episode) and an
            unverified destination on it is critical.
        same_location_gap_minutes: Largest idle gap between two activities at
            one place that still counts as a single stay (no ride needed).
        overnight_gap_hours: Largest idle gap between stays at one place on
            consecutive local days that still counts as staying overnight (no
            return home and no morning trip).
        travel_buffer_minutes: Arrive this long before a passenger's next
            activity.
        stop_buffer_minutes: Loading or unloading time per ride stop.
    """

    model_config = SettingsConfigDict(
        env_prefix="FL_",
        # Local development only: a ``.env`` in the working directory is read
        # after the process environment, which always wins. Production values
        # come from the deployment's secret store through the environment.
        env_file=".env",
        env_file_encoding="utf-8",
        # A blank ``FL_X=`` (a template line, a compose default) means unset,
        # so an empty optional secret fails closed with a clear message
        # instead of a validation error.
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
        # Settings are shared process-wide through ``get_settings``; freezing
        # them keeps a late assignment from bypassing every validator.
        frozen=True,
        # #CRITICAL: Security: a rejected secret (short HA token, malformed
        # data key) must never be echoed in an error or traceback.
        # #VERIFY: tests/unit/test_config_security.py::TestNoSecretEcho feeds
        # real rejected secrets through Settings, load_settings and the CLI.
        hide_input_in_errors=True,
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_logs: bool = False
    include_timestamp: bool = True

    bind_host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    timezone: str = "America/Los_Angeles"
    database_path: Path = Path("/data/family_logistics.db")
    registry_seed: Path | None = None

    caldav_url: str = "https://caldav.icloud.com"
    caldav_username: str | None = None
    caldav_app_password: SecretStr | None = None
    caldav_calendars: str = ""
    caldav_allowed_hosts: CommaList = Field(default_factory=lambda: ["icloud.com"])
    caldav_transport_calendar: str = "Family Transportation"
    collect_days_back: int = Field(default=2, ge=0, le=31)
    collect_days_ahead: int = Field(default=14, ge=1, le=62)
    source_max_staleness_minutes: int = Field(default=120, ge=15)

    ha_token: SecretStr | None = None
    ha_schools: Annotated[tuple[HaSchool, ...], NoDecode] = Field(
        default=(), max_length=MAX_HA_SCHOOLS
    )

    apprise_url: str | None = None
    # The saved Apprise configuration the app posts to (``/notify/{KEY}``).
    apprise_config_key: str = "family-logistics"
    # Dedicated tags, sent in the request body to select services within that
    # configuration; who receives each tag is configured in Apprise, not here.
    apprise_tag_report: str = "fl-report"
    apprise_tag_status: str = "fl-status"
    apprise_tag_warning: str = "fl-warning"
    apprise_tag_urgent: str = "fl-urgent"
    apprise_tag_ops: str = "fl-ops"
    apprise_tag_shadow: str = "fl-shadow"

    proxy_trusted_cidrs: CommaList = Field(
        default_factory=lambda: list(DEFAULT_PROXY_TRUSTED_CIDRS)
    )
    ui_allowed_users: CommaList = Field(default_factory=list[str])

    data_key: SecretStr | None = None
    audit_key: SecretStr | None = None
    audit_key_version: int = Field(default=1, ge=1)

    route_provider: RouteProviderName = "ha_relay"
    google_routes_api_key: SecretStr | None = None
    routes_monthly_budget_usd: float = Field(default=10.0, ge=0)
    routes_budget_warn_usd: float = Field(default=8.0, ge=0)

    llm_enabled: bool = False
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None

    weekly_cron: str = "0 15 * * SUN"
    daily_cron: str = "0 19 * * *"
    change_scan_minutes: int = Field(default=60, ge=5)
    # #CRITICAL: Safety: the scheduler is off by default. Enabling it makes the
    # process the first production caller of ``AssessmentService.run`` and of
    # the notification service (shadow mode unless production is chosen).
    # #VERIFY: tests/unit/test_config_validation.py asserts the default is
    # False; the roadmap's M3 blockers close before it is switched on.
    scheduler_enabled: bool = False
    # #CRITICAL: Safety: on-demand runs are off by default. Enabling them makes
    # the process a production caller of ``AssessmentService.run`` (through
    # ``POST /api/v1/assessments``) before issues #19 to #23 are closed, exactly
    # as the scheduler does. A disabled POST starts nothing and allocates no id.
    # #VERIFY: tests/unit/test_config_validation.py asserts the default is
    # False; tests/unit/test_assessments_api.py (TestDisabled) pins the 503.
    on_demand_enabled: bool = False

    # #CRITICAL: Safety: shadow mode is the default. Switching to production is
    # a process decision (both parents, after the M3 shadow gate in
    # docs/project/roadmap.md); the code does not enforce that approval.
    # #VERIFY: tests/unit/test_config_validation.py::TestDefaults asserts the
    # default is shadow; review the mode change in the deployment repository.
    notification_mode: Literal["shadow", "production"] = "shadow"
    quiet_hours_start: time = time(22, 0)
    quiet_hours_end: time = time(6, 0)
    imminent_window_minutes: int = Field(default=180, ge=0)

    # #ASSUME: Timing: proposed defaults pending owner approval (defaults
    # register); tuned during the shadow period.
    same_location_gap_minutes: int = Field(default=60, ge=0)
    overnight_gap_hours: int = Field(default=14, ge=0)
    travel_buffer_minutes: int = Field(default=10, ge=0)
    stop_buffer_minutes: int = Field(default=5, ge=0)

    @field_validator("ha_token", mode="before")
    @classmethod
    def _strip_ha_token(cls, value: object) -> object:
        """Drop surrounding whitespace, such as a secret file's trailing newline.

        Args:
            value: Raw value from the environment or a constructor argument.

        Returns:
            The stripped string for string input; other values unchanged.
        """
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("ha_token")
    @classmethod
    def _ha_token_is_usable(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject a configured HA token that is too short or not header-safe.

        Args:
            value: The configured token, or None when unset.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If a token is set but is shorter than 32 characters or
                contains anything other than printable ASCII.
        """
        if value is None:
            return value
        token = value.get_secret_value()
        if len(token) < HA_TOKEN_MIN_LENGTH:
            msg = f"FL_HA_TOKEN must be at least {HA_TOKEN_MIN_LENGTH} characters"
            raise ValueError(msg)
        if HA_TOKEN_CHARSET.fullmatch(token) is None:
            msg = "FL_HA_TOKEN must be printable ASCII without spaces"
            raise ValueError(msg)
        return value

    @field_validator("caldav_url")
    @classmethod
    def _caldav_url_is_https(cls, value: str) -> str:
        """Require an HTTPS CalDAV endpoint with a host and no credentials.

        Args:
            value: The configured base URL.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the URL is not an https URL with a host, or embeds
                credentials.
        """
        # #CRITICAL: Security: Basic credentials (the app-specific password)
        # travel with every CalDAV request; plain HTTP would expose them, and
        # ``https://a@b`` would send them to ``b``.
        # #VERIFY: tests/unit/test_config_validation.py::TestCaldavUrl; the
        # transport applies the same rule per request (test_readonly_transport).
        problem = url_problem(value, HTTPS_ONLY)
        if problem is not None:
            msg = f"FL_CALDAV_URL {problem}"
            raise ValueError(msg)
        return value

    @field_validator("apprise_url", "llm_base_url")
    @classmethod
    def _service_url_is_usable(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        """Require an http(s) service URL with a host and no credentials.

        Args:
            value: The configured URL, or None when unset.
            info: Validation context (names the field).

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the URL is unusable; the message never echoes it.
        """
        if value is None:
            return value
        problem = url_problem(value, HTTP_OR_HTTPS)
        if problem is not None:
            msg = f"FL_{str(info.field_name).upper()} {problem}"
            raise ValueError(msg)
        return value

    @field_validator(*DEDICATED_TAG_FIELDS)
    @classmethod
    def _tag_is_dedicated(cls, value: str, info: ValidationInfo) -> str:
        """Require every Apprise tag to be a dedicated ``fl-*`` tag.

        Args:
            value: The configured tag.
            info: Validation context (names the field).

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the tag is not ``fl-`` plus lower-case words; the
                message never echoes the value.
        """
        # #CRITICAL: Security: only dedicated tags may ever be used, so a shared
        # severity tag (``all``, ``urgent``) can never reach other recipients.
        # #VERIFY: tests/unit/test_config_validation.py::TestDedicatedTags
        # rejects non-dedicated values from constructor and environment; the
        # Apprise client re-checks at send time (test_apprise_client.py).
        if not is_dedicated_tag(value):
            msg = (
                f"FL_{str(info.field_name).upper()} must be fl-<word>[-<word>] "
                "in lower case (letters and digits, at most 64 characters)"
            )
            raise ValueError(msg)
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        """Fail at startup, not at first use, for an unknown IANA zone.

        Args:
            value: The configured zone name.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the zone is not in the IANA database.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            msg = "FL_TIMEZONE must be an IANA zone name such as America/Los_Angeles"
            raise ValueError(msg) from exc
        return value

    @field_validator("apprise_config_key")
    @classmethod
    def _config_key_is_valid(cls, value: str) -> str:
        """Require an Apprise configuration key that is safe in a URL path.

        Args:
            value: The configured key.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the key is not 1 to 64 letters, digits, ``_`` or ``-``.
        """
        if APPRISE_KEY_PATTERN.fullmatch(value) is None:
            msg = "FL_APPRISE_CONFIG_KEY must be 1 to 64 letters, digits, _ or -"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _budget_and_quiet_hours_are_coherent(self) -> Self:
        """Reject a warning above the hard cap and an unusable quiet-hours window.

        Returns:
            The validated settings.

        Raises:
            ValueError: If the budget warning exceeds the monthly cap, a quiet
                hours time carries a UTC offset, or quiet hours start and end
                at the same time.
        """
        if self.routes_budget_warn_usd > self.routes_monthly_budget_usd:
            msg = (
                "FL_ROUTES_BUDGET_WARN_USD must not exceed FL_ROUTES_MONTHLY_BUDGET_USD"
            )
            raise ValueError(msg)
        if self.quiet_hours_start.tzinfo is not None or (
            self.quiet_hours_end.tzinfo is not None
        ):
            msg = (
                "FL_QUIET_HOURS_START and FL_QUIET_HOURS_END are local wall "
                "times and must not carry a UTC offset"
            )
            raise ValueError(msg)
        if self.quiet_hours_start == self.quiet_hours_end:
            msg = "FL_QUIET_HOURS_START and FL_QUIET_HOURS_END must differ"
            raise ValueError(msg)
        return self

    @field_validator("ha_schools", mode="before")
    @classmethod
    def _parse_ha_schools_json(cls, value: object) -> object:
        """Parse the JSON text of ``FL_HA_SCHOOLS``, refusing repeated keys.

        The field is ``NoDecode``, so the raw text arrives here instead of
        being decoded by pydantic-settings (which keeps the last of two equal
        keys and words its errors in a way that can quote the text).

        Args:
            value: Raw value from the environment or a constructor argument.

        Returns:
            The decoded list for string input (an empty one for blank text);
            other values unchanged for validation.

        Raises:
            ValueError: If the text is not JSON or repeats an object key. The
                message never echoes the text, and the decoder error is not
                chained because it can quote it.
        """
        if not isinstance(value, str):
            return value
        if not value.strip():
            return ()
        # #CRITICAL: Security: the text can hold household calendar entity IDs
        # and child identifiers, so no error built here may quote it.
        # #VERIFY: tests/unit/test_config_ha_schools.py asserts that a malformed
        # value and a repeated key are rejected without echoing the text.
        try:
            decoded = cast(
                "object",
                json.loads(value, object_pairs_hook=_reject_duplicate_json_keys),
            )
        except json.JSONDecodeError:
            msg = "FL_HA_SCHOOLS value is not valid JSON"
            raise ValueError(msg) from None
        return decoded

    @model_validator(mode="after")
    def _ha_schools_are_distinct(self) -> Self:
        """Require one entry per school, one school per person, own bells.

        A district calendar may serve several schools; a bell calendar (the
        only source of start and end times) belongs to one school and cannot
        double as another school's district calendar.

        Returns:
            The validated settings.

        Raises:
            ValueError: If two entries name the same location, one person is
                listed under two schools (a person has one school day per
                date), or a bell calendar is shared with another school. The
                message never echoes a value.
        """
        locations = [school.location_id for school in self.ha_schools]
        if len(set(locations)) != len(locations):
            msg = "FL_HA_SCHOOLS lists one location twice"
            raise ValueError(msg)
        people = [pid for school in self.ha_schools for pid in school.person_ids]
        if len(set(people)) != len(people):
            msg = "FL_HA_SCHOOLS lists one person under two schools"
            raise ValueError(msg)
        bells = [cal for school in self.ha_schools for cal in school.bell_calendars]
        districts = {
            cal for school in self.ha_schools for cal in school.district_calendars
        }
        if len(set(bells)) != len(bells) or not districts.isdisjoint(bells):
            msg = "FL_HA_SCHOOLS shares a bell calendar between two schools"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _tags_are_distinct(self) -> Self:
        """Require six different tags so audiences can never be merged.

        Returns:
            The validated settings.

        Raises:
            ValueError: If two tag settings hold the same value.
        """
        tags = [getattr(self, name) for name in DEDICATED_TAG_FIELDS]
        if len(set(tags)) != len(tags):
            msg = "the FL_APPRISE_TAG_* values must be distinct"
            raise ValueError(msg)
        return self

    @field_validator("registry_seed", mode="before")
    @classmethod
    def _blank_seed_is_unset(cls, value: object) -> object:
        """Treat an empty ``FL_REGISTRY_SEED`` as unset, not the current directory.

        Args:
            value: Raw value from the environment or a constructor argument.

        Returns:
            None for blank text; other values unchanged for validation.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "proxy_trusted_cidrs", "ui_allowed_users", "caldav_allowed_hosts", mode="before"
    )
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        """Split a comma-separated string into trimmed, non-empty items.

        Args:
            value: Raw value from the environment or a constructor argument.

        Returns:
            A list for string input; other values unchanged for validation.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("proxy_trusted_cidrs")
    @classmethod
    def _valid_cidrs(cls, value: list[str]) -> list[str]:
        """Normalize every entry as an IP network.

        Args:
            value: CIDR strings.

        Returns:
            Normalized CIDR strings.

        Raises:
            ValueError: If an entry is not a valid IPv4/IPv6 network.
        """
        try:
            return [str(ipaddress.ip_network(item, strict=True)) for item in value]
        except ValueError as exc:
            # The ipaddress message quotes the rejected item; keep it out.
            msg = "FL_PROXY_TRUSTED_CIDRS has an entry that is not a valid network"
            raise ValueError(msg) from exc

    @field_validator("data_key")
    @classmethod
    def _data_key_is_fernet(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject a configured data key that is not a valid Fernet key.

        Args:
            value: The configured key, or None when unset.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the key is not 32 url-safe base64-encoded bytes.
        """
        if value is not None:
            try:
                Fernet(value.get_secret_value().encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                msg = "FL_DATA_KEY must be a Fernet key (32 url-safe base64 bytes)"
                raise ValueError(msg) from exc
        return value

    @field_validator("audit_key")
    @classmethod
    def _audit_key_is_32_bytes(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject a configured audit key that is not 32 bytes of url-safe base64.

        Args:
            value: The configured key, or None when unset.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the key is not the canonical 44-character url-safe
                base64 encoding of exactly 32 bytes. The message never echoes it.
        """
        if value is not None:
            try:
                decode_audit_key(value.get_secret_value())
            except ValueError as exc:
                msg = (
                    "FL_AUDIT_KEY must be 32 random bytes as url-safe base64 "
                    "(44 characters)"
                )
                raise ValueError(msg) from exc
        return value

    @model_validator(mode="after")
    def _audit_key_differs_from_data_key(self) -> Self:
        """Require separate keys so one leak cannot expose both uses.

        Returns:
            The validated settings.

        Raises:
            ValueError: If both keys are set to the same value.
        """
        if (
            self.audit_key is not None
            and self.data_key is not None
            and self.audit_key.get_secret_value() == self.data_key.get_secret_value()
        ):
            msg = "FL_AUDIT_KEY must differ from FL_DATA_KEY"
            raise ValueError(msg)
        return self

    @field_validator("caldav_allowed_hosts")
    @classmethod
    def _caldav_allowed_hosts_are_domains(cls, value: list[str]) -> list[str]:
        """Normalize the CalDAV host allowlist and refuse malformed entries.

        Args:
            value: Configured registrable domains.

        Returns:
            The entries, lower-cased and without a trailing dot.

        Raises:
            ValueError: If the list is empty or an entry is empty or carries a
                scheme, path, port, wildcard or anything else but a domain. The
                message never echoes an entry.
        """
        problem = host_allowlist_problem(value)
        if problem is not None:
            msg = f"FL_CALDAV_ALLOWED_HOSTS {problem}"
            raise ValueError(msg)
        return [normalize_host(entry) for entry in value]

    @model_validator(mode="after")
    def _caldav_url_host_is_allowed(self) -> Self:
        """Fail at startup when the CalDAV base URL is outside the allowlist.

        Returns:
            The validated settings.

        Raises:
            ValueError: If the host of ``caldav_url`` is not allowed.
        """
        host = httpx.URL(self.caldav_url).host
        if not host_allowed(host, self.caldav_allowed_hosts):
            msg = "FL_CALDAV_URL host is not covered by FL_CALDAV_ALLOWED_HOSTS"
            raise ValueError(msg)
        return self

    @property
    def calendar_list(self) -> list[str]:
        """Return the configured calendars as a list, ignoring blanks.

        Returns:
            Calendar hrefs or IDs in configuration order.
        """
        return [c.strip() for c in self.caldav_calendars.split(",") if c.strip()]


def load_settings() -> Settings:
    """Build settings from the environment without leaking rejected values.

    Returns:
        Settings built from the environment.

    Raises:
        ConfigurationError: If any setting is invalid. The message and details
            name the offending ``FL_`` variables and the reason only, never
            the value.
    """
    try:
        return Settings()
    except PydanticValidationError as exc:
        problems = [
            f"FL_{'_'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
    # Raised outside the handler so the pydantic error, which carries the
    # rejected input, is neither the cause nor the context of this one.
    msg = "Invalid configuration: " + "; ".join(problems)
    raise ConfigurationError(msg, details={"problems": problems})


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Invalid settings surface as the ``ConfigurationError`` that
    ``load_settings`` raises. A failure is not cached, so the next call builds
    settings again.

    Returns:
        Cached Settings built from the environment.
    """
    return load_settings()
