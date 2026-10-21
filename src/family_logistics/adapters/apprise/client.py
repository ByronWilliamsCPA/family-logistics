"""Minimal Apprise API client.

The application only ever sends to its own dedicated tags (``fl-*`` by
default, set by the ``FL_APPRISE_TAG_*`` settings). Who receives each tag is
configured in the notification service, never here. Any other tag is refused
before a request is made, so a shared severity tag can never be used by
mistake.

#CRITICAL: Security: callers pass counts, statuses and internal references
only. Message bodies must never contain secrets, addresses or other household
data; this client does not inspect content, so the rule is enforced by the
callers that build messages.
#ASSUME: External resources: the Apprise API may be down or slow. A failed
send returns False and is logged; it never raises into the caller and is
never reported as delivered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx

from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from family_logistics.core.config import Settings

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
MAX_TITLE_LENGTH: Final = 120
MAX_BODY_LENGTH: Final = 2000

logger = get_logger(__name__)


class NotificationTagError(ValueError):
    """Raised when a caller asks to send to a tag the app does not own."""


def configured_tags(settings: Settings) -> frozenset[str]:
    """Return the dedicated tags this deployment may send to.

    Args:
        settings: Application settings.

    Returns:
        The six configured ``FL_APPRISE_TAG_*`` values.
    """
    return frozenset(
        {
            settings.apprise_tag_report,
            settings.apprise_tag_status,
            settings.apprise_tag_warning,
            settings.apprise_tag_urgent,
            settings.apprise_tag_ops,
            settings.apprise_tag_shadow,
        }
    )


class AppriseClient:
    """Sends notifications to the Apprise API's ``/notify/<tag>`` endpoint.

    Args:
        base_url: Apprise API base URL.
        allowed_tags: The only tags this client will send to.
        timeout: Request timeout in seconds.
        transport: Optional transport (tests use ``httpx.MockTransport``).
    """

    def __init__(
        self,
        base_url: str,
        allowed_tags: frozenset[str],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )
        self._allowed_tags = allowed_tags

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def notify(self, tag: str, title: str, body: str) -> bool:
        """Send one notification.

        Args:
            tag: One of the configured dedicated tags.
            title: Short title (truncated to 120 characters).
            body: Message body (truncated to 2000 characters).

        Returns:
            True only when the Apprise API accepted the request (2xx).

        Raises:
            NotificationTagError: If ``tag`` is not one of the allowed tags.
        """
        if tag not in self._allowed_tags:
            msg = f"refusing to send to tag {tag!r}: not a configured app tag"
            raise NotificationTagError(msg)
        payload = {
            "title": title[:MAX_TITLE_LENGTH],
            "body": body[:MAX_BODY_LENGTH],
        }
        try:
            response = self._client.post(f"/notify/{tag}", json=payload)
        except httpx.HTTPError as exc:
            logger.warning("notification.failed", tag=tag, error=type(exc).__name__)
            return False
        delivered = response.is_success
        if not delivered:
            logger.warning(
                "notification.rejected", tag=tag, status=response.status_code
            )
        return delivered


def ops_alert(client: AppriseClient, settings: Settings, title: str, body: str) -> bool:
    """Send a service-health alert to the ops tag.

    Ops alerts go to the operator only, in shadow and production mode alike.

    Args:
        client: A configured Apprise client.
        settings: Application settings (for the ops tag).
        title: Short alert title.
        body: Alert body with no household data.

    Returns:
        True when the Apprise API accepted the alert.
    """
    # TODO(M3): call from the scheduler on failed or incomplete runs, source
    # collection failures, a degraded database and the routing budget warning.
    return client.notify(settings.apprise_tag_ops, title, body)
