"""HA-only routes (bearer token; network restriction is a deployment control, ADR-0007).

``build_ha_router`` returns the router every HA route is registered on. It
carries the bearer-token dependency at router level, so a route added to it
cannot forget authentication. Failed attempts are throttled per client address
and every authentication failure returns the same 401.

``POST /api/v1/ingest/ha-snapshot`` (the school snapshot, ADR 0007) is the
first business route. Later milestones (M2 and M4) add the others to this
router: GET /api/v1/status, POST /api/v1/assessments,
GET /api/v1/assessments/{run_id}, GET /api/v1/route-requests and
POST /api/v1/route-results. They inherit the dependency and must not be
registered on any other router.
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
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.concurrency import run_in_threadpool

from family_logistics.adapters.home_assistant.schemas import (
    MAX_SNAPSHOT_BYTES,
    HaSnapshotPayload,
    safe_error_locations,
)
from family_logistics.core.exceptions import ConfigurationError, ValidationError
from family_logistics.db.school_snapshots import (
    SchoolSnapshotBusyError,
    SchoolSnapshotConflictError,
)
from family_logistics.middleware.correlation import get_correlation_id
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import SecretStr

    from family_logistics.core.config import Settings
    from family_logistics.services.school_snapshot import SchoolSnapshotIngest

logger = get_logger(__name__)

MAX_FAILURES = 5
"""Failed attempts from one client, within ``FAILURE_WINDOW_SECONDS``, that lock it out."""

FAILURE_WINDOW_SECONDS = 60.0
"""Sliding window for counting failures, and the length of a lockout."""

MAX_TRACKED_CLIENTS = 1024
"""Upper bound on throttle entries; only entries with no live lockout or in-flight
attempt are evicted (the least recently active first)."""

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
    """Attempt history for one client.

    Attributes:
        failures: Monotonic times of failures inside the current window.
        locked_until: Monotonic time the lockout ends; 0.0 when not locked.
        in_flight: Admitted attempts whose token check has not finished.
    """

    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0
    in_flight: int = 0


class HaAuthThrottle:
    """In-memory, per-client throttle for failed Home Assistant authentication.

    A client that fails ``max_failures`` times within ``window`` seconds is
    locked out for ``window`` seconds. While locked, the caller must refuse the
    request without evaluating the token. One client's state never affects
    another's, and a successful authentication leaves no failure behind.

    Admission is atomic. ``admit`` checks the lockout and reserves an attempt
    slot under one lock acquisition, and a slot counts toward the limit while
    its token check runs. So at most ``max_failures`` evaluations can be
    outstanding or failed inside the window for one client, however many
    requests arrive in parallel. The caller must settle every admitted attempt
    exactly once: ``release`` on success, ``fail`` on failure (``fail`` also
    when the check raised).

    Memory is bounded: expired entries are pruned on every call and at most
    ``max_clients`` are tracked. A full table makes room only by evicting an
    entry that is not locked out and has no attempt in flight (an expired one
    first, then the least recently active). If every slot holds a live lockout
    or an in-flight attempt, an unknown client is refused until one frees, so
    cycling source addresses cannot erase a lockout. All state is guarded by a
    lock because sync dependencies run in a threadpool.

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
        """Report whether a client is locked out, without reserving anything.

        Args:
            client: Client key.

        Returns:
            Whole seconds (at least 1) until the lockout ends, or 0 when the
            client is not locked out.
        """
        with self._lock:
            now = self._clock()
            self._prune(now)
            state = self._clients.get(client)
            if state is None or state.locked_until <= now:
                return 0
            return max(1, math.ceil(state.locked_until - now))

    def admit(self, client: str) -> int:
        """Check the lockout and reserve an attempt slot, atomically.

        Args:
            client: Client key.

        Returns:
            0 when a slot was reserved: the caller must evaluate the token and
            then call ``release`` or ``fail`` for this client. Otherwise whole
            seconds (at least 1) to wait; nothing was reserved and the caller
            must refuse the request without evaluating the token.
        """
        with self._lock:
            now = self._clock()
            self._prune(now)
            state = self._clients.get(client)
            if state is None:
                if len(self._clients) >= self._max_clients and not self._evict(now):
                    return self._seconds_until_a_slot_frees(now)
                state = self._clients.setdefault(client, _ClientState())
            if state.locked_until > now:
                return max(1, math.ceil(state.locked_until - now))
            self._trim(state, now)
            if len(state.failures) + state.in_flight >= self._max_failures:
                # Every remaining attempt is already running; once they settle
                # the client is either locked out or has a slot again.
                return 1
            state.in_flight += 1
            return 0

    def release(self, client: str) -> None:
        """Settle an admitted attempt that authenticated successfully.

        Args:
            client: Client key passed to the matching ``admit``.
        """
        with self._lock:
            state = self._clients.get(client)
            if state is None:
                return
            state.in_flight = max(0, state.in_flight - 1)
            if self._is_idle(state, self._clock()):
                del self._clients[client]

    def fail(self, client: str) -> bool:
        """Settle an admitted attempt as a failure.

        Args:
            client: Client key passed to the matching ``admit``.

        Returns:
            True when this failure started a lockout.
        """
        with self._lock:
            now = self._clock()
            state = self._clients.setdefault(client, _ClientState())
            self._clients.move_to_end(client)
            state.in_flight = max(0, state.in_flight - 1)
            self._trim(state, now)
            state.failures.append(now)
            locked = len(state.failures) >= self._max_failures
            if locked:
                state.failures.clear()
                state.locked_until = now + self._window
            return locked

    def _trim(self, state: _ClientState, now: float) -> None:
        """Drop failures that have left the window.

        Args:
            state: The client's state.
            now: Current monotonic time.
        """
        while state.failures and state.failures[0] <= now - self._window:
            state.failures.popleft()

    def _is_idle(self, state: _ClientState, now: float) -> bool:
        """Report whether an entry holds nothing worth keeping.

        Args:
            state: The client's state.
            now: Current monotonic time.

        Returns:
            True when it has no lockout, no in-flight attempt and no failure
            inside the window.
        """
        last = state.failures[-1] if state.failures else 0.0
        return (
            state.in_flight == 0
            and state.locked_until <= now
            and last <= now - self._window
        )

    def _prune(self, now: float) -> None:
        """Drop idle entries from the least recently active end.

        Entries are ordered by last failure and expiry grows with it, so the
        first live entry ends the scan. The caller must hold the lock.

        Args:
            now: Current monotonic time.
        """
        while self._clients:
            state = next(iter(self._clients.values()))
            if not self._is_idle(state, now):
                return
            self._clients.popitem(last=False)

    def _evict(self, now: float) -> bool:
        """Free one slot without touching a live lockout or in-flight attempt.

        Prefers an expired entry, then the least recently active entry that is
        not locked out and has nothing in flight. The caller must hold the lock.

        Args:
            now: Current monotonic time.

        Returns:
            True when a slot was freed, False when every entry is protected.
        """
        victim: str | None = None
        for key, state in self._clients.items():
            if state.locked_until > now or state.in_flight:
                continue
            if self._is_idle(state, now):
                victim = key
                break
            if victim is None:
                victim = key
        if victim is None:
            return False
        del self._clients[victim]
        return True

    def _seconds_until_a_slot_frees(self, now: float) -> int:
        """Estimate the wait when every tracked entry is protected.

        Args:
            now: Current monotonic time.

        Returns:
            Whole seconds (at least 1) until the earliest lockout ends.
        """
        ends = [s.locked_until for s in self._clients.values() if s.locked_until > now]
        return max(1, math.ceil(min(ends) - now)) if ends else 1


def _client_key(request: Request) -> str:
    """Return the throttle key for a request.

    Args:
        request: The incoming request.

    Returns:
        The peer address, or ``UNKNOWN_CLIENT`` when the transport gives none.
    """
    # #ASSUME: Security: Home Assistant calls this service directly, not through
    # the reverse proxy listed in FL_PROXY_TRUSTED_CIDRS, so the TCP peer is the
    # real client. ``serve`` starts uvicorn with proxy_headers=False, so
    # request.client is always the TCP peer and a client cannot choose its own
    # throttle key with X-Forwarded-For (FORWARDED_ALLOW_IPS is ignored). Behind
    # a proxy every client would share one throttle bucket.
    # #VERIFY: tests/unit/test_app_cli_config.py asserts serve passes
    # proxy_headers=False; if HA is ever routed through the proxy, key on the
    # trusted-proxy client IP (as the security middleware does) instead of
    # request.client.host, and do not re-enable uvicorn proxy headers for it.
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


def _token_is_valid(settings: Settings, authorization: str | None, client: str) -> bool:
    """Evaluate the presented token and log a failure without its value.

    Args:
        settings: Application settings carrying ``ha_token``.
        authorization: Raw ``Authorization`` header value, or None.
        client: Throttle key, for the log line.

    Returns:
        True when the token is valid; an unset ``ha_token`` is never valid.
    """
    if settings.ha_token is None:
        logger.error("ha_auth.token_unconfigured", client=client)
        return False
    if verify_ha_token(authorization, settings.ha_token):
        return True
    logger.warning("ha_auth.failed", client=client)
    return False


def require_ha_token(
    settings: Settings, throttle: HaAuthThrottle
) -> Callable[..., None]:
    """Build the dependency that authenticates Home Assistant requests.

    Args:
        settings: Application settings carrying ``ha_token``.
        throttle: Failure throttle shared by the routes it guards.

    Returns:
        A FastAPI dependency that returns None on success, raises 429 with
        ``Retry-After`` while the client is locked out or has no attempt slot
        free (without evaluating the token), and raises a uniform 401 on any
        authentication failure.
    """

    # #CRITICAL: Security: this dependency must be attached to the HA router
    # itself (``APIRouter(dependencies=...)``), never to individual routes, so a
    # route added later cannot forget authentication. An unset token fails
    # closed with 401.
    # #VERIFY: tests/unit/test_ha_router.py::TestRouterLevelGuarantee asserts a
    # route added to the router after creation still requires the token.
    #
    # #CRITICAL: Concurrency: the dependency runs in a threadpool, so the
    # lockout check and the failure record must be one atomic step; a separate
    # check-then-record lets a parallel burst all pass the check before any
    # failure lands and evaluate far more than ``max_failures`` tokens. Every
    # admitted attempt must be settled exactly once, even when the check raises,
    # or the client leaks an in-flight slot and is eventually locked out.
    # #VERIFY: tests/unit/test_ha_router.py::TestAtomicAdmission asserts a
    # threaded burst evaluates at most MAX_FAILURES tokens and that a raising
    # check still settles its slot.
    def dependency(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        client = _client_key(request)
        wait = throttle.admit(client)
        if wait:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts",
                headers={"Retry-After": str(wait)},
            )
        authenticated = False
        locked = False
        try:
            authenticated = _token_is_valid(settings, authorization, client)
        finally:
            if authenticated:
                throttle.release(client)
            else:
                locked = throttle.fail(client)
        if authenticated:
            return
        if locked:
            logger.warning(
                "ha_auth.locked_out",
                client=client,
                seconds=throttle.retry_after(client),
            )
        raise _unauthorized()

    return dependency


class _BodyTooLargeError(Exception):
    """The request body exceeds ``MAX_SNAPSHOT_BYTES``."""


INVALID_SNAPSHOT = "Invalid snapshot"
"""``detail`` of every 422 from the snapshot ingest."""


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    """Read the request body, refusing it as soon as it exceeds ``limit``.

    Args:
        request: The incoming request.
        limit: Largest accepted body, in bytes.

    Returns:
        The body.

    Raises:
        HTTPException: 400 for a malformed ``Content-Length``.
        _BodyTooLargeError: When the declared or streamed size exceeds ``limit``.
    """
    # #CRITICAL: security: the size limit is enforced before any parsing, on
    # the declared length and again on the bytes actually streamed, so a
    # chunked or lying client cannot make the app buffer more than ``limit``.
    # The route reads the body itself (no FastAPI body parameter), so the
    # router-level token check runs before a single body byte is read.
    # #VERIFY: tests/unit/test_ha_snapshot_route.py::TestSize and
    # TestAuthentication.test_authentication_comes_before_the_size_limit.
    declared = request.headers.get("content-length")
    if declared is not None:
        if not (declared.isascii() and declared.isdigit()):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad Request")
        if int(declared) > limit:
            raise _BodyTooLargeError
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _BodyTooLargeError
    return bytes(body)


def _is_json(request: Request) -> bool:
    """Report whether the request declares a JSON body.

    Args:
        request: The incoming request.

    Returns:
        True for ``application/json`` with or without parameters.
    """
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    return media_type.strip().lower() == "application/json"


def _invalid(
    code: str | None = None, errors: list[dict[str, object]] | None = None
) -> JSONResponse:
    """Build a 422 that carries no payload text.

    Args:
        code: Machine-readable reason, if any.
        errors: Redacted validation locations, if any.

    Returns:
        The response.
    """
    content: dict[str, object] = {"detail": INVALID_SNAPSHOT}
    if code is not None:
        content["code"] = code
    if errors is not None:
        content["errors"] = errors
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=content
    )


def _unavailable(reason: str | None = None) -> JSONResponse:
    """Build the 503 for an ingest the app is not configured to accept.

    Args:
        reason: Error code for the operator log; None when no service exists.

    Returns:
        The response; its body names no configuration value.
    """
    logger.error("ha_snapshot.unavailable", reason=reason or "NOT_WIRED")
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "School snapshot ingest is not available"},
    )


def _ingest_snapshot(
    service: SchoolSnapshotIngest, body: bytes, correlation_id: str | None
) -> JSONResponse:
    """Parse, validate and store a snapshot body (runs in a worker thread).

    Args:
        service: The ingest service.
        body: The raw request body, already size-checked.
        correlation_id: Request correlation ID for the audit row.

    Returns:
        The response: 200 with the acknowledgement, 409, 422 or 503 (with
        ``Retry-After`` while another writer holds the database).
    """
    # #CRITICAL: security: neither a response nor a log line may echo the
    # payload (children's school calendars, calendar entity IDs). Errors are
    # reduced to codes and redacted locations; logs carry only the reason.
    # #VERIFY: tests/unit/test_ha_snapshot_route.py::TestMalformed and
    # TestUnknownReferences assert a marker string never reaches the
    # response or caplog.
    try:
        snapshot = HaSnapshotPayload.model_validate_json(body)
    except PydanticValidationError as exc:
        errors = safe_error_locations(exc)
        logger.warning("ha_snapshot.rejected", reason="schema", errors=len(errors))
        return _invalid(errors=errors)
    try:
        ack = service.ingest(snapshot, correlation_id=correlation_id)
    except SchoolSnapshotConflictError:
        logger.warning("ha_snapshot.rejected", reason="older_than_stored")
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "A newer school snapshot is already stored"},
        )
    except SchoolSnapshotBusyError as exc:
        # #ASSUME: timing: the write lock is held briefly (another ingest, a
        # purge or a migration); Home Assistant retries on its next poll or
        # after Retry-After. Nothing was stored and no audit row was written.
        # #VERIFY: TestBusy in tests/unit/test_ha_snapshot_route.py.
        logger.warning("ha_snapshot.rejected", reason=exc.error_code)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "School snapshot store is busy"},
            headers={"Retry-After": str(exc.retry_after)},
        )
    except ValidationError as exc:
        logger.warning("ha_snapshot.rejected", reason=exc.error_code)
        return _invalid(code=exc.error_code)
    except ConfigurationError as exc:
        return _unavailable(exc.error_code)
    logger.info(
        "ha_snapshot.ingested",
        status=ack.status.value,
        schools=ack.schools,
        days=ack.days,
        fetched_at=ack.fetched_at.isoformat(),
    )
    return JSONResponse(content=ack.as_dict())


def _add_snapshot_route(
    router: APIRouter, school_ingest: SchoolSnapshotIngest | None
) -> None:
    """Register ``POST /ingest/ha-snapshot`` on the authenticated router.

    Args:
        router: The HA router (token dependency already attached).
        school_ingest: The ingest service; None answers 503.
    """

    async def ingest_ha_snapshot(request: Request) -> JSONResponse:
        """Accept a Home Assistant school snapshot (ADR 0007).

        Args:
            request: The incoming request; the body is read here, bounded.

        Returns:
            200 with counts and freshness; 409, 413, 415, 422 or 503 otherwise.
        """
        if school_ingest is None:
            return _unavailable()
        if not _is_json(request):
            return JSONResponse(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                content={"detail": "Expected application/json"},
            )
        try:
            body = await _read_bounded_body(request, MAX_SNAPSHOT_BYTES)
        except _BodyTooLargeError:
            logger.warning("ha_snapshot.rejected", reason="too_large")
            return JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={"detail": "Payload too large"},
            )
        return await run_in_threadpool(
            _ingest_snapshot, school_ingest, body, get_correlation_id()
        )

    router.add_api_route(
        "/ingest/ha-snapshot",
        ingest_ha_snapshot,
        methods=["POST"],
        name="ingest_ha_snapshot",
    )


def build_ha_router(
    settings: Settings,
    *,
    throttle: HaAuthThrottle | None = None,
    school_ingest: SchoolSnapshotIngest | None = None,
) -> APIRouter:
    """Build the Home Assistant router with authentication at router level.

    Every route listed in the module docstring is registered on this router
    and inherits the bearer-token dependency automatically.

    Args:
        settings: Application settings carrying ``ha_token``.
        throttle: Failure throttle; a default one is created when omitted.
        school_ingest: School snapshot ingest service; without it the ingest
            route answers 503.

    Returns:
        A router under ``/api/v1`` whose every route requires the HA token.
    """
    guard = require_ha_token(
        settings, throttle if throttle is not None else HaAuthThrottle()
    )
    router = APIRouter(
        prefix="/api/v1",
        tags=["home-assistant"],
        dependencies=[Depends(guard)],
    )
    _add_snapshot_route(router, school_ingest)
    return router
