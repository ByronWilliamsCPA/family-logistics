"""Configuration settings for Family Logistics.

Settings are loaded from environment variables with the prefix ``FL_`` (for
example ``FL_TIMEZONE``). Secrets are typed as ``SecretStr`` so they never
appear in ``repr()`` output or structured logs, and validation errors never
echo the rejected input (``hide_input_in_errors``); ``load_settings`` turns a
validation failure into a ``ConfigurationError`` that names fields only.

#CRITICAL: Security: secrets (CalDAV app password, HA token, routing and LLM
keys) arrive only through the environment, injected by the deployment's secret
store. Nothing here reads a secret from a file in the image or from Git.
#VERIFY: model_config sets no env_file or secrets_dir (grep this module), and
the Dockerfile copies no secret; docs/guides/configuration.md says .env is
not read by the app.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from family_logistics.core.exceptions import ConfigurationError

HA_TOKEN_MIN_LENGTH = 32
"""Minimum length of the bearer token Home Assistant presents (ADR-0007)."""

RouteProviderName = Literal["ha_relay", "google_routes", "static"]

CommaList = Annotated[list[str], NoDecode]
"""A list read from a comma-separated environment value (not JSON)."""

DEFAULT_PROXY_TRUSTED_CIDRS: tuple[str, ...] = ()
"""No proxy is trusted by default: identity headers are ignored until the
deployment sets ``FL_PROXY_TRUSTED_CIDRS`` to its reverse proxy's address."""


APPRISE_TAG_PATTERN = re.compile(r"fl-[a-z0-9][a-z0-9-]*")
"""Dedicated Apprise tags: the ``fl-`` prefix and one plain tag, no expression."""

CONSUMED = "consumed"
M1, M2, M3, M4, M5 = "M1", "M2", "M3", "M4", "M5"

FIELD_STATUS: dict[str, str] = {
    "log_level": CONSUMED,
    "json_logs": CONSUMED,
    "include_timestamp": CONSUMED,
    "bind_host": CONSUMED,
    "port": CONSUMED,
    "timezone": M1,
    "database_path": CONSUMED,
    "registry_seed": CONSUMED,
    "caldav_url": M1,
    "caldav_username": M1,
    "caldav_app_password": M1,
    "caldav_calendars": M1,
    "ha_token": CONSUMED,
    "apprise_url": CONSUMED,
    "apprise_config_key": CONSUMED,
    "apprise_tag_report": M3,
    "apprise_tag_status": M3,
    "apprise_tag_warning": M3,
    "apprise_tag_urgent": M3,
    "apprise_tag_ops": CONSUMED,
    "apprise_tag_shadow": M3,
    "proxy_trusted_cidrs": M4,
    "ui_allowed_users": M4,
    "data_key": CONSUMED,
    "route_provider": M3,
    "google_routes_api_key": M3,
    "routes_monthly_budget_usd": M3,
    "routes_budget_warn_usd": M3,
    "llm_enabled": M5,
    "llm_base_url": M5,
    "llm_api_key": M5,
    "llm_model": M5,
    "weekly_cron": M3,
    "daily_cron": M3,
    "change_scan_minutes": M3,
    "notification_mode": CONSUMED,
    "quiet_hours_start": M3,
    "quiet_hours_end": M3,
    "imminent_window_minutes": M3,
}
"""Whether each setting is read today or waits for a milestone.

Settings marked with a milestone are defined ahead of the code that reads
them so the deployment contract is stable. Two tests in
tests/unit/test_config_security.py keep this table honest: one fails when a
field is added without an entry here, and one fails when a field marked
``consumed`` is not read (``.<field>``) anywhere in ``src/`` outside this
module. Milestone-marked fields are, by definition, not read yet; the table
records that they are dormant rather than preventing it.
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
        caldav_calendars: Calendar hrefs or IDs to collect (discovered in M0).
        ha_token: Bearer token Home Assistant presents to the HA-only routes.
        apprise_url: Apprise API base URL.
        apprise_config_key: Apprise API persistent-configuration key; the
            client posts to ``/notify/<key>`` and selects recipients with the
            ``tag`` field.
        apprise_tag_report: Weekly and daily report email.
        apprise_tag_status: Completed-assessment short push.
        apprise_tag_warning: New validation error push.
        apprise_tag_urgent: Critical issue on an imminent ride (bypasses quiet hours).
        apprise_tag_ops: Service-health alerts (Morgan only).
        apprise_tag_shadow: Shadow-mode output (Morgan only).
        proxy_trusted_cidrs: TCP peers whose Authentik identity headers are
            believed (the reverse proxy only); empty, the default, trusts none.
        ui_allowed_users: Authentik usernames allowed into the registry UI;
            empty denies everyone.
        data_key: Fernet key encrypting protected addresses at rest; required
            before any protected address is stored.
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
        change_scan_minutes: Interval of the incremental change scan.
        notification_mode: ``shadow`` (default) sends the Sunday report to Morgan
            only and nothing to Riley; ``production`` needs joint approval.
        quiet_hours_start: Start of quiet hours (local time).
        quiet_hours_end: End of quiet hours (local time).
        imminent_window_minutes: Rides starting within this window are
            imminent; their critical issues bypass quiet hours.
    """

    model_config = SettingsConfigDict(
        env_prefix="FL_",
        case_sensitive=False,
        extra="ignore",
        # #CRITICAL: Security: a rejected secret (short HA token, malformed
        # data key) must never be echoed in an error or traceback.
        # #VERIFY: tests/unit/test_config_security.py::TestNoSecretEcho.
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

    ha_token: SecretStr | None = None

    apprise_url: str | None = None
    apprise_config_key: str = Field(
        default="family-logistics", pattern=r"^[A-Za-z0-9_-]{1,128}$"
    )
    # Dedicated tags; who receives each one is configured in Apprise, not here.
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

    # #CRITICAL: Shadow mode is the default. Production notifications to both
    # parents require joint approval after the two-week shadow period.
    # #VERIFY: tests/unit/test_app_cli_config.py::TestSettings::
    # test_defaults_are_safe asserts the shadow default.
    notification_mode: Literal["shadow", "production"] = "shadow"
    quiet_hours_start: time = time(22, 0)
    quiet_hours_end: time = time(6, 0)
    imminent_window_minutes: int = Field(default=180, ge=0)

    @field_validator("ha_token")
    @classmethod
    def _ha_token_long_enough(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject a configured HA token shorter than the minimum length.

        Args:
            value: The configured token, or None when unset.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If a token is set but is shorter than 32 characters.
        """
        if value is not None and len(value.get_secret_value()) < HA_TOKEN_MIN_LENGTH:
            msg = f"FL_HA_TOKEN must be at least {HA_TOKEN_MIN_LENGTH} characters"
            raise ValueError(msg)
        return value

    @field_validator(
        "apprise_tag_report",
        "apprise_tag_status",
        "apprise_tag_warning",
        "apprise_tag_urgent",
        "apprise_tag_ops",
        "apprise_tag_shadow",
    )
    @classmethod
    def _apprise_tag_is_dedicated(cls, value: str) -> str:
        """Require a dedicated ``fl-`` tag that is a single plain tag.

        Args:
            value: The configured tag.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the tag does not match ``fl-[a-z0-9-]+``.
        """
        # #CRITICAL: Security: the app may only notify its own dedicated tags.
        # A shared tag (for example "critical") or an Apprise tag expression
        # (spaces mean AND, commas mean OR) would reach people outside this
        # app's routing, so anything else fails at settings load.
        # #VERIFY: tests/unit/test_apprise_client.py::TestTagSettings rejects
        # "critical", "all", "fl-a,fl-b", "fl-a b" and "FL-ops".
        if not APPRISE_TAG_PATTERN.fullmatch(value):
            msg = "FL_APPRISE_TAG_* values must match fl-[a-z0-9-]+"
            raise ValueError(msg)
        return value

    @field_validator("caldav_url")
    @classmethod
    def _caldav_url_is_https(cls, value: str) -> str:
        """Require HTTPS for the CalDAV endpoint.

        Args:
            value: The configured base URL.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the URL does not use the https scheme.
        """
        # #CRITICAL: Security: Basic credentials (the app-specific password)
        # travel with every CalDAV request; plain HTTP would expose them.
        # #VERIFY: tests/unit/test_config_security.py::TestCaldavUrl.
        if not value.lower().startswith("https://"):
            msg = "FL_CALDAV_URL must use https://"
            raise ValueError(msg)
        return value

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

    @field_validator("proxy_trusted_cidrs", "ui_allowed_users", mode="before")
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
            msg = f"FL_PROXY_TRUSTED_CIDRS has an invalid network: {exc}"
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
        msg = "Invalid configuration: " + "; ".join(problems)
        raise ConfigurationError(msg, details={"problems": problems}) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Returns:
        Cached Settings built from the environment.
    """
    return load_settings()
