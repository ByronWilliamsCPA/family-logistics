"""Apprise notification adapter (dedicated ``fl-*`` tags only)."""

from family_logistics.adapters.apprise.client import (
    AppriseClient,
    NotificationTagError,
    configured_tags,
    ops_alert,
)

__all__ = ["AppriseClient", "NotificationTagError", "configured_tags", "ops_alert"]
