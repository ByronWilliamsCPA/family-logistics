"""Persistence for the ``ha_relay`` route provider (ADR 0006).

Home Assistant prices legs and posts the results back; the app caches them.
``route_request`` queues the legs the engine asked for and could not answer,
``route_cache`` holds the observed driving times. Both are keyed by location id
(never address text), so a location edit invalidates exactly the right rows
(migration 0005's trigger and cascading keys, not caller discipline).

#CRITICAL: Data integrity: the app, not the sender, decides what a result
means. ``record_results`` derives the cache key (origin, destination, weekday,
bucket) from the stored request row and ignores anything else the sender
claims, and stamps ``observed_at`` with the server's receipt time. A changed
Home Assistant response shape therefore cannot file a travel time under the
wrong leg, weekday or age.
#VERIFY: tests/unit/test_route_relay_repo.py::TestRecordResults.

#CRITICAL: Data integrity: a cached time is valid only for the same weekday and
15-minute bucket, and only while ``0 <= age <= CACHE_VALIDITY``. Anything else
is a miss, which the engine turns into ROUTE-010 and an INCOMPLETE run; a
duration is never borrowed from another bucket or guessed.
#VERIFY: tests/unit/test_route_relay_repo.py::TestLookup.

#CRITICAL: Concurrency: every write runs under ``BEGIN IMMEDIATE`` (SQLite's
write lock), so a result can never be filed for a request that a concurrent
location edit just dropped, and two writers are serialized. A lock held past
the timeout raises ``RouteRelayBusyError`` and nothing is written.
#VERIFY: tests/unit/test_route_relay_repo.py::TestWriteLock.

#ASSUME: External resources: Home Assistant resolves a bare ``departure_time``
(a time of day with no date) to the NEXT occurrence of that time, not to "now".
This is not verified. Only requests whose weekday matches the weekday of the
next local occurrence are served (``due_requests``), so under that assumption
the stored weekday and bucket label is true.
#VERIFY: the owner runs the same request in the evening and again before 08:00;
matching durations mean next-occurrence (docs/guides/ha-route-relay.md,
deployment checklist). Findings stay in shadow mode until then.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from sqlalchemy import delete, func, insert, literal, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from family_logistics.core.database import BUSY_TIMEOUT_MS
from family_logistics.core.exceptions import DatabaseError, ValidationError
from family_logistics.db.column_types import UtcDateTime
from family_logistics.db.schema import (
    MAX_ROUTE_DURATION_SECONDS,
    location,
    route_cache,
    route_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Generator, Sequence

    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.sql.elements import ColumnElement

CACHE_VALIDITY = timedelta(days=28)
"""How long an observation answers its weekday bucket (owner decision, ADR 0006).
Also the retention period: older entries are purged."""

REQUEST_TTL = timedelta(days=8)
"""How long a request waits for Home Assistant. It must outlast the longest wait
for a request's weekday to come due (6 days) plus a day of slack; a request that
expires is simply enqueued again by the next estimate that misses."""

BUCKET_MINUTES = 15
BUCKETS_PER_DAY = 24 * 60 // BUCKET_MINUTES

MAX_PENDING_REQUESTS = 500
"""Cap on live pending requests, so a bug cannot grow the queue without bound."""

MAX_REQUESTS_PER_POLL = 50
"""Cap on requests one poll returns, bounding the routing-provider calls a single
Home Assistant automation run makes."""

LOCK_RETRY_AFTER_SECONDS = 5
"""Seconds a client is asked to wait after ``RouteRelayBusyError``."""

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
"""Lowercase three-letter weekday names, indexed by ``date.weekday()``."""

__all__ = [
    "BUCKETS_PER_DAY",
    "BUCKET_MINUTES",
    "CACHE_VALIDITY",
    "LOCK_RETRY_AFTER_SECONDS",
    "MAX_PENDING_REQUESTS",
    "MAX_REQUESTS_PER_POLL",
    "MAX_ROUTE_DURATION_SECONDS",
    "REQUEST_TTL",
    "WEEKDAY_NAMES",
    "BatchOutcome",
    "CachedLeg",
    "EnqueueStatus",
    "ItemOutcome",
    "Leg",
    "LocationSource",
    "PendingRequest",
    "RejectReason",
    "ResultStatus",
    "RouteAnswer",
    "RouteRelayBusyError",
    "RouteRelayRepository",
    "bucket_start",
    "due_weekday",
    "slot_of",
]


class RouteRelayBusyError(DatabaseError):
    """Another writer held the database's write lock past the lock timeout.

    Nothing is written; the client may retry after ``retry_after`` seconds.
    """

    retry_after: int = LOCK_RETRY_AFTER_SECONDS


class EnqueueStatus(StrEnum):
    """What ``RouteRelayRepository.enqueue`` did."""

    CREATED = "created"
    EXISTS = "exists"
    FULL = "full"


class ResultStatus(StrEnum):
    """How one posted result was handled."""

    ACCEPTED = "accepted"
    UNCHANGED = "unchanged"
    REJECTED = "rejected"


class RejectReason(StrEnum):
    """Why one posted result was rejected."""

    UNKNOWN_REQUEST = "unknown_request"
    EXPIRED = "expired"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Leg:
    """One priced leg: where, and for which weekday and time-of-day bucket.

    Attributes:
        origin: Origin location id.
        destination: Destination location id.
        weekday: Local weekday, Monday is 0.
        bucket: 15-minute bucket of local time of day, 0 to 95.
    """

    origin: str
    destination: str
    weekday: int
    bucket: int

    @classmethod
    def at(
        cls, origin: str, destination: str, departure: datetime, zone: tzinfo
    ) -> Leg:
        """Build the leg a departure time falls in.

        Args:
            origin: Origin location id.
            destination: Destination location id.
            departure: Departure (aware).
            zone: The household's time zone (``FL_TIMEZONE``).

        Returns:
            The leg for the departure's local weekday and bucket.

        Note:
            A naive ``departure`` raises ``ValidationError``.
        """
        weekday, bucket = slot_of(departure, zone)
        return cls(origin, destination, weekday, bucket)


@dataclass(frozen=True, slots=True)
class CachedLeg:
    """A valid cached observation.

    Attributes:
        duration: Observed driving time.
        observed_at: When the server received it (UTC).
    """

    duration: timedelta
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PendingRequest:
    """A queued request as stored.

    Attributes:
        request_id: Opaque identifier Home Assistant echoes back.
        leg: The leg and its weekday bucket.
        created_at: When it was queued (UTC).
        expires_at: When it stops being answerable (UTC).
    """

    request_id: str
    leg: Leg
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LocationSource:
    """What a location offers as a routing endpoint, still protected.

    Attributes:
        zone_entity_id: The Home Assistant zone entity id, or None.
        address_token: Fernet token of the address, or None. Decrypt it only
            while building a response.
    """

    zone_entity_id: str | None
    address_token: str | None


@dataclass(frozen=True, slots=True)
class RouteAnswer:
    """One result as Home Assistant posted it, after boundary validation.

    Attributes:
        request_id: The request being answered.
        duration_seconds: Driving time in seconds.
    """

    request_id: str
    duration_seconds: int


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    """The handling of one posted result.

    Attributes:
        request_id: The request id as posted.
        status: Accepted, unchanged or rejected.
        reason: Why it was rejected; None otherwise.
    """

    request_id: str
    status: ResultStatus
    reason: RejectReason | None = None


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """The handling of a whole posted batch.

    Attributes:
        items: One outcome per posted result, in order.
    """

    items: tuple[ItemOutcome, ...]

    def count(self, status: ResultStatus) -> int:
        """Count the items with a status.

        Args:
            status: The status to count.

        Returns:
            How many items have it.
        """
        return sum(1 for item in self.items if item.status is status)


def bucket_start(bucket: int) -> time:
    """Return the local time of day a bucket starts at.

    Args:
        bucket: Bucket number, 0 to 95.

    Returns:
        The bucket's first second as a time of day.
    """
    minutes = bucket * BUCKET_MINUTES
    return time(minutes // 60, minutes % 60)


def slot_of(departure: datetime, zone: tzinfo) -> tuple[int, int]:
    """Return the local weekday and bucket a departure falls in.

    Args:
        departure: Departure (aware).
        zone: The household's time zone.

    Returns:
        ``(weekday, bucket)`` with Monday as 0 and buckets 0 to 95.

    Note:
        A naive ``departure`` raises ``ValidationError``.
    """
    _require_aware("departure", departure)
    local = departure.astimezone(zone)
    return local.weekday(), (local.hour * 60 + local.minute) // BUCKET_MINUTES


def _require_aware(name: str, value: datetime) -> None:
    """Refuse a naive timestamp without echoing it.

    Args:
        name: Field name for the message.
        value: The timestamp.

    Raises:
        ValidationError: If ``value`` is naive.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{name} must be timezone-aware"
        raise ValidationError(msg, field=name)


def _exists(candidate: datetime, zone: tzinfo) -> bool:
    """Report whether a local wall-clock time occurs (it does not in a DST gap).

    Args:
        candidate: A local time carrying ``zone``.
        zone: The household's time zone.

    Returns:
        True when the wall-clock time survives a round trip through UTC.
    """
    round_trip = candidate.astimezone(UTC).astimezone(zone)
    return round_trip.replace(tzinfo=None) == candidate.replace(tzinfo=None)


def due_weekday(bucket: int, now: datetime, zone: tzinfo) -> int:
    """Return the weekday of the next local occurrence of a bucket's start time.

    Home Assistant can price only "the next occurrence of a time of day", so a
    request is due when its weekday equals this value.

    Args:
        bucket: The request's bucket.
        now: Poll time (aware).
        zone: The household's time zone.

    Returns:
        The weekday (Monday is 0) of the first local occurrence of the
        bucket's start time strictly after ``now``.
    """
    # #EDGE: Timing: a wall-clock time inside a spring-forward gap does not
    # occur that day, so the next occurrence is the following day; an
    # ambiguous time in a fall-back hour counts as its first occurrence. The
    # weekday is that of the local date either way. A poll in the same second
    # as the bucket's start counts the time as already passed (next day).
    # #VERIFY: tests/unit/test_route_relay_repo.py::TestDueRequests covers a
    # gap, an ambiguous hour and midnight; check one request near a DST change
    # in shadow mode.
    start = bucket_start(bucket)
    moment = now.astimezone(UTC)
    day: date = now.astimezone(zone).date()
    while True:
        candidate = datetime.combine(day, start, tzinfo=zone)
        if _exists(candidate, zone) and candidate.astimezone(UTC) > moment:
            return day.weekday()
        day += timedelta(days=1)


@contextmanager
def _write_locked(engine: Engine, lock_timeout_ms: int) -> Generator[Connection]:
    """Yield a connection inside ``BEGIN IMMEDIATE``; commit on success.

    Args:
        engine: The application engine.
        lock_timeout_ms: How long to wait for the write lock.

    Yields:
        Connection: A connection holding the database's write lock. Any
        exception rolls the whole transaction back.

    Raises:
        RouteRelayBusyError: The write lock was not granted in time.
    """
    with engine.connect() as connection:
        connection.exec_driver_sql(f"PRAGMA busy_timeout={int(lock_timeout_ms)}")
        try:
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            except OperationalError as exc:
                msg = "the database is locked by another writer; nothing stored"
                raise RouteRelayBusyError(
                    msg, operation="route_relay", error_code="ROUTE_RELAY_BUSY"
                ) from exc
            committed = False
            try:
                yield connection
                connection.commit()
                committed = True
            finally:
                if not committed:
                    connection.rollback()
        finally:
            connection.exec_driver_sql(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            connection.commit()


def _before(column: ColumnElement[datetime], instant: datetime) -> ColumnElement[bool]:
    """Build ``column < instant`` compared numerically, as the CHECKs do.

    Args:
        column: A ``UtcDateTime`` column.
        instant: The instant to compare with (aware).

    Returns:
        The SQL expression.
    """
    return func.julianday(column) < func.julianday(literal(instant, UtcDateTime()))


class RouteRelayRepository:
    """Stores the route cache and the request queue.

    Args:
        engine: Database engine at the schema head.
        lock_timeout_ms: How long a write waits for the write lock before
            raising ``RouteRelayBusyError``.
    """

    def __init__(
        self, engine: Engine, *, lock_timeout_ms: int = BUSY_TIMEOUT_MS
    ) -> None:
        self._engine = engine
        self._lock_timeout_ms = lock_timeout_ms

    def lookup(self, leg: Leg, *, now: datetime) -> CachedLeg | None:
        """Return the cached observation for a leg when it is still valid.

        Args:
            leg: The leg, weekday and bucket.
            now: Reference instant (aware).

        Returns:
            The observation, or None when there is none or it is older than
            ``CACHE_VALIDITY`` or dated in the future.

        Note:
            A naive ``now`` raises ``ValidationError``.
        """
        _require_aware("now", now)
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(route_cache.c.duration_seconds, route_cache.c.observed_at)
                    .where(route_cache.c.origin_location_id == leg.origin)
                    .where(route_cache.c.destination_location_id == leg.destination)
                    .where(route_cache.c.weekday == leg.weekday)
                    .where(route_cache.c.bucket == leg.bucket)
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        observed_at = cast("datetime", row["observed_at"])
        age = now - observed_at
        if age < timedelta(0) or age > CACHE_VALIDITY:
            return None
        return CachedLeg(
            timedelta(seconds=cast("int", row["duration_seconds"])), observed_at
        )

    def enqueue(self, leg: Leg, *, now: datetime) -> EnqueueStatus:
        """Queue a request for a leg unless one is already pending.

        Args:
            leg: The leg, weekday and bucket.
            now: Reference instant (aware).

        Returns:
            ``CREATED`` for a new request, ``EXISTS`` when one is already
            pending for the same leg, weekday and bucket, ``FULL`` when the
            queue holds ``MAX_PENDING_REQUESTS`` live requests.

        Note:
            A naive ``now`` raises ``ValidationError``. If the write lock is not
            granted in time, ``RouteRelayBusyError`` is raised and nothing is
            written. A location id with no ``location`` row violates a foreign
            key and raises ``sqlalchemy.exc.IntegrityError``; nothing is
            written.
        """
        _require_aware("now", now)
        with _write_locked(self._engine, self._lock_timeout_ms) as connection:
            # An expired pending row would otherwise block the unique index.
            connection.execute(
                delete(route_request)
                .where(route_request.c.status == "pending")
                .where(route_request.c.origin_location_id == leg.origin)
                .where(route_request.c.destination_location_id == leg.destination)
                .where(route_request.c.weekday == leg.weekday)
                .where(route_request.c.bucket == leg.bucket)
                .where(_before(route_request.c.expires_at, now))
            )
            exists = connection.execute(
                select(route_request.c.request_id)
                .where(route_request.c.status == "pending")
                .where(route_request.c.origin_location_id == leg.origin)
                .where(route_request.c.destination_location_id == leg.destination)
                .where(route_request.c.weekday == leg.weekday)
                .where(route_request.c.bucket == leg.bucket)
            ).first()
            if exists is not None:
                return EnqueueStatus.EXISTS
            live = connection.execute(
                select(func.count())
                .select_from(route_request)
                .where(route_request.c.status == "pending")
                .where(~_before(route_request.c.expires_at, now))
            ).scalar_one()
            if live >= MAX_PENDING_REQUESTS:
                return EnqueueStatus.FULL
            connection.execute(
                insert(route_request).values(
                    request_id=uuid.uuid4().hex,
                    origin_location_id=leg.origin,
                    destination_location_id=leg.destination,
                    weekday=leg.weekday,
                    bucket=leg.bucket,
                    created_at=now,
                    expires_at=now + REQUEST_TTL,
                    status="pending",
                )
            )
        return EnqueueStatus.CREATED

    def pending(self, *, now: datetime) -> list[PendingRequest]:
        """Return every live pending request, oldest first.

        Args:
            now: Reference instant (aware).

        Returns:
            The requests whose expiry is still ahead.

        Note:
            A naive ``now`` raises ``ValidationError``.
        """
        _require_aware("now", now)
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(route_request)
                    .where(route_request.c.status == "pending")
                    .where(~_before(route_request.c.expires_at, now))
                    .order_by(route_request.c.created_at, route_request.c.request_id)
                )
                .mappings()
                .all()
            )
        return [
            PendingRequest(
                request_id=cast("str", row["request_id"]),
                leg=Leg(
                    cast("str", row["origin_location_id"]),
                    cast("str", row["destination_location_id"]),
                    cast("int", row["weekday"]),
                    cast("int", row["bucket"]),
                ),
                created_at=cast("datetime", row["created_at"]),
                expires_at=cast("datetime", row["expires_at"]),
            )
            for row in rows
        ]

    def due_requests(
        self, *, now: datetime, zone: tzinfo, limit: int = MAX_REQUESTS_PER_POLL
    ) -> list[PendingRequest]:
        """Return the pending requests Home Assistant can price right now.

        A request is due when its weekday equals the weekday of the next local
        occurrence of its bucket's start time (``due_weekday``). The others stay
        queued until their day.

        Args:
            now: Poll time (aware).
            zone: The household's time zone.
            limit: Most requests to return, oldest first.

        Returns:
            The due requests.

        Note:
            A naive ``now`` raises ``ValidationError``.
        """
        due = [
            request
            for request in self.pending(now=now)
            if request.leg.weekday == due_weekday(request.leg.bucket, now, zone)
        ]
        return due[:limit]

    def location_sources(
        self, location_ids: Collection[str]
    ) -> dict[str, LocationSource]:
        """Return the routing endpoints the given locations offer.

        Nothing is decrypted here.

        Args:
            location_ids: Location ids to look up.

        Returns:
            Location id to its zone id and protected address; ids with no row
            are absent.
        """
        if not location_ids:
            return {}
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        location.c.location_id,
                        location.c.ha_zone_entity_id,
                        location.c.address_ciphertext,
                    ).where(location.c.location_id.in_(sorted(location_ids)))
                )
                .mappings()
                .all()
            )
        return {
            cast("str", row["location_id"]): LocationSource(
                cast("str | None", row["ha_zone_entity_id"]),
                cast("str | None", row["address_ciphertext"]),
            )
            for row in rows
        }

    def record_results(
        self,
        answers: Sequence[RouteAnswer],
        *,
        now: datetime,
        before_commit: Callable[[Connection, BatchOutcome], None] | None = None,
    ) -> BatchOutcome:
        """Apply a batch of posted results in one transaction.

        Each item is judged on its own: an unknown or expired request id and a
        replay with a different value are rejected, an identical replay is
        unchanged, anything else fills the cache for the request's own leg,
        weekday and bucket (never the sender's) at the server's ``now``.

        Args:
            answers: Validated results, in posted order.
            now: Receipt time (aware); becomes ``observed_at``.
            before_commit: Called with the open connection and the outcome just
                before commit, for the audit row. If it raises, nothing is
                written.

        Returns:
            One outcome per answer.

        Raises:
            ValidationError: A naive ``now``, or a duration outside
                ``1..MAX_ROUTE_DURATION_SECONDS``. Nothing is written.

        Note:
            If the write lock is not granted in time, ``RouteRelayBusyError`` is
            raised and nothing is written.
        """
        _require_aware("now", now)
        for answer in answers:
            if not 1 <= answer.duration_seconds <= MAX_ROUTE_DURATION_SECONDS:
                msg = "duration_seconds is outside the storable range"
                raise ValidationError(msg, field="duration_seconds")
        with _write_locked(self._engine, self._lock_timeout_ms) as connection:
            outcome = BatchOutcome(
                tuple(_apply(connection, answer, now) for answer in answers)
            )
            if before_commit is not None:
                before_commit(connection, outcome)
        return outcome


def _apply(connection: Connection, answer: RouteAnswer, now: datetime) -> ItemOutcome:
    """Judge one posted result and, when it is acceptable, store it.

    Args:
        connection: The connection holding the write lock.
        answer: The result.
        now: Receipt time (aware).

    Returns:
        The item's outcome.
    """
    row = (
        connection.execute(
            select(route_request).where(route_request.c.request_id == answer.request_id)
        )
        .mappings()
        .first()
    )
    if row is None:
        return ItemOutcome(
            answer.request_id, ResultStatus.REJECTED, RejectReason.UNKNOWN_REQUEST
        )
    if row["status"] == "answered":
        if row["answered_duration_seconds"] == answer.duration_seconds:
            return ItemOutcome(answer.request_id, ResultStatus.UNCHANGED)
        return ItemOutcome(
            answer.request_id, ResultStatus.REJECTED, RejectReason.CONFLICT
        )
    if cast("datetime", row["expires_at"]) <= now:
        return ItemOutcome(
            answer.request_id, ResultStatus.REJECTED, RejectReason.EXPIRED
        )
    connection.execute(
        update(route_request)
        .where(route_request.c.request_id == answer.request_id)
        .values(
            status="answered",
            answered_duration_seconds=answer.duration_seconds,
            answered_at=now,
        )
    )
    values = {
        "origin_location_id": row["origin_location_id"],
        "destination_location_id": row["destination_location_id"],
        "weekday": row["weekday"],
        "bucket": row["bucket"],
        "duration_seconds": answer.duration_seconds,
        "observed_at": now,
    }
    connection.execute(
        sqlite_insert(route_cache)
        .values(values)
        .on_conflict_do_update(
            index_elements=[
                route_cache.c.origin_location_id,
                route_cache.c.destination_location_id,
                route_cache.c.weekday,
                route_cache.c.bucket,
            ],
            set_={
                "duration_seconds": answer.duration_seconds,
                "observed_at": now,
            },
        )
    )
    return ItemOutcome(answer.request_id, ResultStatus.ACCEPTED)
