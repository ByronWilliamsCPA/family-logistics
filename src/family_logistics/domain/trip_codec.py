"""JSON-safe encoding of ``Trip`` for the previous-run snapshot.

Only the fields change impact needs to compare are kept in full. ``free_notes``
is dropped because it is free text that may name people or places and no rule
reads it from a stored trip. Decoding validates every field, so a corrupted
snapshot raises ``ValidationError`` instead of yielding a half-formed trip.
"""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn, cast

from family_logistics.core.exceptions import ValidationError
from family_logistics.domain.models import (
    Destination,
    DestinationKind,
    OccurrenceKind,
    PassengerKind,
    SourceEventKey,
    Trip,
    TripPassenger,
    TripStatus,
    TripType,
)

JsonObject = dict[str, object]


def _bad(name: str) -> NoReturn:
    """Raise the error for a malformed snapshot field (the value is not echoed)."""
    message = f"Stored trip field {name} is malformed"
    raise ValidationError(message, field=name)


def _text(data: JsonObject, name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        _bad(name)
    return value


def _optional_text(data: JsonObject, name: str) -> str | None:
    value = data.get(name)
    if value is not None and not isinstance(value, str):
        _bad(name)
    return value


def _object(data: JsonObject, name: str) -> JsonObject:
    value = data.get(name)
    if not isinstance(value, dict):
        _bad(name)
    return cast("JsonObject", value)


def _list(data: JsonObject, name: str) -> list[object]:
    value = data.get(name)
    if not isinstance(value, list):
        _bad(name)
    return cast("list[object]", value)


def _time(data: JsonObject, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(data, name))
    except ValueError:
        _bad(name)
    if parsed.tzinfo is None:
        _bad(name)
    return parsed


def _enum[E: (TripType, TripStatus, OccurrenceKind, PassengerKind, DestinationKind)](
    data: JsonObject, name: str, kind: type[E]
) -> E:
    try:
        return kind(_text(data, name))
    except ValueError:
        _bad(name)


def trip_to_json(trip: Trip) -> JsonObject:
    """Encode a trip for storage.

    Args:
        trip: The accepted trip.

    Returns:
        A JSON-safe mapping (no ``free_notes``).
    """
    return {
        "key": [trip.key.calendar_id, trip.key.uid, trip.key.recurrence_id],
        "trip_type": trip.trip_type.value,
        "status": trip.status.value,
        "driver_id": trip.driver_id,
        "pickup": trip.pickup,
        "passengers": [
            {
                "passenger_id": p.passenger_id,
                "kind": p.kind.value,
                "display_name": p.display_name,
                "destination": {
                    "raw": p.destination.raw,
                    "kind": p.destination.kind.value,
                },
            }
            for p in trip.passengers
        ],
        "stop_order": list(trip.stop_order) if trip.stop_order is not None else None,
        "start": trip.start.isoformat(),
        "end": trip.end.isoformat(),
        "confirm_code": trip.confirm_code,
        "occurrence": trip.occurrence.value,
    }


def _key(data: JsonObject) -> SourceEventKey:
    raw = _list(data, "key")
    if len(raw) != 3 or not all(isinstance(part, str) for part in raw[:2]):
        _bad("key")
    recurrence = raw[2]
    if recurrence is not None and not isinstance(recurrence, str):
        _bad("key")
    return SourceEventKey(cast("str", raw[0]), cast("str", raw[1]), recurrence)


def _passenger(raw: object) -> TripPassenger:
    if not isinstance(raw, dict):
        _bad("passengers")
    data = cast("JsonObject", raw)
    destination = _object(data, "destination")
    return TripPassenger(
        _text(data, "passenger_id"),
        _enum(data, "kind", PassengerKind),
        _text(data, "display_name"),
        Destination(
            _text(destination, "raw"), _enum(destination, "kind", DestinationKind)
        ),
    )


def trip_from_json(data: JsonObject) -> Trip:
    """Decode a stored trip.

    Args:
        data: Mapping produced by ``trip_to_json``.

    Returns:
        The trip, with empty ``free_notes``. A missing or malformed field
        raises ``ValidationError``.
    """
    stops = data.get("stop_order")
    if stops is not None and not (
        isinstance(stops, list)
        and all(isinstance(s, str) for s in cast("list[object]", stops))
    ):
        _bad("stop_order")
    return Trip(
        key=_key(data),
        trip_type=_enum(data, "trip_type", TripType),
        status=_enum(data, "status", TripStatus),
        driver_id=_text(data, "driver_id"),
        pickup=_text(data, "pickup"),
        passengers=tuple(_passenger(p) for p in _list(data, "passengers")),
        stop_order=tuple(cast("list[str]", stops)) if stops is not None else None,
        start=_time(data, "start"),
        end=_time(data, "end"),
        confirm_code=_optional_text(data, "confirm_code"),
        occurrence=_enum(data, "occurrence", OccurrenceKind),
    )
