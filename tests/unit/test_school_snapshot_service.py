"""School snapshot ingest service: validate, map, store and audit (ADR 0007)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fl_engine import NOW
from fl_ha_snapshot import (
    FRIDAY,
    SCHOOLS,
    as_json,
    bell,
    payload,
    seed_with_zones,
    snapshot_settings,
)
from sqlalchemy import func, select

from family_logistics.adapters.home_assistant.schemas import HaSnapshotPayload
from family_logistics.adapters.home_assistant.school_days import SnapshotMappingError
from family_logistics.core.database import create_sqlite_engine
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.db import migrations_api
from family_logistics.db.audit import AuditRepository
from family_logistics.db.schema import audit_log, school_day
from family_logistics.db.school_snapshots import (
    ReplaceStatus,
    SchoolSnapshotConflictError,
)
from family_logistics.domain.events import SchoolDayKind
from family_logistics.services.school_snapshot import (
    AUDIT_ACTOR,
    AUDIT_OBJECT_REF,
    MAX_FUTURE_SKEW,
    IngestRejectedError,
    SchoolIngestUnavailableError,
    SchoolSnapshotIngest,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    seed_with_zones(engine)
    yield engine
    engine.dispose()


def make_ingest(engine, audit_key, *, clock=lambda: NOW, **settings):
    return SchoolSnapshotIngest(
        engine, snapshot_settings(audit_key, **settings), clock=clock
    )


def parse(**overrides) -> HaSnapshotPayload:
    return HaSnapshotPayload.model_validate_json(as_json(payload(**overrides)))


def count(engine, table) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


class TestStored:
    def test_days_and_freshness_are_stored(self, engine, audit_key):
        ingest = make_ingest(engine, audit_key)
        ack = ingest.ingest(parse())
        assert ack.status is ReplaceStatus.STORED
        assert (ack.schools, ack.days) == (2, 3)
        assert ack.fetched_at == NOW - timedelta(minutes=1)
        assert ack.received_at == NOW
        latest = ingest.repository.latest()
        assert latest is not None
        assert [(d.person_id, d.kind) for d in latest.days] == [
            ("alex", SchoolDayKind.NORMAL),
            ("jordan", SchoolDayKind.NORMAL),
            ("sam", SchoolDayKind.NORMAL),
        ]
        assert {d.date for d in latest.days} == {FRIDAY}

    def test_the_ack_is_small_and_serializable(self, engine, audit_key):
        ack = make_ingest(engine, audit_key).ingest(parse())
        assert ack.as_dict() == {
            "status": "stored",
            "schools": 2,
            "days": 3,
            "fetched_at": (NOW - timedelta(minutes=1)).isoformat(),
            "received_at": NOW.isoformat(),
        }

    def test_a_fetch_slightly_in_the_future_is_clamped_to_now(self, engine, audit_key):
        ahead = NOW + timedelta(minutes=4)
        ack = make_ingest(engine, audit_key).ingest(parse(fetched_at=ahead.isoformat()))
        assert ack.fetched_at == NOW

    def test_the_window_runs_past_the_assessment_window(self, engine, audit_key):
        far = FRIDAY + timedelta(days=16)
        too_far = FRIDAY + timedelta(days=17)
        yesterday = FRIDAY - timedelta(days=1)
        middle = [bell(yesterday), bell(FRIDAY), bell(far), bell(too_far)]
        ingest = make_ingest(engine, audit_key)
        ingest.ingest(parse(middle=middle, high=[]))
        latest = ingest.repository.latest()
        assert latest is not None
        assert [d.date for d in latest.days] == [FRIDAY, far]

    def test_the_default_clock_is_the_utc_wall_clock(self, engine, audit_key):
        before = datetime.now(UTC)
        fetched = (before - timedelta(minutes=1)).isoformat()
        ingest = SchoolSnapshotIngest(engine, snapshot_settings(audit_key))
        ack = ingest.ingest(parse(fetched_at=fetched))
        assert before <= ack.received_at <= datetime.now(UTC)
        assert ack.received_at.utcoffset() == timedelta(0)


class TestIdempotency:
    def test_an_identical_repost_changes_nothing(self, engine, audit_key):
        first = make_ingest(engine, audit_key).ingest(parse())
        later = make_ingest(engine, audit_key, clock=lambda: NOW + timedelta(minutes=3))
        again = later.ingest(parse())
        assert again.status is ReplaceStatus.UNCHANGED
        assert (again.fetched_at, again.received_at) == (
            first.fetched_at,
            first.received_at,
        )
        assert count(engine, school_day) == 3

    def test_an_older_fetch_is_a_conflict(self, engine, audit_key):
        make_ingest(engine, audit_key).ingest(parse())
        older = (NOW - timedelta(hours=1)).isoformat()
        with pytest.raises(SchoolSnapshotConflictError):
            make_ingest(engine, audit_key).ingest(
                parse(fetched_at=older, middle=[bell(FRIDAY, end="12:00")])
            )


class TestAudit:
    def test_one_entry_per_ingest(self, engine, audit_key):
        ingest = make_ingest(engine, audit_key)
        ingest.ingest(parse(), correlation_id="req-1")
        ingest.ingest(parse(), correlation_id="req-2")
        settings = snapshot_settings(audit_key)
        entries = AuditRepository.from_settings(engine, settings).entries_for(
            AUDIT_OBJECT_REF
        )
        assert [(e.actor, e.action, e.correlation_id) for e in entries] == [
            (AUDIT_ACTOR, "ha_snapshot_stored", "req-1"),
            (AUDIT_ACTOR, "ha_snapshot_unchanged", "req-2"),
        ]
        assert all(e.new_hash for e in entries)

    def test_without_an_audit_key_nothing_is_stored(self, engine):
        with pytest.raises(SchoolIngestUnavailableError):
            make_ingest(engine, None).ingest(parse())
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0


class TestRejected:
    def test_fetched_at_too_far_ahead(self, engine, audit_key):
        ahead = NOW + MAX_FUTURE_SKEW + timedelta(seconds=1)
        with pytest.raises(IngestRejectedError) as caught:
            make_ingest(engine, audit_key).ingest(parse(fetched_at=ahead.isoformat()))
        assert caught.value.error_code == "FETCHED_AT_IN_FUTURE"
        assert count(engine, school_day) == 0

    def test_unknown_zone_stores_nothing(self, engine, audit_key):
        make_ingest(engine, audit_key).ingest(parse())
        newer = NOW.isoformat()
        with pytest.raises(IngestRejectedError) as caught:
            make_ingest(engine, audit_key).ingest(
                parse(
                    fetched_at=newer,
                    zones=["zone.home", "zone.private_unlisted"],
                    middle=[bell(FRIDAY, end="12:00", summary="Early Release")],
                )
            )
        assert caught.value.error_code == "UNKNOWN_ZONE"
        assert "private_unlisted" not in str(caught.value.to_dict())
        latest = make_ingest(engine, audit_key).repository.latest()
        assert latest is not None
        assert {d.kind for d in latest.days} == {SchoolDayKind.NORMAL}
        assert count(engine, audit_log) == 1

    def test_unknown_calendar_stores_nothing(self, engine, audit_key):
        body = payload()
        calendars = body["calendars"]
        assert isinstance(calendars, dict)
        calendars["calendar.private_unlisted"] = {"events": []}
        snapshot = HaSnapshotPayload.model_validate_json(as_json(body))
        with pytest.raises(SnapshotMappingError):
            make_ingest(engine, audit_key).ingest(snapshot)
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0


class TestUnavailable:
    def test_no_configured_school(self, engine, audit_key):
        with pytest.raises(SchoolIngestUnavailableError):
            make_ingest(engine, audit_key, ha_schools=[]).ingest(parse())

    @pytest.mark.parametrize(
        "school",
        [
            SCHOOLS[0] | {"location_id": "nowhere"},
            SCHOOLS[0] | {"person_ids": ["nobody"]},
        ],
        ids=["unknown-location", "unknown-person"],
    )
    def test_configuration_not_in_the_registry(self, engine, audit_key, school):
        ingest = make_ingest(engine, audit_key, ha_schools=[school, SCHOOLS[1]])
        with pytest.raises(SchoolIngestUnavailableError) as caught:
            ingest.ingest(parse())
        assert "nowhere" not in str(caught.value.to_dict())
        assert "nobody" not in str(caught.value.to_dict())
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0

    def test_unavailable_is_a_configuration_error(self):
        assert issubclass(SchoolIngestUnavailableError, ConfigurationError)


class TestClosures:
    def test_a_district_closure_reaches_every_school(self, engine, audit_key):
        closure = {"start": "2026-10-26", "end": "2026-10-27", "summary": "No School"}
        ingest = make_ingest(engine, audit_key)
        ingest.ingest(parse(district=[closure]))
        latest = ingest.repository.latest()
        assert latest is not None
        monday = [d for d in latest.days if d.date.isoformat() == "2026-10-26"]
        assert {d.kind for d in monday} == {SchoolDayKind.NO_SCHOOL}
        assert len(monday) == 3
