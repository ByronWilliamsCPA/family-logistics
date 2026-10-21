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

BEARER_PREFIX = "Bearer "


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
    # everything rather than accepting an empty header.
    if expected is None or authorization is None:
        return False
    if not authorization.startswith(BEARER_PREFIX):
        return False
    presented = authorization.removeprefix(BEARER_PREFIX).encode("utf-8")
    return hmac.compare_digest(presented, expected.get_secret_value().encode("utf-8"))
