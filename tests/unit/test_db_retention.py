"""Raw source text purge: 30-day retention of source_event location/description."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, select

from family_logistics.core.database import create_sqlite_engine
from family_logistics.core.exceptions import ValidationError
from family_logistics.db import migrations_api, schema
from family_logistics.db.retention import RAW_TEXT_RETENTION_DAYS, purge_raw_source_text

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
LOS_ANGELES = ZoneInfo("America/Los_Angeles")
LOCATION_TEXT = "Room 4, Example Hall"
DESCRIPTION_TEXT = "Bring the signed form"
NAIVE_NOW = NOW.replace(tzinfo=None)


@pytest.fixture
def engine(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(schema.calendar_source).values(
                calendar_id="cal-1", display_name="Transport", kind="transport"
            )
        )
    yield engine
    engine.dispose()


def add_event(
    engine,
    uid: str,
    *,
    end: datetime,
    deleted_at: datetime | None = None,
    location: str | None = LOCATION_TEXT,
    description: str | None = DESCRIPTION_TEXT,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(schema.source_event).values(
                calendar_id="cal-1",
                uid=uid,
                recurrence_id="",
                start_utc=end - timedelta(hours=1),
                end_utc=end,
                summary="Practice",
                location=location,
                description=description,
                status="confirmed",
                raw_hash="0" * 64,
                first_seen=NOW,
                last_seen=NOW,
                deleted_at=deleted_at,
            )
        )


def purge(engine, *, now: datetime = NOW) -> int:
    with engine.begin() as connection:
        return purge_raw_source_text(connection, now=now)


def raw_text(engine) -> dict[str, tuple[str | None, str | None]]:
    """uid -> (location, description) for every stored event."""
    table = schema.source_event
    with engine.connect() as connection:
        rows = connection.execute(
            select(table.c.uid, table.c.location, table.c.description)
        )
        return {row.uid: (row.location, row.description) for row in rows}


KEPT = (LOCATION_TEXT, DESCRIPTION_TEXT)
CLEARED = (None, None)


@pytest.mark.unit
class TestRetentionBoundary:
    def test_retention_is_thirty_days(self):
        assert RAW_TEXT_RETENTION_DAYS == 30

    @pytest.mark.parametrize(
        ("age", "purged"),
        [
            (timedelta(days=29), False),
            (timedelta(days=30), False),
            (timedelta(days=30, seconds=1), True),
            (timedelta(days=31), True),
        ],
        ids=["29-days", "exactly-30-days", "30-days-and-1-second", "31-days"],
    )
    def test_boundary_is_strictly_more_than_thirty_days_after_the_end(
        self, engine, age, purged
    ):
        add_event(engine, "uid-1", end=NOW - age)
        assert purge(engine) == (1 if purged else 0)
        assert raw_text(engine) == {"uid-1": CLEARED if purged else KEPT}

    def test_event_ending_in_the_future_is_untouched(self, engine):
        add_event(engine, "uid-1", end=NOW + timedelta(days=2))
        assert purge(engine) == 0
        assert raw_text(engine) == {"uid-1": KEPT}

    def test_cutoff_is_an_instant_not_the_wall_clock_of_now(self, engine):
        # The same instant as NOW, written in Los Angeles time: the comparison
        # must use UTC, so 31 days is purged and 29 days is kept.
        local_now = NOW.astimezone(LOS_ANGELES)
        add_event(engine, "uid-old", end=NOW - timedelta(days=31))
        add_event(engine, "uid-new", end=NOW - timedelta(days=29))
        assert purge(engine, now=local_now) == 1
        assert raw_text(engine) == {"uid-old": CLEARED, "uid-new": KEPT}


@pytest.mark.unit
class TestDeletedAtPrecedence:
    def test_old_deleted_at_purges_an_event_that_ended_recently(self, engine):
        add_event(
            engine,
            "uid-1",
            end=NOW - timedelta(days=1),
            deleted_at=NOW - timedelta(days=40),
        )
        assert purge(engine) == 1
        assert raw_text(engine) == {"uid-1": CLEARED}

    def test_recent_deleted_at_keeps_an_event_that_ended_long_ago(self, engine):
        add_event(
            engine,
            "uid-1",
            end=NOW - timedelta(days=60),
            deleted_at=NOW - timedelta(days=5),
        )
        assert purge(engine) == 0
        assert raw_text(engine) == {"uid-1": KEPT}

    def test_end_is_used_when_the_event_was_never_deleted(self, engine):
        add_event(engine, "uid-1", end=NOW - timedelta(days=45), deleted_at=None)
        assert purge(engine) == 1
        assert raw_text(engine) == {"uid-1": CLEARED}


@pytest.mark.unit
class TestPurgeScope:
    def test_only_location_and_description_change_and_no_row_is_removed(self, engine):
        add_event(engine, "uid-1", end=NOW - timedelta(days=45))
        table = schema.source_event
        columns = [
            column
            for column in table.c
            if column.name not in {"location", "description"}
        ]
        with engine.connect() as connection:
            before = connection.execute(select(*columns).order_by(table.c.id)).all()
        assert purge(engine) == 1
        with engine.connect() as connection:
            after = connection.execute(select(*columns).order_by(table.c.id)).all()
        assert after == before
        assert len(after) == 1
        summary = next(
            value
            for column, value in zip(columns, after[0], strict=True)
            if column.name == "summary"
        )
        assert summary == "Practice"
        assert raw_text(engine) == {"uid-1": CLEARED}

    def test_rows_with_nothing_left_to_clear_are_not_counted(self, engine):
        add_event(
            engine,
            "uid-empty",
            end=NOW - timedelta(days=45),
            location=None,
            description=None,
        )
        add_event(
            engine,
            "uid-location-only",
            end=NOW - timedelta(days=45),
            location=LOCATION_TEXT,
            description=None,
        )
        add_event(
            engine,
            "uid-description-only",
            end=NOW - timedelta(days=45),
            location=None,
            description=DESCRIPTION_TEXT,
        )
        assert purge(engine) == 2
        assert raw_text(engine) == {
            "uid-empty": CLEARED,
            "uid-location-only": CLEARED,
            "uid-description-only": CLEARED,
        }

    def test_second_call_changes_nothing_and_returns_zero(self, engine):
        add_event(engine, "uid-old", end=NOW - timedelta(days=45))
        add_event(engine, "uid-new", end=NOW - timedelta(days=2))
        assert purge(engine) == 1
        assert purge(engine) == 0
        assert raw_text(engine) == {"uid-old": CLEARED, "uid-new": KEPT}

    def test_a_later_run_purges_what_has_since_aged_out(self, engine):
        add_event(engine, "uid-1", end=NOW - timedelta(days=10))
        assert purge(engine) == 0
        assert purge(engine, now=NOW + timedelta(days=21)) == 1
        assert raw_text(engine) == {"uid-1": CLEARED}


@pytest.mark.unit
class TestNaiveNowRejected:
    def test_naive_now_raises_validation_error_for_the_now_field(self, engine):
        add_event(engine, "uid-1", end=NOW - timedelta(days=45))
        with pytest.raises(ValidationError) as caught:
            purge(engine, now=NAIVE_NOW)
        assert caught.value.details == {"field": "now"}
        assert raw_text(engine) == {"uid-1": KEPT}

    def test_message_does_not_echo_the_value(self, engine):
        with pytest.raises(ValidationError) as caught:
            purge(engine, now=NAIVE_NOW)
        rendered = f"{caught.value} {caught.value.to_dict()} {caught.value!r}"
        assert "2026" not in rendered
        assert "18:00" not in rendered
