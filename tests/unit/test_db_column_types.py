"""UtcDateTime: aware UTC round trip on SQLite, which keeps no offset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    create_engine,
    insert,
    select,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import StatementError
from sqlalchemy.pool import StaticPool

from family_logistics.core.database import create_sqlite_engine
from family_logistics.db import migrations_api, schema
from family_logistics.db.column_types import UtcDateTime

LOS_ANGELES = ZoneInfo("America/Los_Angeles")
# 22:00 PDT (UTC-7, DST ends 2026-11-01) is 05:00 UTC on the next day.
LA_EVENING = datetime(2026, 10, 21, 22, 0, tzinfo=LOS_ANGELES)
UTC_EQUIVALENT = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)
STORED_TEXT = "2026-10-22 05:00:00.000000"
NAIVE = UTC_EQUIVALENT.replace(tzinfo=None)


class OffsetlessZone(tzinfo):
    """A tzinfo whose offset is unknown, so the datetime is effectively naive."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return None


@pytest.fixture
def stamps():
    """A throwaway in-memory table with one nullable UtcDateTime column."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    metadata = MetaData()
    table = Table(
        "stamp",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("moment", UtcDateTime(), nullable=True),
    )
    metadata.create_all(engine)
    yield engine, table
    engine.dispose()


def stored_text(engine) -> list[object]:
    """The raw column text, bypassing the type."""
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT moment FROM stamp ORDER BY id")
        return [row[0] for row in rows]


@pytest.mark.unit
class TestUtcDateTimeRoundTrip:
    def test_aware_non_utc_value_round_trips_as_the_same_instant(self, stamps):
        engine, table = stamps
        with engine.begin() as connection:
            connection.execute(insert(table).values(id=1, moment=LA_EVENING))
        with engine.connect() as connection:
            value = connection.scalar(select(table.c.moment))
        assert value == LA_EVENING
        assert value == UTC_EQUIVALENT
        assert value.tzinfo is UTC
        assert (value.hour, value.day) == (5, 22)

    def test_value_is_stored_as_utc_wall_clock(self, stamps):
        engine, table = stamps
        with engine.begin() as connection:
            connection.execute(insert(table).values(id=1, moment=LA_EVENING))
        assert stored_text(engine) == [STORED_TEXT]

    def test_aware_utc_value_is_unchanged(self, stamps):
        engine, table = stamps
        with engine.begin() as connection:
            connection.execute(insert(table).values(id=1, moment=UTC_EQUIVALENT))
            value = connection.scalar(select(table.c.moment))
        assert value == UTC_EQUIVALENT
        assert value.tzinfo is UTC
        assert stored_text(engine) == [STORED_TEXT]

    def test_naive_value_is_rejected_at_insert(self, stamps):
        engine, table = stamps
        naive = NAIVE
        with (
            pytest.raises(StatementError, match="naive datetime rejected") as caught,
            engine.begin() as connection,
        ):
            connection.execute(insert(table).values(id=1, moment=naive))
        assert isinstance(caught.value.orig, ValueError)
        assert stored_text(engine) == []

    def test_none_passes_through(self, stamps):
        engine, table = stamps
        with engine.begin() as connection:
            connection.execute(insert(table).values(id=1, moment=None))
        with engine.connect() as connection:
            value = connection.scalar(select(table.c.moment))
        assert value is None
        assert stored_text(engine) == [None]

    def test_naive_text_written_by_raw_sql_reads_back_as_aware_utc(self, stamps):
        engine, table = stamps
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO stamp (id, moment) VALUES (1, ?)", (STORED_TEXT,)
            )
        with engine.connect() as connection:
            value = connection.scalar(select(table.c.moment))
        assert value == UTC_EQUIVALENT
        assert value.tzinfo is UTC

    def test_aware_value_in_where_matches_the_stored_row(self, stamps):
        engine, table = stamps
        with engine.begin() as connection:
            connection.execute(insert(table).values(id=1, moment=LA_EVENING))
        with engine.connect() as connection:
            by_la = connection.scalars(
                select(table.c.id).where(table.c.moment == LA_EVENING)
            )
            assert list(by_la) == [1]
            by_utc = connection.scalars(
                select(table.c.id).where(table.c.moment == UTC_EQUIVALENT)
            )
            assert list(by_utc) == [1]
            # 05:00 in Los Angeles is a different instant (12:00 UTC).
            other = datetime(2026, 10, 22, 5, 0, tzinfo=LOS_ANGELES)
            by_other = connection.scalars(
                select(table.c.id).where(table.c.moment == other)
            )
            assert list(by_other) == []

    def test_range_comparison_orders_by_instant(self, stamps):
        engine, table = stamps
        with engine.begin() as connection:
            connection.execute(insert(table).values(id=1, moment=LA_EVENING))
        # 23:00 in Los Angeles is 06:00 UTC, an hour after the stored 05:00 UTC,
        # although its wall-clock text sorts before the stored text.
        later_instant = datetime(2026, 10, 21, 23, 0, tzinfo=LOS_ANGELES)
        with engine.connect() as connection:
            rows = connection.scalars(
                select(table.c.id).where(table.c.moment < later_instant)
            )
            assert list(rows) == [1]


@pytest.mark.unit
class TestUtcDateTimeProcessors:
    def test_bind_rejects_naive_value(self):
        naive = NAIVE
        with pytest.raises(ValueError, match="naive datetime rejected"):
            UtcDateTime().process_bind_param(naive, sqlite.dialect())

    def test_bind_rejects_tzinfo_without_an_offset(self):
        value = datetime(2026, 10, 22, 5, 0, tzinfo=OffsetlessZone())
        with pytest.raises(ValueError, match="naive datetime rejected"):
            UtcDateTime().process_bind_param(value, sqlite.dialect())

    def test_bind_converts_to_utc(self):
        bound = UtcDateTime().process_bind_param(LA_EVENING, sqlite.dialect())
        assert bound == UTC_EQUIVALENT
        assert bound.utcoffset() == timedelta(0)
        assert (bound.hour, bound.day) == (5, 22)

    def test_bind_passes_none(self):
        assert UtcDateTime().process_bind_param(None, sqlite.dialect()) is None

    def test_result_tags_naive_value_as_utc(self):
        naive = NAIVE
        value = UtcDateTime().process_result_value(naive, sqlite.dialect())
        assert value == UTC_EQUIVALENT
        assert value.tzinfo is UTC

    def test_result_converts_aware_value_to_utc(self):
        value = UtcDateTime().process_result_value(LA_EVENING, sqlite.dialect())
        assert value == UTC_EQUIVALENT
        assert (value.hour, value.day) == (5, 22)
        assert value.tzinfo is UTC

    def test_result_passes_none(self):
        assert UtcDateTime().process_result_value(None, sqlite.dialect()) is None

    def test_ddl_type_is_datetime_with_timezone(self):
        dialect = sqlite.dialect()
        column_type = UtcDateTime()
        assert column_type.cache_ok is True
        assert isinstance(column_type.impl_instance, DateTime)
        assert column_type.impl_instance.timezone is True
        assert column_type.compile(dialect=dialect) == DateTime(timezone=True).compile(
            dialect=dialect
        )


@pytest.fixture
def migrated(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fl.db")
    migrations_api.upgrade(engine)
    yield engine
    engine.dispose()


@pytest.mark.unit
class TestUtcDateTimeOnRealSchema:
    def seed_calendar(self, connection) -> None:
        connection.execute(
            insert(schema.calendar_source).values(
                calendar_id="cal-1", display_name="Transport", kind="transport"
            )
        )

    def test_los_angeles_offset_round_trips_through_source_event(self, migrated):
        end = LA_EVENING + timedelta(hours=1)
        with migrated.begin() as connection:
            self.seed_calendar(connection)
            connection.execute(
                insert(schema.source_event).values(
                    calendar_id="cal-1",
                    uid="uid-1",
                    start_utc=LA_EVENING,
                    end_utc=end,
                    status="confirmed",
                    raw_hash="0" * 64,
                    first_seen=LA_EVENING,
                    last_seen=LA_EVENING,
                )
            )
        table = schema.source_event
        with migrated.connect() as connection:
            row = connection.execute(
                select(table.c.start_utc, table.c.end_utc, table.c.first_seen)
            ).one()
            raw = connection.exec_driver_sql(
                "SELECT start_utc FROM source_event"
            ).scalar()
        assert row.start_utc == UTC_EQUIVALENT
        assert row.end_utc == UTC_EQUIVALENT + timedelta(hours=1)
        assert row.first_seen == UTC_EQUIVALENT
        assert row.start_utc.tzinfo is UTC
        assert raw == STORED_TEXT

    def test_naive_datetime_is_rejected_by_a_real_column(self, migrated):
        with migrated.begin() as connection:
            self.seed_calendar(connection)
        with (
            pytest.raises(StatementError, match="naive datetime rejected") as caught,
            migrated.begin() as connection,
        ):
            connection.execute(
                insert(schema.sync_state).values(
                    calendar_id="cal-1", last_full_sync=NAIVE
                )
            )
        assert isinstance(caught.value.orig, ValueError)

    def test_only_audit_recorded_at_is_a_plain_datetime(self):
        dialect = sqlite.dialect()
        plain = {
            (table.name, column.name)
            for table in schema.metadata.tables.values()
            for column in table.columns
            if column.type.compile(dialect=dialect) == "DATETIME"
            and not isinstance(column.type, UtcDateTime)
        }
        assert plain == {("audit_log", "recorded_at")}
