"""HA-only routes (bearer token; network restriction is a deployment control, ADR-0007).

``build_ha_router`` returns the router every HA route is registered on. It
carries the bearer-token dependency at router level, so a route added to it
cannot forget authentication. Failed attempts are throttled per client address
and every authentication failure returns the same 401.

No business route exists yet. Later milestones (M2 and M4) add them to this
router: GET /api/v1/status, POST /api/v1/assessments,
GET /api/v1/assessments/{run_id}, POST /api/v1/ingest/ha-snapshot,
GET /api/v1/route-requests and POST /api/v1/route-results. They inherit the
dependency and must not be registered on any other router.
"""

from __future__ import annotations

import hmac
import math
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import SecretStr

    from family_logistics.core.config import Settings

logger = get_logger(__name__)

MAX_FAILURES = 5
"""Failed attempts from one client, within ``FAILURE_WINDOW_SECONDS``, that lock it out."""

FAILURE_WINDOW_SECONDS = 60.0
"""Sliding window for counting failures, and the length of a lockout."""

MAX_TRACKED_CLIENTS = 1024
"""Upper bound on throttle entries; the least recently active is evicted first."""

UNKNOWN_CLIENT = "unknown"
"""Throttle key used when the transport reports no client address."""

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


@dataclass
class _ClientState:
    """Failure history for one client.

    Attributes:
        failures: Monotonic times of failures inside the current window.
        locked_until: Monotonic time the lockout ends; 0.0 when not locked.
    """

    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


class HaAuthThrottle:
    """In-memory, per-client throttle for failed Home Assistant authentication.

    A client that fails ``max_failures`` times within ``window`` seconds is
    locked out for ``window`` seconds. While locked, the caller must refuse the
    request without evaluating the token. One client's state never affects
    another's, and a successful authentication does not touch any state.

    Memory is bounded: expired entries are pruned on every call and at most
    ``max_clients`` are tracked, evicting the least recently active first.
    All state is guarded by a lock because sync dependencies run in a
    threadpool.

    Args:
        max_failures: Failures within ``window`` that trigger a lockout.
        window: Sliding window and lockout length, in seconds.
        max_clients: Maximum number of tracked clients.
        clock: Monotonic time source in seconds; injectable for tests.
    """

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        window: float = FAILURE_WINDOW_SECONDS,
        max_clients: int = MAX_TRACKED_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window = window
        self._max_clients = max_clients
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: OrderedDict[str, _ClientState] = OrderedDict()

    def retry_after(self, client: str) -> int:
        """Report whether a client is locked out.

        Args:
            client: Client key.

        Returns:
            Whole seconds (at least 1) until the lockout ends, or 0 when the
            client may attempt authentication.
        """
        with self._lock:
            now = self._clock()
            self._prune(now)
            state = self._clients.get(client)
            if state is None or state.locked_until <= now:
                return 0
            return max(1, math.ceil(state.locked_until - now))

    def record_failure(self, client: str) -> bool:
        """Record a failed attempt.

        Args:
            client: Client key.

        Returns:
            True when this failure started a lockout.
        """
        with self._lock:
            now = self._clock()
            self._prune(now)
            state = self._clients.setdefault(client, _ClientState())
            self._clients.move_to_end(client)
            while state.failures and state.failures[0] <= now - self._window:
                state.failures.popleft()
            state.failures.append(now)
            locked = len(state.failures) >= self._max_failures
            if locked:
                state.failures.clear()
                state.locked_until = now + self._window
            while len(self._clients) > self._max_clients:
                self._clients.popitem(last=False)
            return locked

    def _prune(self, now: float) -> None:
        """Drop expired entries from the least recently active end.

        Entries are ordered by last failure and expiry grows with it, so the
        first live entry ends the scan. The caller must hold the lock.

        Args:
            now: Current monotonic time.
        """
        while self._clients:
            state = next(iter(self._clients.values()))
            last = state.failures[-1] if state.failures else 0.0
            if state.locked_until > now or last > now - self._window:
                return
            self._clients.popitem(last=False)


def _client_key(request: Request) -> str:
    """Return the throttle key for a request.

    Args:
        request: The incoming request.

    Returns:
        The peer address, or ``UNKNOWN_CLIENT`` when the transport gives none.
    """
    # #ASSUME: Security: Home Assistant calls this service directly, not through
    # the reverse proxy listed in FL_PROXY_TRUSTED_CIDRS, so the TCP peer is the
    # real client. Behind a proxy every client would share one throttle bucket.
    # #VERIFY: if HA is ever routed through the proxy, key on the trusted-proxy
    # client IP (as the security middleware does) instead of request.client.host.
    client = request.client
    return client.host if client is not None else UNKNOWN_CLIENT


def _unauthorized() -> HTTPException:
    """Build the single 401 returned for every authentication failure.

    Returns:
        An exception whose body and headers never reveal the failure cause.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_ha_token(
    settings: Settings, throttle: HaAuthThrottle
) -> Callable[..., None]:
    """Build the dependency that authenticates Home Assistant requests.

    Args:
        settings: Application settings carrying ``ha_token``.
        throttle: Failure throttle shared by the routes it guards.

    Returns:
        A FastAPI dependency that returns None on success, raises 429 with
        ``Retry-After`` while the client is locked out (without evaluating the
        token), and raises a uniform 401 on any authentication failure.
    """

    # #CRITICAL: Security: this dependency must be attached to the HA router
    # itself (``APIRouter(dependencies=...)``), never to individual routes, so a
    # route added later cannot forget authentication. An unset token fails
    # closed with 401.
    # #VERIFY: tests/unit/test_ha_router.py::TestRouterLevelGuarantee asserts a
    # route added to the router after creation still requires the token.
    def dependency(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        client = _client_key(request)
        wait = throttle.retry_after(client)
        if wait:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts",
                headers={"Retry-After": str(wait)},
            )
        if settings.ha_token is None:
            logger.error("ha_auth.token_unconfigured", client=client)
        elif verify_ha_token(authorization, settings.ha_token):
            return
        else:
            logger.warning("ha_auth.failed", client=client)
        if throttle.record_failure(client):
            logger.warning(
                "ha_auth.locked_out",
                client=client,
                seconds=throttle.retry_after(client),
            )
        raise _unauthorized()

    return dependency


def build_ha_router(
    settings: Settings, *, throttle: HaAuthThrottle | None = None
) -> APIRouter:
    """Build the Home Assistant router with authentication at router level.

    Later milestones add the HA routes listed in the module docstring to this
    router; they inherit the bearer-token dependency automatically.

    Args:
        settings: Application settings carrying ``ha_token``.
        throttle: Failure throttle; a default one is created when omitted.

    Returns:
        A router under ``/api/v1`` whose every route requires the HA token.
    """
    guard = require_ha_token(
        settings, throttle if throttle is not None else HaAuthThrottle()
    )
    return APIRouter(
        prefix="/api/v1",
        tags=["home-assistant"],
        dependencies=[Depends(guard)],
    )
