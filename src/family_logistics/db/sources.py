"""Repositories for calendar sources, collected occurrences and sync state.

Occurrences are never physically removed. An occurrence missing from a
*complete* collection of its calendar window gets ``deleted_at`` so change
impact can still compare against the commitment it used to carry. An
incomplete collection never marks anything deleted.

#CRITICAL: Data integrity: a failed or partial collection must never look
like an empty calendar. ``SourceEventRepository.apply`` only marks deletions
when ``CollectionResult.complete`` is True.
#ASSUME: Timing: SQLite stores ``DateTime(timezone=True)`` values without
an offset; every value written here is UTC and is re-tagged as UTC on read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, insert, select, update

from family_logistics.core.exceptions import BusinessLogicError
from family_logistics.db.schema import calendar_source, source_event, sync_state
from family_logistics.domain.events import CalendarKind, CalendarSource, SourceEvent

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sqlalchemy.engine import Connection, Engine

    from family_logistics.domain.events import CollectionResult, EventStatus

MAX_ERROR_LENGTH = 500


class UnregisteredCalendarError(BusinessLogicError):
    """Raised when a calendar that needs a registry owner is not registered."""


def _utc(value: object) -> datetime | None:
    """Re-tag a datetime read from SQLite as UTC."""
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _required_utc(value: object) -> datetime:
    """Re-tag a non-null datetime column as UTC."""
    result = _utc(value)
    if result is None:
        msg = "expected a datetime value"
        raise TypeError(msg)
    return result


def _optional_text(value: object) -> str | None:
    """Return a string column value or None."""
    return value if isinstance(value, str) else None


def _int(value: object) -> int:
    """Return an integer column value."""
    if isinstance(value, int):
        return value
    msg = "expected an integer value"
    raise TypeError(msg)


def _mapping(row: object) -> dict[str, object]:
    """Copy a SQLAlchemy row mapping into a plain typed dict."""
    return dict(cast("Mapping[str, object]", row))


@dataclass(frozen=True, slots=True)
class SyncState:
    """Stored change-detection state for one calendar.

    Attributes:
        calendar_id: Calendar identifier.
        sync_token: Last RFC 6578 sync token, if any.
        ctag: Last CalendarServer ctag, if any.
        last_full_sync: When the last complete window collection finished.
    """

    calendar_id: str
    sync_token: str | None
    ctag: str | None
    last_full_sync: datetime | None


@dataclass(frozen=True, slots=True)
class ApplySummary:
    """What ``SourceEventRepository.apply`` changed.

    Attributes:
        inserted: New occurrences.
        updated: Occurrences whose content changed.
        unchanged: Occurrences seen again with the same content.
        deleted: Occurrences marked deleted (complete results only).
    """

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0


class CalendarSourceRepository:
    """Calendar sources the collector reads.

    Kind and owner are set by the registry (seed data). Discovery only adds the
    shared transportation calendar, which needs no owner; an individual's
    calendar needs its owner (a database CHECK enforces it), so it must be
    registered through the registry seed first. Discovery never changes an
    existing row's kind or owner.

    Args:
        engine: Database engine.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def all(self) -> tuple[CalendarSource, ...]:
        """Return every calendar source.

        Returns:
            Sources ordered by display name.
        """
        query = select(calendar_source).order_by(calendar_source.c.display_name)
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return tuple(_source_from_row(_mapping(row)) for row in rows)

    def get(self, calendar_id: str) -> CalendarSource | None:
        """Return one calendar source.

        Args:
            calendar_id: Calendar identifier.

        Returns:
            The source, or None.
        """
        query = select(calendar_source).where(
            calendar_source.c.calendar_id == calendar_id
        )
        with self._engine.connect() as connection:
            row = connection.execute(query).mappings().first()
        return None if row is None else _source_from_row(_mapping(row))

    def ensure(
        self, calendar_id: str, display_name: str, kind: CalendarKind
    ) -> CalendarSource:
        """Insert a discovered calendar unless it already exists.

        An existing row keeps its kind, owner and enabled flag; only the
        display name is refreshed. A new row is inserted only for a kind that
        needs no owner.

        Args:
            calendar_id: Calendar identifier.
            display_name: Name reported by the server.
            kind: Kind to use for a new row.

        Returns:
            The stored source.

        Raises:
            UnregisteredCalendarError: If the calendar is new and its kind
                needs an owner (``person``): the registry seed must add it.
            RuntimeError: If the row cannot be read back after the insert.
        """
        with self._engine.begin() as connection:
            exists = connection.execute(
                select(calendar_source.c.calendar_id).where(
                    calendar_source.c.calendar_id == calendar_id
                )
            ).first()
            if exists is None:
                if kind is CalendarKind.PERSON:
                    msg = (
                        "calendar is not registered: add it with its owner to "
                        "the registry seed (`registry load`)"
                    )
                    raise UnregisteredCalendarError(msg)
                connection.execute(
                    insert(calendar_source).values(
                        calendar_id=calendar_id,
                        display_name=display_name,
                        kind=kind.value,
                        person_id=None,
                        enabled=True,
                    )
                )
            else:
                connection.execute(
                    update(calendar_source)
                    .where(calendar_source.c.calendar_id == calendar_id)
                    .values(display_name=display_name)
                )
        source = self.get(calendar_id)
        if source is None:  # pragma: no cover - written in the same call
            msg = "calendar source vanished after insert"
            raise RuntimeError(msg)
        return source

    def record_success(self, calendar_id: str, when: datetime) -> None:
        """Record a complete collection.

        Args:
            calendar_id: Calendar identifier.
            when: Collection time (UTC).
        """
        self._set(calendar_id, last_success=when, last_error=None)

    def record_failure(self, calendar_id: str, error: str) -> None:
        """Record a failed or incomplete collection (no secrets in ``error``).

        Args:
            calendar_id: Calendar identifier.
            error: Short description of the failure.
        """
        self._set(calendar_id, last_error=error[:MAX_ERROR_LENGTH])

    def _set(self, calendar_id: str, **values: object) -> None:
        """Update columns of one source."""
        with self._engine.begin() as connection:
            connection.execute(
                update(calendar_source)
                .where(calendar_source.c.calendar_id == calendar_id)
                .values(**values)
            )


def _source_from_row(row: Mapping[str, object]) -> CalendarSource:
    """Build a ``CalendarSource`` from a row."""
    return CalendarSource(
        calendar_id=str(row["calendar_id"]),
        display_name=str(row["display_name"]),
        kind=CalendarKind(str(row["kind"])),
        person_id=_optional_text(row["person_id"]),
        enabled=bool(row["enabled"]),
        last_success=_utc(row["last_success"]),
        last_error=_optional_text(row["last_error"]),
    )


class SyncStateRepository:
    """Per-calendar ctag, sync token and last complete collection.

    Args:
        engine: Database engine.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, calendar_id: str) -> SyncState | None:
        """Return the stored state of a calendar.

        Args:
            calendar_id: Calendar identifier.

        Returns:
            The state, or None when the calendar was never collected.
        """
        query = select(sync_state).where(sync_state.c.calendar_id == calendar_id)
        with self._engine.connect() as connection:
            found = connection.execute(query).mappings().first()
        if found is None:
            return None
        row = _mapping(found)
        return SyncState(
            calendar_id=str(row["calendar_id"]),
            sync_token=_optional_text(row["sync_token"]),
            ctag=_optional_text(row["ctag"]),
            last_full_sync=_utc(row["last_full_sync"]),
        )

    def save(self, state: SyncState) -> None:
        """Insert or replace the state of a calendar.

        Args:
            state: The new state.
        """
        values = {
            "sync_token": state.sync_token,
            "ctag": state.ctag,
            "last_full_sync": state.last_full_sync,
        }
        with self._engine.begin() as connection:
            changed = connection.execute(
                update(sync_state)
                .where(sync_state.c.calendar_id == state.calendar_id)
                .values(**values)
            ).rowcount
            if not changed:
                connection.execute(
                    insert(sync_state).values(calendar_id=state.calendar_id, **values)
                )


class SourceEventRepository:
    """Collected occurrences, keyed by ``(calendar_id, uid, recurrence_id)``.

    Args:
        engine: Database engine.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def apply(self, result: CollectionResult, now: datetime) -> ApplySummary:
        """Store a collection result.

        Every returned occurrence is inserted or updated (and un-deleted if it
        reappears). Only for a complete result, occurrences of the calendar
        that overlap the window but were not returned are marked deleted.

        Args:
            result: One calendar's collection result.
            now: Current time (UTC).

        Returns:
            Counts of what changed.
        """
        inserted = updated = unchanged = deleted = 0
        calendar_id = result.source.calendar_id
        with self._engine.begin() as connection:
            existing = _existing_hashes(connection, calendar_id)
            seen: set[tuple[str, str]] = set()
            for event in result.events:
                identity = (event.uid, event.recurrence_id)
                seen.add(identity)
                previous = existing.get(identity)
                if previous is None:
                    connection.execute(insert(source_event).values(**_row(event, now)))
                    inserted += 1
                    continue
                row_id, raw_hash, was_deleted = previous
                values = _row(event, now)
                del values["first_seen"]
                values["deleted_at"] = None
                connection.execute(
                    update(source_event)
                    .where(source_event.c.id == row_id)
                    .values(**values)
                )
                if raw_hash == event.raw_hash and not was_deleted:
                    unchanged += 1
                else:
                    updated += 1
            if result.complete:
                deleted = _mark_missing(connection, result, seen, now)
        return ApplySummary(inserted, updated, unchanged, deleted)

    def active(
        self,
        start: datetime,
        end: datetime,
        calendar_ids: Iterable[str] | None = None,
    ) -> tuple[SourceEvent, ...]:
        """Return non-deleted occurrences overlapping a window.

        Args:
            start: Window start (UTC).
            end: Window end (UTC).
            calendar_ids: Restrict to these calendars; all when None.

        Returns:
            Occurrences ordered by start time.
        """
        return self._query(start, end, calendar_ids, deleted=False)

    def deleted(
        self,
        start: datetime,
        end: datetime,
        calendar_ids: Iterable[str] | None = None,
    ) -> tuple[SourceEvent, ...]:
        """Return deleted occurrences overlapping a window (for change impact).

        Args:
            start: Window start (UTC).
            end: Window end (UTC).
            calendar_ids: Restrict to these calendars; all when None.

        Returns:
            Occurrences ordered by start time.
        """
        return self._query(start, end, calendar_ids, deleted=True)

    def _query(
        self,
        start: datetime,
        end: datetime,
        calendar_ids: Iterable[str] | None,
        *,
        deleted: bool,
    ) -> tuple[SourceEvent, ...]:
        """Select occurrences overlapping a window."""
        conditions = [
            source_event.c.start_utc < end,
            source_event.c.end_utc > start,
            source_event.c.deleted_at.is_not(None)
            if deleted
            else source_event.c.deleted_at.is_(None),
        ]
        if calendar_ids is not None:
            conditions.append(source_event.c.calendar_id.in_(list(calendar_ids)))
        query = (
            select(source_event)
            .where(and_(*conditions))
            .order_by(source_event.c.start_utc, source_event.c.uid)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return tuple(_event_from_row(_mapping(row)) for row in rows)


def _existing_hashes(
    connection: Connection, calendar_id: str
) -> dict[tuple[str, str], tuple[int, str, bool]]:
    """Map ``(uid, recurrence_id)`` to ``(row id, raw hash, deleted)``."""
    rows = connection.execute(
        select(
            source_event.c.id,
            source_event.c.uid,
            source_event.c.recurrence_id,
            source_event.c.raw_hash,
            source_event.c.deleted_at,
        ).where(source_event.c.calendar_id == calendar_id)
    ).mappings()
    existing: dict[tuple[str, str], tuple[int, str, bool]] = {}
    for raw in rows:
        row = _mapping(raw)
        identity = (str(row["uid"]), str(row["recurrence_id"]))
        existing[identity] = (
            _int(row["id"]),
            str(row["raw_hash"]),
            row["deleted_at"] is not None,
        )
    return existing


def _mark_missing(
    connection: Connection,
    result: CollectionResult,
    seen: set[tuple[str, str]],
    now: datetime,
) -> int:
    """Mark window occurrences of a calendar that were not returned as deleted."""
    candidates = connection.execute(
        select(
            source_event.c.id, source_event.c.uid, source_event.c.recurrence_id
        ).where(
            source_event.c.calendar_id == result.source.calendar_id,
            source_event.c.deleted_at.is_(None),
            source_event.c.start_utc < result.window_end,
            source_event.c.end_utc > result.window_start,
        )
    ).mappings()
    missing: list[int] = []
    for raw in candidates:
        row = _mapping(raw)
        if (str(row["uid"]), str(row["recurrence_id"])) not in seen:
            missing.append(_int(row["id"]))
    if missing:
        connection.execute(
            update(source_event)
            .where(source_event.c.id.in_(missing))
            .values(deleted_at=now)
        )
    return len(missing)


def _row(event: SourceEvent, now: datetime) -> dict[str, object]:
    """Column values for an occurrence."""
    return {
        "calendar_id": event.calendar_id,
        "uid": event.uid,
        "recurrence_id": event.recurrence_id,
        "start_utc": event.start,
        "end_utc": event.end,
        "all_day": event.all_day,
        "summary": event.summary,
        "location": event.location,
        "description": event.description,
        "status": event.status,
        "etag": event.etag,
        "raw_hash": event.raw_hash,
        "is_override": event.is_override,
        "series_uid_has_rrule": event.series_uid_has_rrule,
        "first_seen": now,
        "last_seen": event.last_seen,
    }


def _event_from_row(row: Mapping[str, object]) -> SourceEvent:
    """Build a ``SourceEvent`` from a row."""
    return SourceEvent(
        calendar_id=str(row["calendar_id"]),
        uid=str(row["uid"]),
        recurrence_id=str(row["recurrence_id"]),
        start=_required_utc(row["start_utc"]),
        end=_required_utc(row["end_utc"]),
        all_day=bool(row["all_day"]),
        summary=str(row["summary"] or ""),
        location=_optional_text(row["location"]),
        description=_optional_text(row["description"]),
        status=cast("EventStatus", str(row["status"])),
        etag=_optional_text(row["etag"]),
        raw_hash=str(row["raw_hash"]),
        is_override=bool(row["is_override"]),
        series_uid_has_rrule=bool(row["series_uid_has_rrule"]),
        last_seen=_required_utc(row["last_seen"]),
    )
