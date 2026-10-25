"""Persistence for the Home Assistant school snapshot (ADR 0007).

Only the parsed ``SchoolDay`` rows and the snapshot's freshness are stored. The
raw ``calendar.get_events`` payload, its event text and the zone list never
reach the database (children's data, privacy class C).

#CRITICAL: Data integrity: ``replace`` swaps the whole snapshot (every
``school_day`` row and the single ``school_snapshot`` row) in one transaction,
together with whatever the caller's ``before_commit`` hook writes (the audit
row). Any failure, including a failing hook, leaves the previous snapshot
exactly as it was, so a failed ingest can never leave a partial school
calendar that reads as complete.
#VERIFY: tests/unit/test_school_snapshot_repo.py::TestReplace (a failing hook
rolls everything back; a conflict changes nothing).

#CRITICAL: Timing: the stored freshness is ``min(fetched_at, received_at)``,
so a snapshot sent with a Home Assistant clock ahead of the server never reads
fresher than the server's own clock, and the 26 hour staleness limit
(``domain.context.DEFAULT_SOURCE_MAX_AGES``) is measured from the fetch, not
from the arrival, so an old fetch delivered late cannot masquerade as fresh.
#VERIFY: tests/unit/test_school_snapshot_repo.py::TestFreshness; the database
CHECK ``ck_school_snapshot_fetched_not_after_received`` enforces the order.

#CRITICAL: Concurrency: ``replace`` reads the stored freshness and writes the
new snapshot under ``BEGIN IMMEDIATE`` (SQLite's write lock), so two ingests
arriving together are serialized and an older fetch can never overwrite a
newer one between the check and the write.
#VERIFY: the ordering rule is pinned by TestReplace; the lock itself follows
the pattern of ``migrations_api._locked_transaction``.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from sqlalchemy import delete, insert, select

from family_logistics.core.exceptions import BusinessLogicError, ValidationError
from family_logistics.db.schema import school_day, school_snapshot
from family_logistics.domain.context import HA_SNAPSHOT_SOURCE
from family_logistics.domain.events import SchoolDay, SchoolDayKind, SchoolSnapshot

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Sequence
    from datetime import date, datetime

    from sqlalchemy.engine import Connection, Engine

_SNAPSHOT_ID = 1
"""Primary key of the single ``school_snapshot`` row."""


class ReplaceStatus(StrEnum):
    """What ``SchoolSnapshotRepository.replace`` did."""

    STORED = "stored"
    UNCHANGED = "unchanged"


class SchoolSnapshotConflictError(BusinessLogicError):
    """A snapshot older than (or as old as, but different from) the stored one.

    Nothing is written. The message names no payload content.
    """


@dataclass(frozen=True, slots=True)
class SnapshotFreshness:
    """Freshness of the stored snapshot.

    Attributes:
        fetched_at: The earlier of Home Assistant's fetch time and the arrival
            time (UTC); the snapshot's age is measured from it.
        received_at: When the server stored it (UTC).
        content_hash: SHA-256 of the days and the fetch time as sent.
        day_count: Number of stored school days.
    """

    fetched_at: datetime
    received_at: datetime
    content_hash: str
    day_count: int


@dataclass(frozen=True, slots=True)
class ReplaceOutcome:
    """Result of one ``replace``.

    Attributes:
        status: Stored, or unchanged for an identical re-post.
        freshness: The freshness now stored (the earlier one when unchanged).
    """

    status: ReplaceStatus
    freshness: SnapshotFreshness


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


def _day_key(day: SchoolDay) -> tuple[date, str]:
    return (day.date, day.person_id)


def content_hash(days: Iterable[SchoolDay], fetched_at: datetime) -> str:
    """Digest a snapshot's content canonically (order-independent).

    A plain SHA-256 is enough here: the digest sits next to the very rows it
    summarises, so it reveals nothing they do not.

    Args:
        days: Parsed school days.
        fetched_at: Home Assistant's fetch time as sent (aware).

    Returns:
        64 lowercase hex characters.
    """
    rows = sorted(
        [
            day.date.isoformat(),
            day.person_id,
            day.location_id,
            day.start.isoformat(),
            day.end.isoformat(),
            day.kind.value,
        ]
        for day in days
    )
    canonical = json.dumps(
        {"days": rows, "fetched_at": fetched_at.isoformat()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@contextmanager
def _write_locked(engine: Engine) -> Generator[Connection]:
    """Yield a connection inside ``BEGIN IMMEDIATE``; commit on success.

    Args:
        engine: The application engine.

    Yields:
        Connection: A connection holding the database's write lock. Any
        exception rolls the whole transaction back.
    """
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        committed = False
        try:
            yield connection
            connection.commit()
            committed = True
        finally:
            if not committed:
                connection.rollback()


def _freshness_on(connection: Connection) -> SnapshotFreshness | None:
    row = (
        connection.execute(
            select(school_snapshot).where(school_snapshot.c.id == _SNAPSHOT_ID)
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return SnapshotFreshness(
        fetched_at=cast("datetime", row["fetched_at"]),
        received_at=cast("datetime", row["received_at"]),
        content_hash=cast("str", row["content_hash"]),
        day_count=cast("int", row["day_count"]),
    )


class SchoolSnapshotRepository:
    """Stores and reads the latest school snapshot.

    Args:
        engine: Database engine at the schema head.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def freshness(self) -> SnapshotFreshness | None:
        """Return the stored snapshot's freshness.

        Returns:
            The freshness, or None when no snapshot was ever stored.
        """
        with self._engine.connect() as connection:
            return _freshness_on(connection)

    def latest(self) -> SchoolSnapshot | None:
        """Return the stored snapshot as the assessment consumes it.

        Returns:
            The days ordered by day then person, with ``received_at`` set to
            the stored freshness (``min(fetched_at, received_at)``), or None
            when no snapshot was ever stored (never an empty snapshot, so a
            missing snapshot is never read as "no school").
        """
        with self._engine.connect() as connection:
            freshness = _freshness_on(connection)
            if freshness is None:
                return None
            rows = connection.execute(
                select(school_day).order_by(school_day.c.day, school_day.c.person_id)
            ).mappings()
            days = tuple(
                SchoolDay(
                    person_id=cast("str", row["person_id"]),
                    date=cast("date", row["day"]),
                    location_id=cast("str", row["location_id"]),
                    start=cast("datetime", row["start_utc"]),
                    end=cast("datetime", row["end_utc"]),
                    kind=SchoolDayKind(cast("str", row["kind"])),
                    source=HA_SNAPSHOT_SOURCE,
                )
                for row in rows
            )
        return SchoolSnapshot(days=days, received_at=freshness.fetched_at)

    def replace(
        self,
        days: Sequence[SchoolDay],
        *,
        fetched_at: datetime,
        received_at: datetime,
        before_commit: Callable[[Connection, ReplaceOutcome], None] | None = None,
    ) -> ReplaceOutcome:
        """Replace the stored snapshot with ``days``, atomically.

        An identical re-post (same days and same ``fetched_at``) writes nothing
        and reports ``UNCHANGED``. A snapshot whose freshness is older than
        the stored one, or equal to it with different content, is refused.

        Args:
            days: Parsed school days; at most one per person and day.
            fetched_at: Home Assistant's fetch time as sent (aware).
            received_at: Server time of arrival (aware).
            before_commit: Called with the open connection and the outcome
                just before commit (also when unchanged), for the audit row.
                If it raises, nothing is written.

        Returns:
            What was done and the freshness now stored.

        Raises:
            ValidationError: A naive timestamp, or two days for one person and
                day. Nothing is written.
            SchoolSnapshotConflictError: The snapshot is older than the stored
                one. Nothing is written.
        """
        _require_aware("fetched_at", fetched_at)
        _require_aware("received_at", received_at)
        keys = [_day_key(day) for day in days]
        if len(set(keys)) != len(keys):
            msg = "a school snapshot holds at most one day per person and date"
            raise ValidationError(msg, field="days")
        digest = content_hash(days, fetched_at)
        incoming = SnapshotFreshness(
            fetched_at=min(fetched_at, received_at),
            received_at=received_at,
            content_hash=digest,
            day_count=len(days),
        )
        with _write_locked(self._engine) as connection:
            stored = _freshness_on(connection)
            if stored is not None and stored.content_hash == digest:
                outcome = ReplaceOutcome(ReplaceStatus.UNCHANGED, stored)
            else:
                if stored is not None and incoming.fetched_at <= stored.fetched_at:
                    msg = "school snapshot is not newer than the stored one"
                    raise SchoolSnapshotConflictError(msg)
                self._write(connection, days, incoming)
                outcome = ReplaceOutcome(ReplaceStatus.STORED, incoming)
            if before_commit is not None:
                before_commit(connection, outcome)
        return outcome

    @staticmethod
    def _write(
        connection: Connection,
        days: Sequence[SchoolDay],
        freshness: SnapshotFreshness,
    ) -> None:
        connection.execute(delete(school_day))
        connection.execute(delete(school_snapshot))
        connection.execute(
            insert(school_snapshot).values(
                id=_SNAPSHOT_ID,
                fetched_at=freshness.fetched_at,
                received_at=freshness.received_at,
                content_hash=freshness.content_hash,
                day_count=freshness.day_count,
            )
        )
        if days:
            connection.execute(
                insert(school_day),
                [
                    {
                        "person_id": day.person_id,
                        "day": day.date,
                        "location_id": day.location_id,
                        "start_utc": day.start,
                        "end_utc": day.end,
                        "kind": day.kind.value,
                    }
                    for day in days
                ],
            )


__all__ = [
    "ReplaceOutcome",
    "ReplaceStatus",
    "SchoolSnapshotConflictError",
    "SchoolSnapshotRepository",
    "SnapshotFreshness",
    "content_hash",
]
