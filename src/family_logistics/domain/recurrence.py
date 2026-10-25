"""Recurrence expansion: iCalendar objects to normalized occurrences.

One calendar object (a VCALENDAR with a series master plus its RECURRENCE-ID
overrides, or a single event) becomes one ``SourceEvent`` per occurrence that
overlaps the collection window:

- EXDATE removes occurrences; RDATE adds them.
- An override VEVENT (edited, moved or cancelled occurrence) replaces the
  generated occurrence and keeps the series identity through its original
  RECURRENCE-ID, so a moved lesson is the same occurrence, not a new one.
- An object holding only overrides (no RRULE/RDATE master, as when the account
  was invited to single instances) keys each one by its RECURRENCE-ID too.
  Only a plain single event gets the empty ``recurrence_id``.
- STATUS:CANCELLED occurrences are kept with ``status="cancelled"``: the engine
  needs to see that a commitment was cancelled, not just that it is missing.
- Times carrying a TZID are converted to UTC through that zone (DST-safe);
  floating times and all-day dates are interpreted in the configured zone.

#ASSUME: Data integrity: iCloud sends TZID-qualified times with VTIMEZONE
definitions and RECURRENCE-ID values that match generated occurrences.
#VERIFY with the sanitized real fixtures from M0 check 2.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, cast

import icalendar
import recurring_ical_events
import x_wr_timezone

from family_logistics.domain.events import SourceEvent
from family_logistics.domain.models import OccurrenceKind

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from family_logistics.domain.events import EventStatus

STATUSES: dict[str, EventStatus] = {
    "CONFIRMED": "confirmed",
    "TENTATIVE": "tentative",
    "CANCELLED": "cancelled",
}


class RecurrenceError(ValueError):
    """A calendar object could not be parsed or expanded."""


def recurrence_id_text(value: date | datetime, zone: ZoneInfo) -> str:
    """Render a RECURRENCE-ID as the occurrence-key string.

    Args:
        value: The RECURRENCE-ID value (date for all-day series).
        zone: Zone for floating datetimes.

    Returns:
        ``YYYY-MM-DD`` for a date, otherwise the UTC ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    if not isinstance(value, datetime):
        return value.isoformat()
    return _to_utc(value, zone).strftime("%Y-%m-%dT%H:%M:%SZ")


def occurrence_kind(event: SourceEvent) -> OccurrenceKind:
    """Classify an occurrence for the parser and confirmation rules.

    Args:
        event: A normalized occurrence.

    Returns:
        SINGLE, SERIES_INSTANCE or OVERRIDE.
    """
    if event.is_override:
        return OccurrenceKind.OVERRIDE
    if event.series_uid_has_rrule:
        return OccurrenceKind.SERIES_INSTANCE
    return OccurrenceKind.SINGLE


def _to_utc(value: datetime, zone: ZoneInfo) -> datetime:
    """Convert an aware or floating datetime to UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(UTC)


def _boundary(value: date | datetime, zone: ZoneInfo) -> datetime:
    """Convert a DTSTART/DTEND value (date or datetime) to UTC."""
    if isinstance(value, datetime):
        return _to_utc(value, zone)
    return datetime.combine(value, time.min, tzinfo=zone).astimezone(UTC)


def _prop(component: icalendar.Component, name: str) -> object:
    """Return a raw property value (icalendar's own typing is a wide union)."""
    return cast("object", component.get(name))


def _text(component: icalendar.Component, name: str) -> str | None:
    """Return a text property, or None when absent or blank."""
    value = _prop(component, name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decoded(component: icalendar.Component, name: str) -> object:
    """Return the decoded ``.dt`` value of a date/time property, or None."""
    value = _prop(component, name)
    return cast("object", getattr(value, "dt", None))


def _temporal(component: icalendar.Component, name: str) -> date | datetime | None:
    """Return a DATE or DATE-TIME property value, or None when absent."""
    value = _decoded(component, name)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    msg = f"{name} is not a date or date-time"
    raise RecurrenceError(msg)


def _required_temporal(component: icalendar.Component, name: str) -> date | datetime:
    """Return a mandatory DATE or DATE-TIME property value."""
    value = _temporal(component, name)
    if value is None:
        msg = f"{name} is missing"
        raise RecurrenceError(msg)
    return value


def _end(component: icalendar.Component, start: date | datetime) -> date | datetime:
    """Return DTEND, deriving it from DURATION or the RFC 5545 defaults.

    recurring-ical-events normally sets DTEND on every occurrence; the
    fallbacks keep a malformed or unusual object from crashing expansion.
    """
    dtend = _temporal(component, "DTEND")
    if dtend is not None:
        return dtend
    duration = _decoded(component, "DURATION")
    if isinstance(duration, timedelta):
        return start + duration
    if isinstance(start, datetime):
        return start
    return start + timedelta(days=1)


def _raw_hash(fields: dict[str, object]) -> str:
    """Hash the normalized occurrence content for change detection."""
    canonical = json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def expand_object(  # noqa: PLR0913 - keyword-only collection context
    ics: str,
    *,
    calendar_id: str,
    window_start: datetime,
    window_end: datetime,
    zone: ZoneInfo,
    etag: str | None,
    seen_at: datetime,
) -> tuple[SourceEvent, ...]:
    """Expand one calendar object into occurrences overlapping a window.

    Args:
        ics: Raw iCalendar text of one calendar object.
        calendar_id: Opaque calendar identifier.
        window_start: Window start (UTC, inclusive).
        window_end: Window end (UTC, exclusive).
        zone: Zone for floating times and all-day dates.
        etag: ETag of the object, if known.
        seen_at: Collection time (UTC).

    Returns:
        Occurrences sorted by start time.

    Raises:
        RecurrenceError: When the object cannot be parsed or expanded.
    """
    try:
        calendar = x_wr_timezone.to_standard(icalendar.Calendar.from_ical(ics))
        raw_events = [c for c in calendar.subcomponents if c.name == "VEVENT"]
        series = {
            str(_prop(c, "UID")) for c in raw_events if "RRULE" in c or "RDATE" in c
        }
        overrides = {
            (str(_prop(c, "UID")), recurrence_id_text(recurrence, zone))
            for c in raw_events
            if (recurrence := _temporal(c, "RECURRENCE-ID")) is not None
        }
        local_start = window_start.astimezone(zone)
        local_end = window_end.astimezone(zone)
        occurrences = recurring_ical_events.of(calendar).between(local_start, local_end)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        # RecurrenceError subclasses ValueError, so it is re-wrapped here too.
        msg = f"calendar object could not be expanded: {type(exc).__name__}"
        raise RecurrenceError(msg) from exc
    events = [
        _occurrence(
            component,
            calendar_id=calendar_id,
            zone=zone,
            etag=etag,
            seen_at=seen_at,
            series=series,
            overrides=overrides,
        )
        for component in occurrences
    ]
    inside = [e for e in events if e.start < window_end and e.end > window_start]
    return tuple(sorted(inside, key=lambda e: (e.start, e.uid, e.recurrence_id)))


def _occurrence(  # noqa: PLR0913 - keyword-only expansion context
    component: icalendar.Component,
    *,
    calendar_id: str,
    zone: ZoneInfo,
    etag: str | None,
    seen_at: datetime,
    series: set[str],
    overrides: set[tuple[str, str]],
) -> SourceEvent:
    """Normalize one expanded VEVENT occurrence."""
    uid = str(_prop(component, "UID"))
    start_value = _required_temporal(component, "DTSTART")
    all_day = not isinstance(start_value, datetime)
    start = _boundary(start_value, zone)
    end = max(_boundary(_end(component, start_value), zone), start)
    in_series = uid in series
    recurrence_id = ""
    # recurring-ical-events stamps a RECURRENCE-ID (its DTSTART) on every
    # occurrence, plain single events included, so the value only becomes the
    # key when the UID is a series or the object carries its own override
    # VEVENTs. A single event therefore keeps "" (stable when rescheduled), and
    # orphan overrides of one UID keep distinct keys.
    keyed = in_series or any(o_uid == uid for o_uid, _ in overrides)
    recurrence = _temporal(component, "RECURRENCE-ID")
    if keyed and recurrence is not None:
        recurrence_id = recurrence_id_text(recurrence, zone)
    status = STATUSES.get(
        (_text(component, "STATUS") or "CONFIRMED").upper(), "confirmed"
    )
    summary = _text(component, "SUMMARY") or ""
    location = _text(component, "LOCATION")
    description = _text(component, "DESCRIPTION")
    fields: dict[str, object] = {
        "uid": uid,
        "recurrence_id": recurrence_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": all_day,
        "summary": summary,
        "location": location,
        "description": description,
        "status": status,
    }
    return SourceEvent(
        calendar_id=calendar_id,
        uid=uid,
        recurrence_id=recurrence_id,
        start=start,
        end=end,
        all_day=all_day,
        summary=summary,
        location=location,
        description=description,
        status=status,
        etag=etag,
        raw_hash=_raw_hash(fields),
        is_override=(uid, recurrence_id) in overrides,
        series_uid_has_rrule=in_series,
        last_seen=seen_at,
    )
