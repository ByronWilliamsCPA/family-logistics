"""HA-only routes (bearer token; network restriction is a deployment control, ADR-0007).

TODO(M2/M4): GET /api/v1/status, POST /api/v1/assessments,
GET /api/v1/assessments/{run_id}, POST /api/v1/ingest/ha-snapshot,
GET /api/v1/route-requests and POST /api/v1/route-results, each guarded by
``verify_ha_token``.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import SecretStr

BEARER_SCHEME = "bearer"
"""Auth scheme, compared case-insensitively (RFC 7235 section 2.1, RFC 6750)."""


def verify_ha_token(authorization: str | None, expected: SecretStr | None) -> bool:
    """Check the bearer token Home Assistant presents.

    Args:
        authorization: Raw ``Authorization`` header value, or None.
        expected: The configured ``FL_HA_TOKEN``, or None when unset.

    Returns:
        True only when a token is configured and the header carries exactly it.
    """
    # #CRITICAL: Security: compare in constant time so response timing does
    # not reveal how much of a guessed token matched; an unset token denies
    # everything rather than accepting an empty header. The scheme name is
    # case-insensitive (RFC 7235); the token itself is compared exactly.
    # #VERIFY: tests/unit/test_ha_token.py covers scheme case, a wrong or
    # missing token, a missing scheme and an unset FL_HA_TOKEN.
    if expected is None or authorization is None:
        return False
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != BEARER_SCHEME:
        return False
    presented = credentials.lstrip(" ").encode("utf-8")
    if not presented:
        return False
    return hmac.compare_digest(presented, expected.get_secret_value().encode("utf-8"))
