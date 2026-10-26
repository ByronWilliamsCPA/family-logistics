"""Home Assistant route relay routes (ADR 0006).

``GET /api/v1/route-requests`` lists the legs Home Assistant should price now;
``POST /api/v1/route-results`` takes the durations back. Both are registered on
the router from ``build_ha_router``, so the bearer-token dependency applies at
router level and cannot be forgotten here.

#CRITICAL: Security: a leg endpoint is the location's Home Assistant zone
entity id; only a location with no zone falls back to its decrypted address,
and that decryption happens here, inside the GET handler's response building,
and nowhere else. Addresses are never logged, never written to an audit row and
never put in an error message; a request whose endpoint cannot be resolved or
decrypted is left out of the response, not sent half-resolved.
#VERIFY: tests/unit/test_route_relay_api.py::TestGetRouteRequests (the zone and
address tests assert on the response body and on the captured log text).

#CRITICAL: Data integrity: the sender never decides what a result means. A
result names only a ``request_id`` and a ``duration_seconds``; the leg, weekday
and bucket come from the app's own request row. A duration outside
``1..MAX_ROUTE_DURATION_SECONDS``, or one that is not an integer, is a 422 and
nothing is stored, so a changed Home Assistant response shape fails visibly.
#VERIFY: tests/unit/test_route_relay_api.py::TestPostRouteResults.

#ASSUME: External resources: Home Assistant resolves a bare time-of-day
``departure_time`` to its next occurrence. NOT verified, so GET serves only the
requests whose weekday matches the next local occurrence and whose occurrence is
at least ``SERVE_LEAD_TIME`` away (``due_requests``), and POST rejects a result
whose request is no longer due (``not_due``).
#VERIFY: docs/guides/ha-route-relay.md, deployment checklist (first that the
integration returns a time-dependent duration at all, then the evening against
pre-08:00 comparison).

#ASSUME: External resources: Home Assistant's configured time zone equals
``FL_TIMEZONE``. ``departure_time`` is a bare local ``HH:MM:SS`` that Home
Assistant reads in its own zone; if the zones differ, the result is silently
filed under the wrong bucket or weekday and nothing here can detect it.
#VERIFY: docs/guides/ha-route-relay.md, deployment checklist (Settings, General,
time zone equals ``FL_TIMEZONE``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, StrictInt, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from family_logistics.core.crypto import (
    AddressCipher,
    ProtectedDataError,
    require_cipher,
)
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.db.audit import record_on
from family_logistics.db.route_relay import (
    MAX_REQUESTS_PER_POLL,
    MAX_ROUTE_DURATION_SECONDS,
    WEEKDAY_NAMES,
    BatchOutcome,
    LocationSource,
    PendingRequest,
    ResultStatus,
    RouteAnswer,
    RouteRelayBusyError,
    bucket_start,
)
from family_logistics.middleware.correlation import get_correlation_id
from family_logistics.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import APIRouter
    from sqlalchemy.engine import Connection

    from family_logistics.core.config import Settings
    from family_logistics.db.route_relay import RouteRelayRepository

logger = get_logger(__name__)

MAX_RESULTS_PER_POST = 100
"""Most results one POST may carry (422 above it); twice the number of requests
one poll returns. The byte cap on the body is ``MAX_BODY_BYTES``."""

MAX_BODY_BYTES = 64 * 1024
"""Largest ``POST /route-results`` body read, in bytes (413 above it). A full batch
of 100 results is well under 10 KiB, so this leaves room for extra fields."""

MAX_REQUEST_ID_LENGTH = 64
"""Longest ``request_id`` accepted; the app issues 32-character ids."""

BODY_TOO_LARGE = "Request body too large"
"""Fixed 413 text for a body over ``MAX_BODY_BYTES``."""

STORE_UNAVAILABLE = "Route store unavailable"
"""Fixed 503 text for a store error; it never carries SQL or location detail."""

DATABASE_BUSY = "Database busy"
"""Fixed 503 text when the write lock was not granted in time."""

AUDIT_UNAVAILABLE = "Audit log unavailable"
"""Fixed 503 text when the audit row cannot be written."""

AUDIT_ACTOR = "home-assistant"
"""Audit actor for results posted through the HA token."""

AUDIT_OBJECT_REF = "route-results"
"""Audit object reference; a fixed label, never location data."""


class RouteEndpoint(BaseModel):
    """One end of a leg, in the form the Home Assistant action takes.

    Attributes:
        kind: ``zone`` for a zone entity id, ``address`` for address text.
        value: The zone entity id or the address.
    """

    kind: Literal["zone", "address"]
    value: str


class RouteRequestItem(BaseModel):
    """One leg Home Assistant should price.

    Attributes:
        request_id: Opaque id to echo back in the result.
        origin: Where the leg starts.
        destination: Where the leg ends.
        weekday: Lowercase three-letter local weekday.
        departure_time: Local time of day, ``HH:MM:SS``.
    """

    request_id: str
    origin: RouteEndpoint
    destination: RouteEndpoint
    weekday: str
    departure_time: str


class RouteRequestsResponse(BaseModel):
    """Body of ``GET /api/v1/route-requests``.

    Attributes:
        requests: The due requests, oldest first.
    """

    requests: list[RouteRequestItem]


class RouteResultItem(BaseModel):
    """One posted result.

    Anything beyond these two fields is ignored: the app already knows the leg,
    weekday and bucket from its own request row.

    Attributes:
        request_id: The request being answered.
        duration_seconds: Driving time as a positive integer number of seconds.
    """

    request_id: Annotated[
        str, Field(min_length=1, max_length=MAX_REQUEST_ID_LENGTH, strict=True)
    ]
    duration_seconds: Annotated[
        StrictInt, Field(strict=True, ge=1, le=MAX_ROUTE_DURATION_SECONDS)
    ]


class RouteResultsBody(BaseModel):
    """Body of ``POST /api/v1/route-results``.

    Attributes:
        results: Between 1 and ``MAX_RESULTS_PER_POST`` results.
    """

    results: Annotated[
        list[RouteResultItem], Field(min_length=1, max_length=MAX_RESULTS_PER_POST)
    ]


class RouteResultOutcome(BaseModel):
    """How one posted result was handled.

    Attributes:
        request_id: The id as posted.
        status: ``accepted``, ``unchanged`` or ``rejected``.
        reason: Why it was rejected; None otherwise.
    """

    request_id: str
    status: str
    reason: str | None


class RouteResultsResponse(BaseModel):
    """Body of ``POST /api/v1/route-results``.

    Attributes:
        accepted: Results stored.
        unchanged: Identical replays, ignored.
        rejected: Results refused for their own reason.
        results: One outcome per posted result, in order.
    """

    accepted: int
    unchanged: int
    rejected: int
    results: list[RouteResultOutcome]


def _utc_now() -> datetime:
    """Return the current instant (aware, UTC)."""
    return datetime.now(UTC)


def _unavailable(detail: str, retry_after: int | None = None) -> HTTPException:
    """Build a 503 whose body carries no store detail.

    Args:
        detail: Short, fixed message.
        retry_after: Seconds the client should wait, when it can retry.

    Returns:
        The exception.
    """
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
        headers=headers,
    )


class _Endpoints:
    """Resolves location ids to zone ids or decrypted addresses.

    The cipher is built on first use, so a poll whose legs are all zoned needs
    no data key.

    Args:
        settings: Application settings carrying ``data_key``.
        sources: Zone ids and protected addresses by location id.
    """

    def __init__(self, settings: Settings, sources: dict[str, LocationSource]) -> None:
        self._settings = settings
        self._sources = sources
        self._cipher: AddressCipher | None = None

    def resolve(self, location_id: str) -> RouteEndpoint | None:
        """Return the endpoint for a location, or None when it has none.

        Args:
            location_id: The location's id.

        Returns:
            The zone endpoint, else the decrypted address, else None when the
            location has neither or the address cannot be decrypted.
        """
        source = self._sources.get(location_id)
        if source is None:
            return None
        if source.zone_entity_id:
            return RouteEndpoint(kind="zone", value=source.zone_entity_id)
        if source.address_token is None:
            return None
        try:
            if self._cipher is None:
                self._cipher = require_cipher(self._settings.data_key)
            address = self._cipher.decrypt(source.address_token).reveal()
        except (ConfigurationError, ProtectedDataError):
            return None
        return RouteEndpoint(kind="address", value=address)


def _items(
    due: list[PendingRequest], settings: Settings, sources: dict[str, LocationSource]
) -> list[RouteRequestItem]:
    """Build the response items, leaving out requests that cannot be routed.

    The poll limit applies to the routable requests, so unroutable ones queued
    ahead of them cannot starve them.

    Args:
        due: The due requests, oldest first.
        settings: Application settings.
        sources: Zone ids and protected addresses by location id.

    Returns:
        One item per routable request, at most ``MAX_REQUESTS_PER_POLL``.
    """
    endpoints = _Endpoints(settings, sources)
    items: list[RouteRequestItem] = []
    for request in due:
        if len(items) >= MAX_REQUESTS_PER_POLL:
            break
        origin = endpoints.resolve(request.leg.origin)
        destination = endpoints.resolve(request.leg.destination)
        if origin is None or destination is None:
            # Only the request id is logged: no location text, no address.
            logger.warning(
                "route_relay.request_unroutable", request_id=request.request_id
            )
            continue
        items.append(
            RouteRequestItem(
                request_id=request.request_id,
                origin=origin,
                destination=destination,
                weekday=WEEKDAY_NAMES[request.leg.weekday],
                departure_time=bucket_start(request.leg.bucket).strftime("%H:%M:%S"),
            )
        )
    return items


async def _read_body(request: Request) -> bytes:
    """Read the request body, refusing more than ``MAX_BODY_BYTES``.

    Both the declared ``Content-Length`` and the bytes actually read are
    checked, so a missing or understated length cannot slip a large body in.

    Args:
        request: The request, already authenticated.

    Returns:
        The body bytes.

    Note:
        A body over the cap raises ``HTTPException`` with status 413, via
        ``_refuse_over_cap``.
    """
    # #CRITICAL: Security: the cap must hold before anything parses the body;
    # an unbounded read is a memory-exhaustion path for a caller with the token.
    # #VERIFY: tests/unit/test_route_relay_api.py (the 413 cases, with and
    # without a Content-Length header).
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit():
        _refuse_over_cap(int(declared))
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        _refuse_over_cap(len(body))
    return bytes(body)


def _refuse_over_cap(size: int) -> None:
    """Refuse a body of ``size`` bytes when it is over ``MAX_BODY_BYTES``.

    Args:
        size: The declared or accumulated body size in bytes.

    Raises:
        HTTPException: 413 when ``size`` is over the cap.
    """
    if size > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=BODY_TOO_LARGE
        )


def _parse_answers(raw: bytes) -> list[RouteAnswer]:
    """Validate the body and turn it into answers, echoing none of it back.

    Args:
        raw: The body bytes.

    Returns:
        One answer per posted result.

    Raises:
        HTTPException: 422 when the body is not valid JSON or fails
            validation. The error body carries only each problem's location,
            type and message, never the posted input.
    """
    try:
        body = RouteResultsBody.model_validate_json(raw)
    except ValidationError as error:
        problems = [
            {"type": item["type"], "loc": list(item["loc"]), "msg": item["msg"]}
            for item in error.errors(
                include_url=False, include_context=False, include_input=False
            )
        ]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problems
        ) from None
    return [
        RouteAnswer(item.request_id, item.duration_seconds) for item in body.results
    ]


def register_route_relay_routes(
    router: APIRouter,
    settings: Settings,
    repository: RouteRelayRepository,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> None:
    """Register the route relay routes on the authenticated HA router.

    Args:
        router: The router from ``build_ha_router`` (token check at router level).
        settings: Application settings (time zone, data key, audit key).
        repository: The route cache and request queue; its lock timeout is how
            long a write waits for the database write lock.
        clock: Returns the current instant (aware); injectable for tests.
    """
    zone = ZoneInfo(settings.timezone)

    @router.get("/route-requests", response_model=RouteRequestsResponse)
    def get_route_requests() -> RouteRequestsResponse:
        """List the legs Home Assistant should price now."""
        try:
            # No limit here: unroutable requests are dropped in ``_items``
            # before the poll limit applies.
            due = repository.due_requests(now=clock(), zone=zone, limit=None)
            sources = repository.location_sources(
                {end for r in due for end in (r.leg.origin, r.leg.destination)}
            )
        except SQLAlchemyError:
            logger.exception("route_relay.poll_failed")
            raise _unavailable(STORE_UNAVAILABLE) from None
        items = _items(due, settings, sources)
        logger.info("route_relay.requests_served", count=len(items))
        return RouteRequestsResponse(requests=items)

    def audit(connection: Connection, outcome: BatchOutcome) -> None:
        counts = {
            "accepted": outcome.count(ResultStatus.ACCEPTED),
            "unchanged": outcome.count(ResultStatus.UNCHANGED),
            "rejected": outcome.count(ResultStatus.REJECTED),
        }
        record_on(
            connection,
            key=settings.audit_key,
            key_version=settings.audit_key_version,
            actor=AUDIT_ACTOR,
            object_ref=AUDIT_OBJECT_REF,
            action=(
                f"route-results a{counts['accepted']} r{counts['rejected']}"
                f" u{counts['unchanged']}"
            ),
            new_state=counts,
            correlation_id=get_correlation_id(),
        )

    def record(answers: list[RouteAnswer]) -> RouteResultsResponse:
        try:
            outcome = repository.record_results(
                answers, now=clock(), zone=zone, before_commit=audit
            )
        except RouteRelayBusyError as busy:
            raise _unavailable(DATABASE_BUSY, busy.retry_after) from None
        except ConfigurationError:
            # An unset or malformed audit key: nothing was stored (the audit
            # row and the results share one transaction).
            logger.exception("route_relay.audit_unavailable")
            raise _unavailable(AUDIT_UNAVAILABLE) from None
        except SQLAlchemyError:
            logger.exception("route_relay.store_failed")
            raise _unavailable(STORE_UNAVAILABLE) from None
        return RouteResultsResponse(
            accepted=outcome.count(ResultStatus.ACCEPTED),
            unchanged=outcome.count(ResultStatus.UNCHANGED),
            rejected=outcome.count(ResultStatus.REJECTED),
            results=[
                RouteResultOutcome(
                    request_id=item.request_id,
                    status=item.status.value,
                    reason=item.reason.value if item.reason else None,
                )
                for item in outcome.items
            ],
        )

    @router.post(
        "/route-results",
        response_model=RouteResultsResponse,
        responses={
            status.HTTP_413_CONTENT_TOO_LARGE: {"description": "Body over the cap"},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid body"},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Retry"},
        },
    )
    async def post_route_results(request: Request) -> RouteResultsResponse:
        """Store the durations Home Assistant priced.

        The body is a JSON object ``{"results": [{"request_id": str,
        "duration_seconds": int}, ...]}``. It is read here, after the router's
        bearer check, so an unauthenticated request is refused (and throttled)
        whatever it sends.
        """
        raw = await _read_body(request)
        answers = _parse_answers(raw)
        # The repository is synchronous (SQLite); keep it off the event loop.
        return await run_in_threadpool(record, answers)
