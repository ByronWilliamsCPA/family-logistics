"""Home Assistant school snapshot ingest (ADR 0007).

Validates a parsed snapshot against the clock, ``FL_HA_SCHOOLS`` and the
registry, maps it to school days, and replaces the stored snapshot together
with one audit row, all in one transaction. Only the parsed school days and
the snapshot's freshness are stored; event text and zones never are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from sqlalchemy import select

from family_logistics.adapters.home_assistant.school_days import (
    DayWindow,
    map_school_days,
)
from family_logistics.core.exceptions import ConfigurationError, ValidationError
from family_logistics.db.audit import record_on
from family_logistics.db.schema import location, person
from family_logistics.db.school_snapshots import (
    ReplaceHooks,
    ReplaceOutcome,
    ReplaceStatus,
    SchoolSnapshotRepository,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Connection, Engine

    from family_logistics.adapters.home_assistant.schemas import HaSnapshotPayload
    from family_logistics.core.config import Settings

__all__ = [
    "AUDIT_ACTOR",
    "AUDIT_OBJECT_REF",
    "DAYS_BEYOND_ASSESSMENT",
    "MAX_FUTURE_SKEW",
    "IngestAck",
    "IngestRejectedError",
    "SchoolIngestUnavailableError",
    "SchoolSnapshotIngest",
]

# #CRITICAL: timing: Home Assistant's clock may run ahead of the server's. A
# fetch more than this far in the future is refused; a smaller skew is
# accepted and the stored freshness is clamped to the server's clock
# (min(fetched_at, now)), so a snapshot never reads fresher than it is.
# #VERIFY: TestRejected.test_fetched_at_too_far_ahead and
# TestStored.test_a_fetch_slightly_in_the_future_is_clamped_to_now.
MAX_FUTURE_SKEW = timedelta(minutes=5)
"""Largest accepted lead of ``fetched_at`` over the server clock."""

DAYS_BEYOND_ASSESSMENT = 2
"""Local days kept after the assessment window (``FL_COLLECT_DAYS_AHEAD``).

A snapshot stays usable for up to 26 hours (``DEFAULT_SOURCE_MAX_AGES``), so a
run on the next day still finds every day of its own window."""

AUDIT_ACTOR = "home_assistant"
AUDIT_OBJECT_REF = "school_snapshot"


class IngestRejectedError(ValidationError):
    """The snapshot is refused (HTTP 422); the message never echoes input.

    Error codes: ``FETCHED_AT_IN_FUTURE``, ``EMPTY_SNAPSHOT``,
    ``UNKNOWN_ZONE``.
    """


class SchoolIngestUnavailableError(ConfigurationError):
    """The app cannot accept a snapshot as configured (HTTP 503).

    No school is configured, no audit key is set, or ``FL_HA_SCHOOLS`` names
    a location or person the registry does not hold.
    """


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IngestAck:
    """What an ingest did, as acknowledged to Home Assistant.

    Attributes:
        status: Stored, or unchanged for an identical re-post.
        schools: Configured schools covered by the snapshot.
        days: School-day rows in the stored snapshot.
        fetched_at: Stored freshness (``min(fetched_at, received_at)``).
        received_at: Server time the stored snapshot arrived.
    """

    status: ReplaceStatus
    schools: int
    days: int
    fetched_at: datetime
    received_at: datetime

    def as_dict(self) -> dict[str, object]:
        """Return the acknowledgement body (counts and freshness only).

        Returns:
            JSON-ready mapping.
        """
        return {
            "status": self.status.value,
            "schools": self.schools,
            "days": self.days,
            "fetched_at": self.fetched_at.isoformat(),
            "received_at": self.received_at.isoformat(),
        }


class SchoolSnapshotIngest:
    """Accepts Home Assistant school snapshots.

    Args:
        engine: Database engine at the schema head.
        settings: Application settings (``ha_schools``, ``timezone``,
            ``collect_days_ahead``, the audit key).
        clock: Returns the current time (UTC); tests inject a fixed clock.
    """

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._repo = SchoolSnapshotRepository(engine)

    @property
    def repository(self) -> SchoolSnapshotRepository:
        """Return the snapshot repository."""
        return self._repo

    def ingest(
        self, snapshot: HaSnapshotPayload, *, correlation_id: str | None = None
    ) -> IngestAck:
        """Validate, map and store one snapshot with its audit row.

        Args:
            snapshot: The validated payload.
            correlation_id: Request correlation ID for the audit row.

        Returns:
            The acknowledgement.

        Raises:
            SchoolIngestUnavailableError: No school or audit key is configured,
                or the configuration does not match the registry.
            IngestRejectedError: ``fetched_at`` is too far ahead, no
                configured calendar has an event in the window, or a zone is
                not a registry location's zone.

        Note:
            ``SnapshotMappingError`` (a calendar outside or missing from the
            configuration), ``SchoolSnapshotConflictError`` (an older
            snapshot than the stored one) and ``SchoolSnapshotBusyError``
            (the write lock was not granted in time) propagate unchanged.
            The registry and zone checks run under the write lock before the
            comparison with the stored snapshot, so a reference error wins
            over a conflict. Nothing is stored and no audit row is written on
            any error.
        """
        settings = self._settings
        if not settings.ha_schools:
            msg = "no school is configured for the Home Assistant snapshot"
            raise SchoolIngestUnavailableError(msg, error_code="NO_SCHOOLS")
        if settings.audit_key is None:
            msg = "FL_AUDIT_KEY is required to accept a school snapshot"
            raise SchoolIngestUnavailableError(msg, error_code="NO_AUDIT_KEY")
        now = self._clock()
        if snapshot.fetched_at > now + MAX_FUTURE_SKEW:
            msg = "fetched_at is too far in the future"
            raise IngestRejectedError(msg, error_code="FETCHED_AT_IN_FUTURE")
        zone = ZoneInfo(settings.timezone)
        today = now.astimezone(zone).date()
        last = today + timedelta(
            days=settings.collect_days_ahead + DAYS_BEYOND_ASSESSMENT
        )
        days = map_school_days(
            snapshot, settings.ha_schools, tz=zone, window=DayWindow(today, last)
        )
        # #CRITICAL: external resources: a Home Assistant calendar entity that
        # is unavailable returns no events, and an outage hits every calendar
        # at once. A snapshot in which no configured calendar has a single
        # event overlapping the window (after clipping) is refused, so the
        # stored snapshot is kept and goes stale (DATA-011) rather than being
        # replaced by an empty one that reads as "nothing known". Every event
        # that overlaps the window yields a row per configured person, so an
        # empty mapping is exactly that outage signature; a break that spans
        # the window still yields rows and is accepted.
        # #VERIFY: TestEmptySnapshot in test_school_snapshot_service.py.
        if not days:
            msg = "no configured calendar has an event in the snapshot window"
            raise IngestRejectedError(msg, error_code="EMPTY_SNAPSHOT")
        acks: list[IngestAck] = []

        def before_compare(connection: Connection) -> None:
            self._check_registry(connection, snapshot.zones)

        def before_commit(connection: Connection, outcome: ReplaceOutcome) -> None:
            ack = IngestAck(
                status=outcome.status,
                schools=len(settings.ha_schools),
                days=outcome.freshness.day_count,
                fetched_at=outcome.freshness.fetched_at,
                received_at=outcome.freshness.received_at,
            )
            record_on(
                connection,
                key=settings.audit_key,
                key_version=settings.audit_key_version,
                actor=AUDIT_ACTOR,
                object_ref=AUDIT_OBJECT_REF,
                action=f"ha_snapshot_{outcome.status.value}",
                new_state=ack.as_dict(),
                correlation_id=correlation_id,
            )
            acks.append(ack)

        self._repo.replace_with(
            days,
            fetched_at=snapshot.fetched_at,
            received_at=now,
            hooks=ReplaceHooks(
                before_compare=before_compare, before_commit=before_commit
            ),
        )
        return acks[0]

    def _check_registry(self, connection: Connection, zones: tuple[str, ...]) -> None:
        """Check the configuration and the zones against the registry.

        Runs inside the snapshot's write transaction, before the comparison
        with the stored snapshot, so a failure here rolls the whole ingest
        back and is reported ahead of a conflict.

        Args:
            connection: The open write connection.
            zones: Zone entity IDs from the payload.

        Raises:
            SchoolIngestUnavailableError: A configured location or person is
                not in the registry.
            IngestRejectedError: A zone is not any registry location's zone.
        """
        # #CRITICAL: security: zones and configured IDs are checked against
        # the registry inside the write transaction, so an unknown zone or a
        # stale configuration stores nothing (no partial snapshot, no audit
        # row). Errors name no zone, location or person.
        # #VERIFY: TestRejected.test_unknown_zone_stores_nothing and
        # TestUnavailable.test_configuration_not_in_the_registry.
        rows = connection.execute(
            select(location.c.location_id, location.c.ha_zone_entity_id)
        ).mappings()
        location_ids: set[str] = set()
        known_zones: set[str] = set()
        for row in rows:
            location_ids.add(cast("str", row["location_id"]))
            zone = cast("str | None", row["ha_zone_entity_id"])
            if zone is not None:
                known_zones.add(zone)
        people = connection.execute(select(person.c.person_id)).mappings()
        person_ids = {cast("str", row["person_id"]) for row in people}
        schools = self._settings.ha_schools
        if any(s.location_id not in location_ids for s in schools) or any(
            p not in person_ids for s in schools for p in s.person_ids
        ):
            msg = "FL_HA_SCHOOLS names a location or person the registry lacks"
            raise SchoolIngestUnavailableError(msg, error_code="REGISTRY_MISMATCH")
        if not set(zones) <= known_zones:
            msg = "the snapshot holds a zone that no registry location uses"
            raise IngestRejectedError(msg, error_code="UNKNOWN_ZONE")
