"""HA-only routes (bearer token; network restriction is a deployment control, ADR-0007).

``build_ha_router`` returns the router every HA route is registered on. It
carries the bearer-token dependency at router level, so a route added to it
cannot forget authentication. Failed attempts are throttled per client address
and every authentication failure returns the same 401.

``POST /api/v1/ingest/ha-snapshot`` (the school snapshot, ADR 0007; M3,
merged in #29) is the first business route. Once the body is read, its errors
are built by hand (``_error``) rather than raised as ``HTTPException``, so no
body can echo the rejected input: the snapshot is children's school data. The
route relay, GET /api/v1/route-requests and POST /api/v1/route-results
(ADR 0006; M3, merged in #32), is defined in ``api/route_relay.py`` and
registered on this router by ``create_app``. M4 adds GET /api/v1/status,
POST /api/v1/assessments and GET /api/v1/assessments/{run_id}. Every route
inherits the dependency and must not be registered on any other router.
"""

from __future__ import annotations

import hmac
import json
import math
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from family_logistics.adapters.home_assistant.schemas import (
    MAX_SNAPSHOT_BYTES,
    HaSnapshotPayload,
    safe_error_locations,
)
from family_logistics.adapters.home_assistant.school_days import SnapshotMappingError
from family_logistics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    ValidationError,
)
from family_logistics.db.school_snapshots import (
    SchoolSnapshotBusyError,
    SchoolSnapshotConflictError,
)
from family_logistics.middleware.correlation import get_correlation_id
from family_logistics.services.school_snapshot import IngestRejectedError
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

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


class _DuplicateKeyError(ValueError):
    """A JSON object repeats a key; the message never names the key."""


INVALID_SNAPSHOT = "Invalid snapshot"
"""``detail`` of every 422 from the snapshot ingest."""

DUPLICATE_JSON_KEY = "DUPLICATE_JSON_KEY"
"""Code of the 422 for a JSON object that repeats a key at any depth."""

_PUBLIC_VALIDATION_CODES = frozenset({"INVALID_TIME"})
"""Codes of a plain ``ValidationError`` from the store that mean bad input.

Any other plain ``ValidationError`` beneath the ingest (an audit field outside
its column, a repeated person and day the mapper should have prevented) is a
server-side fault, not a client error."""


class ErrorLocation(BaseModel):
    """One redacted validation location (never an input value).

    Attributes:
        loc: Schema field names and list indexes; any other part is ``"*"``.
        type: Pydantic error type.
    """

    loc: list[str | int]
    type: str


class ErrorBody(BaseModel):
    """Body of every non-200 answer from the snapshot ingest.

    Attributes:
        detail: Fixed, payload-free sentence.
        code: Machine-readable reason, when there is one.
        errors: Redacted validation locations, for a schema rejection.
    """

    detail: str
    code: str | None = None
    errors: list[ErrorLocation] | None = None


class IngestAckBody(BaseModel):
    """Body of a 200 from the snapshot ingest (counts and freshness only).

    Attributes:
        status: ``stored``, or ``unchanged`` for an identical re-post.
        schools: Configured schools covered by the snapshot.
        days: School-day rows in the stored snapshot.
        schools_without_days: ``location_id`` of each configured school that
            got no day rows (reported as uncertain, never blocking), in
            configuration order.
        fetched_at: Stored freshness, ISO 8601 with a UTC offset.
        received_at: Server time the stored snapshot arrived, ISO 8601.
    """

    status: Literal["stored", "unchanged"]
    schools: int
    days: int
    schools_without_days: list[str]
    fetched_at: str
    received_at: str


def _error(
    status_code: int, body: ErrorBody, headers: dict[str, str] | None = None
) -> JSONResponse:
    """Build an error answer that carries no payload text.

    Args:
        status_code: HTTP status.
        body: The fixed, payload-free body.
        headers: Extra response headers, if any.

    Returns:
        The response.
    """
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json", exclude_none=True),
        headers=headers,
    )


def _plain(
    status_code: int, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    """Build an error answer that has only a ``detail`` sentence.

    Args:
        status_code: HTTP status.
        detail: Fixed sentence for the client.
        headers: Extra response headers, if any.

    Returns:
        The response.
    """
    return _error(status_code, ErrorBody(detail=detail), headers)


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
    # A chunk is counted before it is kept, so the buffer never exceeds it.
    # The route reads the body itself (no FastAPI body parameter), so the
    # router-level token check runs before a single body byte is read.
    # #VERIFY: tests/unit/test_ha_snapshot_route.py::TestSize and
    # TestAuthentication.
    declared = request.headers.get("content-length")
    if declared is not None:
        if not (declared.isascii() and declared.isdigit()):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad Request")
        # Digits are bounded before int(): a header of thousands of digits
        # would exceed Python's int conversion limit and raise ValueError.
        digits = declared.lstrip("0")
        if len(digits) > len(str(limit)) or int(digits or "0") > limit:
            raise _BodyTooLargeError
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise _BodyTooLargeError
        body.extend(chunk)
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
    body = ErrorBody.model_validate(
        {"detail": INVALID_SNAPSHOT, "code": code, "errors": errors}
    )
    return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, body)


def _unavailable(reason: str | None = None) -> JSONResponse:
    """Build the 503 for an ingest the app is not configured to accept.

    Args:
        reason: Error code for the operator log; None when no service exists.

    Returns:
        The response; its body names no configuration value and, unlike the
        busy 503, carries no ``Retry-After``.
    """
    logger.error("ha_snapshot.unavailable", reason=reason or "NOT_WIRED")
    return _plain(
        status.HTTP_503_SERVICE_UNAVAILABLE, "School snapshot ingest is not available"
    )


def _store_failed(exc: BaseException) -> JSONResponse:
    """Build the 500 for a storage or internal fault, logging only its class.

    Args:
        exc: The fault. Its message is never logged or returned: a database
            error carries statement text and an internal one may carry input.

    Returns:
        A fixed, payload-free 500 with no ``Retry-After``.
    """
    driver_error = cast("BaseException | None", getattr(exc, "orig", None))
    logger.error(
        "ha_snapshot.failed",
        error_class=type(exc).__name__,
        driver_class=type(driver_error).__name__ if driver_error else None,
        sqlite_errorcode=getattr(driver_error, "sqlite_errorcode", None),
        reason=getattr(exc, "error_code", None),
    )
    return _plain(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "School snapshot ingest failed"
    )


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing a key that appears twice.

    ``json.loads`` keeps only the last of two equal keys, so a repeated
    ``events`` list or calendar entity would silently drop the earlier one.

    Args:
        pairs: The key and value pairs of one JSON object, in document order.

    Returns:
        The object as a dict.

    Raises:
        _DuplicateKeyError: If a key repeats. The message never names it.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = "a JSON object repeats a key"
            raise _DuplicateKeyError(msg)
        result[key] = value
    return result


def _parse_snapshot(body: bytes) -> HaSnapshotPayload:
    """Parse a snapshot body, refusing a repeated JSON key at any depth.

    Args:
        body: The raw request body, already size-checked.

    Returns:
        The validated payload. A repeated key raises ``_DuplicateKeyError``;
        a body that is not JSON (invalid UTF-8, a number beyond Python's int
        conversion limit) raises ``ValueError``, and one nested past the parser
        limit ``RecursionError``; a schema mismatch raises pydantic's
        ``ValidationError``.
    """
    # #CRITICAL: data integrity: the schema cannot see duplicate keys (the last
    # one wins), so a repeated key would silently drop an earlier calendar or
    # event list from a snapshot that then reads as a complete one.
    # #VERIFY: TestDuplicateKeys in tests/unit/test_ha_snapshot_route.py.
    document = cast(
        "object",
        json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicate_keys),
    )
    return HaSnapshotPayload.model_validate(document)


def _parse_or_reject(body: bytes) -> HaSnapshotPayload | JSONResponse:
    """Parse a snapshot body, or build the 422 that refuses it.

    Args:
        body: The raw request body, already size-checked.

    Returns:
        The validated payload, or the 422 response (no payload text in it).
    """
    try:
        return _parse_snapshot(body)
    except _DuplicateKeyError:
        logger.warning("ha_snapshot.rejected", reason="duplicate_key")
        return _invalid(code=DUPLICATE_JSON_KEY)
    except PydanticValidationError as exc:
        errors = safe_error_locations(exc)
        logger.warning("ha_snapshot.rejected", reason="schema", errors=len(errors))
        return _invalid(errors=errors)
    except (ValueError, RecursionError):
        logger.warning("ha_snapshot.rejected", reason="json_invalid")
        return _invalid(errors=[{"loc": [], "type": "json_invalid"}])


def _fault_response(exc: Exception) -> JSONResponse:
    """Map an ingest fault other than conflict and busy to a response.

    Args:
        exc: A project validation or configuration error, or a database error.

    Returns:
        422 for bad input (a closed set of codes), 503 for a configuration
        fault, otherwise a 500. A database fault is a 500 without
        ``Retry-After``: only genuine lock contention is
        ``SchoolSnapshotBusyError`` (the store narrows it), so no other fault
        is ever reported as busy.
    """
    if isinstance(exc, ConfigurationError):
        return _unavailable(exc.error_code)
    if isinstance(exc, ValidationError) and (
        isinstance(exc, IngestRejectedError | SnapshotMappingError)
        or exc.error_code in _PUBLIC_VALIDATION_CODES
    ):
        logger.warning("ha_snapshot.rejected", reason=exc.error_code)
        return _invalid(code=exc.error_code)
    return _store_failed(exc)


def _store_snapshot(
    service: SchoolSnapshotIngest,
    snapshot: HaSnapshotPayload,
    correlation_id: str | None,
) -> JSONResponse:
    """Store a validated snapshot and map every outcome to a response.

    Args:
        service: The ingest service.
        snapshot: The validated payload.
        correlation_id: Request correlation ID for the audit row.

    Returns:
        200 with the acknowledgement, or the 409, 422, 500 or 503 for the
        fault (``Retry-After`` only for genuine lock contention).
    """
    try:
        ack = service.ingest(snapshot, correlation_id=correlation_id)
    except SchoolSnapshotConflictError:
        logger.warning("ha_snapshot.rejected", reason="older_than_stored")
        return _plain(
            status.HTTP_409_CONFLICT, "The snapshot is not newer than the stored one"
        )
    except SchoolSnapshotBusyError as exc:
        # #ASSUME: timing: the write lock is held briefly (another ingest, a
        # purge or a migration). Home Assistant pushes on its automation
        # schedule, so its next push retries; Retry-After is advisory for a
        # client that honours it. Nothing was stored and no audit row written.
        # #VERIFY: TestBusy in tests/unit/test_ha_snapshot_route.py.
        logger.warning("ha_snapshot.rejected", reason=exc.error_code)
        return _plain(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "School snapshot store is busy",
            headers={"Retry-After": str(exc.retry_after)},
        )
    except (
        ValidationError,
        ConfigurationError,
        DatabaseError,
        SQLAlchemyError,
    ) as exc:
        return _fault_response(exc)
    logger.info(
        "ha_snapshot.ingested",
        status=ack.status.value,
        schools=ack.schools,
        days=ack.days,
        schools_without_days=list(ack.schools_without_days),
        fetched_at=ack.fetched_at.isoformat(),
    )
    content = ack.as_dict() | {"schools_without_days": list(ack.schools_without_days)}
    return JSONResponse(content=content)


def _ingest_snapshot(
    service: SchoolSnapshotIngest, body: bytes, correlation_id: str | None
) -> JSONResponse:
    """Parse, validate and store a snapshot body (runs in a worker thread).

    Args:
        service: The ingest service.
        body: The raw request body, already size-checked.
        correlation_id: Request correlation ID for the audit row.

    Returns:
        The response: 200 with the acknowledgement, 409, 422, 500 (a storage
        or internal fault, never reported as busy) or 503 (with
        ``Retry-After`` only while another writer holds the database).
    """
    # #CRITICAL: security: neither a response nor a log line may echo the
    # payload (children's school calendars, calendar entity IDs). Errors are
    # reduced to codes and redacted locations; logs carry only the reason and
    # exception class names, never an exception message.
    # #VERIFY: tests/unit/test_ha_snapshot_route.py::TestMalformed,
    # TestUnknownReferences and TestStorageFaults assert a marker string never
    # reaches the response or caplog.
    parsed = _parse_or_reject(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    return _store_snapshot(service, parsed, correlation_id)


def _inline_refs(schema: object, definitions: Mapping[str, object]) -> object:
    """Resolve every ``$ref`` into ``definitions`` so a schema stands alone.

    Args:
        schema: A JSON Schema fragment.
        definitions: The ``$defs`` of the document it came from.

    Returns:
        The fragment with each ``#/$defs/<name>`` reference replaced by its
        definition. The models used here are not recursive.
    """
    if isinstance(schema, dict):
        fragment = cast("dict[str, object]", schema)
        ref = fragment.get("$ref")
        if isinstance(ref, str):
            return _inline_refs(definitions[ref.rsplit("/", 1)[-1]], definitions)
        return {
            key: _inline_refs(value, definitions)
            for key, value in fragment.items()
            if key != "$defs"
        }
    if isinstance(schema, list):
        return [
            _inline_refs(item, definitions) for item in cast("list[object]", schema)
        ]
    return schema


def _request_body_schema() -> dict[str, object]:
    """Describe the JSON body for OpenAPI without making FastAPI parse it.

    Returns:
        An OpenAPI ``requestBody`` object for ``openapi_extra``. The route
        keeps reading the body itself so the size limit and the duplicate key
        check run before any parsing.
    """
    schema = cast("dict[str, object]", HaSnapshotPayload.model_json_schema())
    definitions = cast("Mapping[str, object]", schema.get("$defs", {}))
    return {
        "required": True,
        "content": {"application/json": {"schema": _inline_refs(schema, definitions)}},
    }


def _error_response(description: str, **extra: object) -> dict[str, object]:
    """Build one ``responses=`` entry for an :class:`ErrorBody` answer.

    Args:
        description: What the status means for this route.
        **extra: Further OpenAPI response keys, such as ``headers``.

    Returns:
        The entry.
    """
    return {"model": ErrorBody, "description": description, **extra}


_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "model": IngestAckBody,
        "description": "Stored, or unchanged for an identical re-post.",
    },
    400: _error_response("Malformed Content-Length header."),
    401: _error_response("Missing or wrong bearer token (router dependency)."),
    409: _error_response("Not newer than the stored snapshot (equal freshness too)."),
    413: _error_response("Body larger than the size limit."),
    415: _error_response("Content-Type is not application/json."),
    422: _error_response("Invalid snapshot: schema, repeated key, bad reference."),
    429: _error_response("Too many failed authentications (router dependency)."),
    500: _error_response("Storage or internal fault; not retryable as busy."),
    503: _error_response(
        "Not available as configured, or the store is busy (Retry-After then).",
        headers={
            "Retry-After": {
                "description": "Seconds to wait; present only when the store is busy.",
                "schema": {"type": "integer"},
            }
        },
    ),
}


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

        The JSON body is described in the OpenAPI document but read here, not
        by FastAPI, so the size limit and the duplicate key check come first.

        Args:
            request: The incoming request; the body is read here, bounded.

        Returns:
            200 with counts, freshness and the schools that got no days. Errors
            are 400 (bad Content-Length), 409, 413, 415, 422, 500 and 503; the
            router dependency adds 401 and 429.
        """
        if school_ingest is None:
            return _unavailable()
        if not _is_json(request):
            return _plain(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Expected application/json"
            )
        try:
            body = await _read_bounded_body(request, MAX_SNAPSHOT_BYTES)
        except _BodyTooLargeError:
            logger.warning("ha_snapshot.rejected", reason="too_large")
            return _plain(status.HTTP_413_CONTENT_TOO_LARGE, "Payload too large")
        return await run_in_threadpool(
            _ingest_snapshot, school_ingest, body, get_correlation_id()
        )

    router.add_api_route(
        "/ingest/ha-snapshot",
        ingest_ha_snapshot,
        methods=["POST"],
        name="ingest_ha_snapshot",
        responses=_RESPONSES,
        openapi_extra={"requestBody": _request_body_schema()},
    )


def build_ha_router(
    settings: Settings,
    *,
    throttle: HaAuthThrottle | None = None,
    school_ingest: SchoolSnapshotIngest | None = None,
) -> APIRouter:
    """Build the Home Assistant router with authentication at router level.

    The school snapshot ingest route is registered here; routes added later
    inherit the bearer-token dependency automatically.

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
