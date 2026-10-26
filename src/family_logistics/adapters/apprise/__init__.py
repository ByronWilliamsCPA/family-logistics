"""Apprise notification adapter (dedicated ``fl-*`` tags only).

Standing requirements for this adapter:

- Never claim delivery unless the Apprise API confirms it (enforced by
  ``AppriseClient.notify``, which returns True only for a 200).
- In shadow mode every push, ops alerts included, goes to the shadow tag only,
  and nothing reaches Riley (ADR 0008, D1). Mode-aware routing is not done in
  this adapter: ``services.notifications.NotificationService`` chooses the tag
  and the client only refuses tags it does not own. Recipients are decided by
  how the ``fl-*`` tags are configured in Apprise.
"""

from family_logistics.adapters.apprise.client import (
    AppriseClient,
    NotificationTagError,
    configured_tags,
    ops_alert,
)

__all__ = ["AppriseClient", "NotificationTagError", "configured_tags", "ops_alert"]
