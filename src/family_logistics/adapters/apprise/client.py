"""Minimal Apprise API client.

The application sends through one saved Apprise configuration, ``POST
/notify/{KEY}`` where ``KEY`` is the configuration key (``FL_APPRISE_CONFIG_KEY``),
and selects the services inside it with the ``tag`` payload field. That is the
documented stateful contract of the Apprise API; the key is not a tag.

Only the application's own dedicated tags (``fl-*`` by default, set by the
``FL_APPRISE_TAG_*`` settings) are ever used. Who receives each tag is
configured in the notification service, never here. A tag is checked twice: the
settings reject any value that is not ``fl-<word>[-<word>]``, and this client
refuses anything else at send time, before a request is made, so a shared
severity tag such as ``all`` can never be used by mistake.

#CRITICAL: Security: callers pass counts, statuses and internal references
only. Message bodies must never contain secrets, addresses or other household
data; this client does not inspect content, so the rule is enforced by the
callers that build messages.
#VERIFY: review every caller that builds a message body (none exists before
milestone M3); ``tests/unit/test_apprise_client.py`` pins the exact payload so a
new field cannot be added unnoticed.
#ASSUME: External resources: the Apprise API may be down or slow. A delivery
failure (network error, any status other than 200) returns False and is logged;
it is never reported as delivered. A refused tag is a programming error and
raises ``NotificationTagError`` instead.
#VERIFY: ``test_apprise_client.py::TestDelivery`` covers connection errors,
timeouts and the non-200 statuses; callers must check the returned bool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx

from family_logistics.core.config import (
    APPRISE_KEY_PATTERN,
    DEDICATED_TAG_FIELDS,
    is_dedicated_tag,
)
from family_logistics.core.exceptions import ConfigurationError, ValidationError
from family_logistics.core.urls import HTTP_OR_HTTPS, url_problem
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from family_logistics.core.config import Settings

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
MAX_TITLE_LENGTH: Final = 120
MAX_BODY_LENGTH: Final = 2000
DELIVERED_STATUS: Final = 200
"""The only status the Apprise API uses for a completed send. A 424 means a
send failed or no requested tag matched; 404 means the key has no saved
configuration."""

logger = get_logger(__name__)


class NotificationTagError(ValidationError):
    """Raised when a caller asks to send to a tag the app does not own."""


def configured_tags(settings: Settings) -> frozenset[str]:
    """Return the dedicated tags this deployment may send to.

    Args:
        settings: Application settings.

    Returns:
        The six configured ``FL_APPRISE_TAG_*`` values.
    """
    return frozenset(getattr(settings, name) for name in DEDICATED_TAG_FIELDS)


class AppriseClient:
    """Sends notifications through one saved Apprise configuration.

    Args:
        base_url: Apprise API base URL (http or https, no embedded credentials).
        config_key: Key of the saved configuration, the ``{KEY}`` in
            ``POST /notify/{KEY}``.
        allowed_tags: The only tags this client will send to; each must be a
            dedicated ``fl-*`` tag.
        transport: Optional transport (tests use ``httpx.MockTransport``).

    Raises:
        ConfigurationError: If the base URL, the key or the tag set is unusable.
    """

    def __init__(
        self,
        base_url: str,
        config_key: str,
        allowed_tags: frozenset[str],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        problem = url_problem(base_url, HTTP_OR_HTTPS)
        if problem is not None:
            msg = f"Apprise base URL {problem}"
            raise ConfigurationError(msg)
        if APPRISE_KEY_PATTERN.fullmatch(config_key) is None:
            msg = "Apprise configuration key must be 1 to 64 letters, digits, _ or -"
            raise ConfigurationError(msg)
        if not allowed_tags or not all(is_dedicated_tag(tag) for tag in allowed_tags):
            msg = "Apprise allowed tags must be a non-empty set of dedicated fl-* tags"
            raise ConfigurationError(msg)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=DEFAULT_TIMEOUT_SECONDS,
            transport=transport,
        )
        self._path = f"/notify/{config_key}"
        self._allowed_tags = allowed_tags

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.BaseTransport | None = None
    ) -> AppriseClient | None:
        """Build the client from settings.

        The constructor's checks still apply, so an unusable URL, key or tag
        set raises ``ConfigurationError``.

        Args:
            settings: Application settings.
            transport: Optional transport for tests.

        Returns:
            A client, or None when ``FL_APPRISE_URL`` is not configured.
        """
        if settings.apprise_url is None:
            return None
        return cls(
            settings.apprise_url,
            settings.apprise_config_key,
            configured_tags(settings),
            transport=transport,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def notify(self, tag: str, title: str, body: str) -> bool:
        """Send one notification to the services carrying ``tag``.

        Args:
            tag: One of the configured dedicated tags.
            title: Short title (truncated to 120 characters).
            body: Message body (truncated to 2000 characters).

        Returns:
            True only when the Apprise API answered 200 (the send completed).
            False for any network failure or other status; the caller decides
            whether to escalate, this client never retries.

        Raises:
            NotificationTagError: If ``tag`` is not one of the allowed
                dedicated tags. Nothing is sent.
        """
        if not is_dedicated_tag(tag) or tag not in self._allowed_tags:
            msg = f"refusing to send to tag {tag!r}: not a configured app tag"
            raise NotificationTagError(msg, field="tag")
        payload = {
            "title": title[:MAX_TITLE_LENGTH],
            "body": body[:MAX_BODY_LENGTH],
            "tag": tag,
        }
        try:
            response = self._client.post(self._path, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("notification.failed", tag=tag, error=type(exc).__name__)
            return False
        delivered = response.status_code == DELIVERED_STATUS
        if not delivered:
            logger.warning(
                "notification.rejected", tag=tag, status=response.status_code
            )
        return delivered


def ops_alert(client: AppriseClient, settings: Settings, title: str, body: str) -> bool:
    """Send a service-health alert to the ops tag.

    The alert is addressed to the ops tag in shadow and production mode alike.
    That the ops tag reaches the operator only is a property of the Apprise
    configuration, not of this client.

    Not yet called by the scheduler: wiring it to failed or incomplete runs,
    source collection failures, a degraded database and the routing budget
    warning is part of roadmap milestone M3 (``docs/project/roadmap.md``,
    "ops alerts"). Until then no code path raises these alerts.

    Args:
        client: A configured Apprise client.
        settings: Application settings (for the ops tag).
        title: Short alert title.
        body: Alert body with no household data.

    Returns:
        True when the Apprise API confirmed the send. A client built without
        the ops tag in its allowed set raises ``NotificationTagError`` from
        ``notify``; nothing is sent.
    """
    return client.notify(settings.apprise_tag_ops, title, body)
