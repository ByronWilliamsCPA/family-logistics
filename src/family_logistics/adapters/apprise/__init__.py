"""Apprise notification adapter (dedicated ``fl-*`` tags only).

Standing requirements for this adapter:

- Never claim delivery unless the Apprise API confirms it (enforced by
  ``AppriseClient.notify``, which returns True only for a 200).
- In shadow mode only the Sunday report goes to Morgan, and nothing goes to
  Riley. Mode-aware routing is not implemented here: it arrives with the
  notifier in roadmap milestone M3, and until then recipients are decided only
  by how the ``fl-*`` tags are configured in Apprise.
"""

from family_logistics.adapters.apprise.client import (
    AppriseClient,
    NotificationTagError,
    configured_tags,
    ops_alert,
)

__all__ = ["AppriseClient", "NotificationTagError", "configured_tags", "ops_alert"]
