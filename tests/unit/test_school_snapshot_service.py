"""School snapshot ingest service: validate, map, store and audit (ADR 0007)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fl_engine import NOW, default, routes
from fl_factories import CALENDAR
from fl_ha_snapshot import (
    FRIDAY,
    SCHOOLS,
    as_json,
    bell,
    payload,
    seed_with_zones,
    snapshot_settings,
    weekdays,
)
from fl_service import collect
from sqlalchemy import func, select

from family_logistics.adapters.home_assistant.schemas import HaSnapshotPayload
from family_logistics.adapters.home_assistant.school_days import SnapshotMappingError
from family_logistics.core.database import create_sqlite_engine
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.db import migrations_api
from family_logistics.db.audit import AuditRepository, state_hash
from family_logistics.db.registry import RegistryStore
from family_logistics.db.schema import audit_log, school_day
from family_logistics.db.school_snapshots import (
    ReplaceStatus,
    SchoolSnapshotConflictError,
    SchoolSnapshotRepository,
)
from family_logistics.domain.context import (
    DEFAULT_SOURCE_MAX_AGES,
    HA_SNAPSHOT_SOURCE,
)
from family_logistics.domain.events import (
    SchoolDayKind,
    TransportDirection,
    TransportMode,
)
from family_logistics.services.assessments import AssessmentService
from family_logistics.services.school_snapshot import (
    AUDIT_ACTOR,
    AUDIT_OBJECT_REF,
    DAYS_BEYOND_ASSESSMENT,
    MAX_FUTURE_SKEW,
    IngestErrorCode,
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
            "schools_without_days": [],
            "fetched_at": (NOW - timedelta(minutes=1)).isoformat(),
            "received_at": NOW.isoformat(),
        }

    def test_a_fetch_exactly_at_the_skew_limit_is_accepted(self, engine, audit_key):
        limit = NOW + MAX_FUTURE_SKEW
        ack = make_ingest(engine, audit_key).ingest(parse(fetched_at=limit.isoformat()))
        assert ack.status is ReplaceStatus.STORED
        assert ack.fetched_at == NOW

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
        values = snapshot_settings(audit_key)
        today = before.astimezone(ZoneInfo(values.timezone)).date()
        holiday = {
            "start": (today - timedelta(days=1)).isoformat(),
            "end": (today + timedelta(days=2)).isoformat(),
            "summary": "Winter Break",
        }
        ingest = SchoolSnapshotIngest(engine, values)
        ack = ingest.ingest(parse(fetched_at=fetched, district=[holiday]))
        assert before <= ack.received_at <= datetime.now(UTC)
        assert ack.received_at.utcoffset() == timedelta(0)


class TestWindow:
    """The window starts on the household's local day, not the UTC day."""

    def test_a_pacific_evening_keeps_todays_school_day(self, engine, audit_key):
        # Friday 20:00 Pacific is already Saturday in UTC.
        evening = datetime(2026, 10, 24, 3, tzinfo=UTC)
        thursday = FRIDAY - timedelta(days=1)
        ingest = make_ingest(engine, audit_key, clock=lambda: evening)
        ingest.ingest(parse(middle=[bell(thursday), bell(FRIDAY)], high=[]))
        latest = ingest.repository.latest()
        assert latest is not None
        assert [d.date for d in latest.days] == [FRIDAY]

    def test_a_zone_ahead_of_utc_drops_its_own_yesterday(self, engine, audit_key):
        # 12:00 UTC on Friday is 01:00 on Saturday in Auckland (UTC+13).
        clock = datetime(2026, 10, 23, 12, tzinfo=UTC)
        saturday = FRIDAY + timedelta(days=1)

        def local(day: date) -> dict[str, str]:
            return {
                "start": f"{day.isoformat()}T09:00:00+13:00",
                "end": f"{day.isoformat()}T15:00:00+13:00",
                "summary": "Regular Day",
            }

        ingest = make_ingest(
            engine, audit_key, clock=lambda: clock, timezone="Pacific/Auckland"
        )
        fetched = (clock - timedelta(minutes=1)).isoformat()
        ingest.ingest(
            parse(fetched_at=fetched, middle=[local(FRIDAY), local(saturday)], high=[])
        )
        latest = ingest.repository.latest()
        assert latest is not None
        assert [d.date for d in latest.days] == [saturday]

    def test_the_margin_covers_the_snapshot_staleness_limit(self):
        # A snapshot usable for max_age can be read up to ceil(max_age / 1 day)
        # local days after its fetch, so the kept window must reach that far.
        max_age = dict(DEFAULT_SOURCE_MAX_AGES)[HA_SNAPSHOT_SOURCE]
        assert math.ceil(max_age / timedelta(days=1)) <= DAYS_BEYOND_ASSESSMENT


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

    def test_the_entry_holds_exactly_the_acknowledgement(self, engine, audit_key):
        ack = make_ingest(engine, audit_key).ingest(parse(), correlation_id="req-1")
        settings = snapshot_settings(audit_key)
        (entry,) = AuditRepository.from_settings(engine, settings).entries_for(
            AUDIT_OBJECT_REF
        )
        assert entry.prior_hash is None
        assert entry.new_hash == state_hash(ack.as_dict(), key=audit_key)

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

    def test_codes_are_typed_and_serialize_as_plain_strings(self):
        error = IngestRejectedError("refused", error_code=IngestErrorCode.UNKNOWN_ZONE)
        assert error.error_code == "UNKNOWN_ZONE"
        assert type(error.error_code) is str

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


class TestPrecedence:
    """Reference errors win over a conflict with the stored snapshot."""

    def older(self, **overrides) -> HaSnapshotPayload:
        return parse(
            fetched_at=(NOW - timedelta(hours=1)).isoformat(),
            middle=[bell(FRIDAY, end="12:00", summary="Early Release")],
            **overrides,
        )

    def test_an_older_snapshot_with_an_unknown_zone_is_rejected(
        self, engine, audit_key
    ):
        make_ingest(engine, audit_key).ingest(parse())
        with pytest.raises(IngestRejectedError) as caught:
            make_ingest(engine, audit_key).ingest(
                self.older(zones=["zone.home", "zone.private_unlisted"])
            )
        assert caught.value.error_code == "UNKNOWN_ZONE"
        assert count(engine, audit_log) == 1

    def test_an_older_snapshot_under_a_stale_configuration_is_unavailable(
        self, engine, audit_key
    ):
        make_ingest(engine, audit_key).ingest(parse())
        stale = [SCHOOLS[0] | {"location_id": "nowhere"}, SCHOOLS[1]]
        with pytest.raises(SchoolIngestUnavailableError) as caught:
            make_ingest(engine, audit_key, ha_schools=stale).ingest(self.older())
        assert caught.value.error_code == "REGISTRY_MISMATCH"
        assert count(engine, audit_log) == 1


class TestEmptySnapshot:
    def test_every_calendar_empty_is_rejected_and_the_old_one_kept(
        self, engine, audit_key
    ):
        ingest = make_ingest(engine, audit_key)
        ingest.ingest(parse())
        before = ingest.repository.freshness()
        with pytest.raises(IngestRejectedError) as caught:
            make_ingest(engine, audit_key).ingest(
                parse(fetched_at=NOW.isoformat(), middle=[], high=[], district=[])
            )
        assert caught.value.error_code == "EMPTY_SNAPSHOT"
        assert ingest.repository.freshness() == before
        assert count(engine, school_day) == 3
        assert count(engine, audit_log) == 1

    def test_events_only_outside_the_window_count_as_empty(self, engine, audit_key):
        yesterday = FRIDAY - timedelta(days=1)
        with pytest.raises(IngestRejectedError) as caught:
            make_ingest(engine, audit_key).ingest(
                parse(middle=[bell(yesterday)], high=[], district=[])
            )
        assert caught.value.error_code == "EMPTY_SNAPSHOT"
        assert count(engine, school_day) == 0
        assert count(engine, audit_log) == 0

    def test_a_break_spanning_the_window_is_not_empty(self, engine, audit_key):
        summer = {"start": "2026-06-10", "end": "2026-12-31", "summary": "Summer Break"}
        ingest = make_ingest(engine, audit_key)
        ack = ingest.ingest(parse(middle=[], high=[], district=[summer]))
        assert ack.status is ReplaceStatus.STORED
        latest = ingest.repository.latest()
        assert latest is not None
        assert {d.kind for d in latest.days} == {SchoolDayKind.NO_SCHOOL}

    def test_some_empty_calendars_are_accepted(self, engine, audit_key):
        ack = make_ingest(engine, audit_key).ingest(parse(middle=[], district=[]))
        assert ack.status is ReplaceStatus.STORED
        assert ack.days == 2
        assert ack.schools_without_days == ("middle-school",)


class TestSchoolWithoutDays:
    """A school that got no row is stored and flagged, never refused (P29-09)."""

    def test_its_days_are_reported_as_school_009(self, engine, audit_key):
        with engine.begin() as connection:
            store = RegistryStore(connection)
            store.replace_transport_defaults(
                "alex",
                [default("alex", TransportDirection.TO_SCHOOL, TransportMode.BIKE)],
            )
            store.replace_transport_defaults(
                "sam",
                [
                    default(
                        "sam",
                        TransportDirection.TO_SCHOOL,
                        TransportMode.BIKE,
                        school_id="middle-school",
                    )
                ],
            )
        middle = [bell(day) for day in weekdays(FRIDAY, 17)]
        ack = make_ingest(engine, audit_key).ingest(parse(middle=middle, high=[]))
        assert ack.status is ReplaceStatus.STORED
        assert ack.schools_without_days == ("high-school",)

        collect(engine, CALENDAR, (), at_time=NOW - timedelta(minutes=20))
        settings = snapshot_settings(audit_key, caldav_calendars="")
        service = AssessmentService(engine, settings, routes(), clock=lambda: NOW)
        outcome = service.run(school=SchoolSnapshotRepository(engine).latest())
        flagged = [
            set(tracked.finding.entity_keys)
            for tracked in outcome.result.findings
            if tracked.finding.rule_id == "SCHOOL-009"
        ]
        assert not [keys for keys in flagged if "person:sam" in keys]
        alex_days = {
            key
            for keys in flagged
            if "person:alex" in keys
            for key in keys
            if key.startswith("day:")
        }
        assert alex_days == {f"day:{day.isoformat()}" for day in weekdays(FRIDAY, 15)}

    def test_an_identical_repost_names_the_same_school(self, engine, audit_key):
        make_ingest(engine, audit_key).ingest(parse(high=[]))
        again = make_ingest(engine, audit_key).ingest(parse(high=[]))
        assert again.status is ReplaceStatus.UNCHANGED
        assert again.schools_without_days == ("high-school",)


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
