"""HA-only routes (bearer token; network restriction is a deployment control, ADR-0007).

No route exists yet. Planned for milestones M2 and M4, each to be guarded by
``verify_ha_token``: GET /api/v1/status, POST /api/v1/assessments,
GET /api/v1/assessments/{run_id}, POST /api/v1/ingest/ha-snapshot,
GET /api/v1/route-requests and POST /api/v1/route-results. ``verify_ha_token``
is a plain function, so each route must call it explicitly.
"""

from __future__ import annotations

import hmac
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import SecretStr

_BEARER = re.compile(r"bearer +(\S+)", re.IGNORECASE)
"""``Authorization: Bearer <token>``: the scheme is case-insensitive (RFC 7235
section 2.1, RFC 6750), the token is not."""


def verify_ha_token(authorization: str | None, expected: SecretStr | None) -> bool:
    """Check the bearer token Home Assistant presents.

    Args:
        authorization: Raw ``Authorization`` header value, or None.
        expected: The configured ``FL_HA_TOKEN``, or None when unset.

    Returns:
        True only when a non-empty token is configured and the header carries
        exactly it under the ``Bearer`` scheme (matched case-insensitively).
    """
    # #CRITICAL: Security: compare in constant time so response timing does
    # not reveal how much of a guessed token matched; an unset or empty token
    # denies everything rather than accepting an empty header.
    # #VERIFY: tests/unit/test_ha_token.py asserts hmac.compare_digest is the
    # comparison and that None, empty and malformed headers are all denied.
    if expected is None or authorization is None:
        return False
    secret = expected.get_secret_value()
    if not secret:
        return False
    match = _BEARER.fullmatch(authorization)
    if match is None:
        return False
    return hmac.compare_digest(match.group(1).encode("utf-8"), secret.encode("utf-8"))
