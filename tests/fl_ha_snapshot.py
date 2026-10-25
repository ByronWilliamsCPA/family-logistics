"""Builders for Home Assistant school snapshot tests (fictional household).

Every entity ID here is invented for the fictional household; none is a real
calendar or zone.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from typing import TYPE_CHECKING

from fl_engine import LOCATIONS, NOW
from fl_service import make_settings, seed_registry

from family_logistics.db.registry import RegistryStore

if TYPE_CHECKING:
    from pydantic import SecretStr
    from sqlalchemy.engine import Engine

    from family_logistics.core.config import Settings

MIDDLE_BELL = "calendar.middle_school_bell"
HIGH_BELL = "calendar.high_school_bell"
DISTRICT = "calendar.district"
ZONES = {"home": "zone.home", "middle-school": "zone.middle_school"}
FRIDAY = date(2026, 10, 23)

SCHOOLS: list[dict[str, object]] = [
    {
        "location_id": "middle-school",
        "person_ids": ["sam"],
        "bell_calendars": [MIDDLE_BELL],
        "district_calendars": [DISTRICT],
    },
    {
        "location_id": "high-school",
        "person_ids": ["alex", "jordan"],
        "bell_calendars": [HIGH_BELL],
        "district_calendars": [DISTRICT],
    },
]


def snapshot_settings(audit_key: SecretStr | None, **overrides: object) -> Settings:
    """Settings with both fictional schools and an audit key."""
    values: dict[str, object] = {"ha_schools": SCHOOLS, "audit_key": audit_key}
    return make_settings(**(values | overrides))


def seed_with_zones(engine: Engine) -> None:
    """Seed the fictional household and give two locations an HA zone."""
    seed_registry(engine)
    with engine.begin() as connection:
        store = RegistryStore(connection)
        for location in LOCATIONS:
            zone = ZONES.get(location.location_id)
            if zone is not None:
                store.upsert_location(
                    replace(location, address=None, ha_zone_entity_id=zone)
                )


def bell(
    day: date, start: str = "08:00", end: str = "15:00", summary: str = "Regular Day"
):
    """A timed bell event in Pacific daylight time."""
    return {
        "start": f"{day.isoformat()}T{start}:00-07:00",
        "end": f"{day.isoformat()}T{end}:00-07:00",
        "summary": summary,
    }


def weekdays(first: date, count: int) -> list[date]:
    """The weekdays among ``count`` days from ``first``."""
    days = (first + timedelta(days=n) for n in range(count))
    return [day for day in days if day.weekday() < 5]


def payload(
    *,
    fetched_at: str | None = None,
    middle: list[dict[str, str]] | None = None,
    high: list[dict[str, str]] | None = None,
    district: list[dict[str, str]] | None = None,
    zones: list[str] | None = None,
    **extra: object,
) -> dict[str, object]:
    """A snapshot body: one regular Friday per school unless overridden."""
    body: dict[str, object] = {
        "fetched_at": fetched_at or (NOW - timedelta(minutes=1)).isoformat(),
        "calendars": {
            MIDDLE_BELL: {"events": middle if middle is not None else [bell(FRIDAY)]},
            HIGH_BELL: {"events": high if high is not None else [bell(FRIDAY)]},
            DISTRICT: {"events": district or []},
        },
        "zones": zones if zones is not None else sorted(ZONES.values()),
    }
    return body | extra


def as_json(body: dict[str, object]) -> bytes:
    return json.dumps(body).encode()
