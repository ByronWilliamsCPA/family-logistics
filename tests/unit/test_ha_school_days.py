"""Mapping Home Assistant calendar events to school days (ADR 0007)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from family_logistics.adapters.home_assistant.schemas import HaSnapshotPayload
from family_logistics.adapters.home_assistant.school_days import (
    SCHOOL_DAY_KEYWORDS,
    DayWindow,
    SnapshotMappingError,
    classify_summary,
    map_school_days,
)
from family_logistics.core.config import HaSchool
from family_logistics.core.exceptions import ValidationError
from family_logistics.domain.context import HA_SNAPSHOT_SOURCE
from family_logistics.domain.events import SchoolDay, SchoolDayKind

pytestmark = pytest.mark.unit

PACIFIC = ZoneInfo("America/Los_Angeles")
BELL = "calendar.middle_school_bell"
HIGH_BELL = "calendar.high_school_bell"
DISTRICT = "calendar.district"
FRIDAY = date(2026, 10, 23)

MIDDLE = HaSchool(
    location_id="middle-school",
    person_ids=("sam",),
    bell_calendars=(BELL,),
    district_calendars=(DISTRICT,),
)
HIGH = HaSchool(
    location_id="high-school",
    person_ids=("alex", "jordan"),
    bell_calendars=(HIGH_BELL,),
    district_calendars=(DISTRICT,),
)

Event = dict[str, str]


def timed(day: str, start: str, end: str, summary: str, offset: str = "-07:00"):
    return {
        "start": f"{day}T{start}:00{offset}",
        "end": f"{day}T{end}:00{offset}",
        "summary": summary,
    }


def all_day(start: str, end: str, summary: str) -> Event:
    return {"start": start, "end": end, "summary": summary}


def snapshot(
    calendars: dict[str, list[Event]],
    fetched_at: str = "2026-10-23T06:00:00-07:00",
) -> HaSnapshotPayload:
    body = {
        "fetched_at": fetched_at,
        "calendars": {name: {"events": events} for name, events in calendars.items()},
    }
    return HaSnapshotPayload.model_validate_json(json.dumps(body))


def run(
    calendars: dict[str, list[Event]],
    schools: tuple[HaSchool, ...] = (MIDDLE,),
    first_day: date = FRIDAY,
    last_day: date = date(2026, 11, 8),
) -> tuple[SchoolDay, ...]:
    return map_school_days(
        snapshot(calendars),
        schools,
        tz=PACIFIC,
        window=DayWindow(first_day, last_day),
    )


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def only(days: tuple[SchoolDay, ...]) -> SchoolDay:
    assert len(days) == 1, days
    return days[0]


class TestClassifySummary:
    @pytest.mark.parametrize(
        ("summary", "kind"),
        [
            ("Regular Day", SchoolDayKind.NORMAL),
            ("REGULAR BELL SCHEDULE", SchoolDayKind.NORMAL),
            ("No School", SchoolDayKind.NO_SCHOOL),
            ("NO SCHOOL - Veterans Day", SchoolDayKind.NO_SCHOOL),
            ("No-School Day", SchoolDayKind.NO_SCHOOL),
            ("Teacher Work-Day", SchoolDayKind.NO_SCHOOL),
            ("Thanksgiving Break", SchoolDayKind.NO_SCHOOL),
            ("Early Release", SchoolDayKind.EARLY_RELEASE),
            ("Minimum Day / Early Release", SchoolDayKind.EARLY_RELEASE),
            ("Late Start Monday", SchoolDayKind.LATE_START),
            ("2-Hour Delay", SchoolDayKind.LATE_START),
            ("Late Start / Early Release", SchoolDayKind.UNKNOWN),
            ("No School / Early Release", SchoolDayKind.UNKNOWN),
            ("Holiday Concert", SchoolDayKind.UNKNOWN),
            ("Holiday", SchoolDayKind.UNKNOWN),
            ("Picture Day", SchoolDayKind.UNKNOWN),
            ("", SchoolDayKind.UNKNOWN),
            ("Not a school day", SchoolDayKind.UNKNOWN),
            ("Regular day cancelled", SchoolDayKind.UNKNOWN),
            ("earlyrelease", SchoolDayKind.UNKNOWN),
        ],
    )
    def test_summary_kind(self, summary, kind):
        assert classify_summary(summary) is kind

    def test_the_table_covers_every_kind_but_unknown(self):
        assert set(SCHOOL_DAY_KEYWORDS) == set(SchoolDayKind) - {SchoolDayKind.UNKNOWN}

    def test_no_phrase_is_listed_under_two_kinds(self):
        phrases = [p for group in SCHOOL_DAY_KEYWORDS.values() for p in group]
        assert len(phrases) == len(set(phrases))

    def test_phrases_are_already_normalized(self):
        for group in SCHOOL_DAY_KEYWORDS.values():
            for phrase in group:
                assert phrase == " ".join(phrase.split())
                assert phrase.replace(" ", "").isalnum()
                assert phrase == phrase.casefold()


class TestNormalDay:
    def test_bell_event_gives_times_in_utc(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-23", "08:00", "15:00", "Regular Day")],
                    DISTRICT: [],
                }
            )
        )
        assert day == SchoolDay(
            person_id="sam",
            date=FRIDAY,
            location_id="middle-school",
            start=utc(2026, 10, 23, 15, 0),
            end=utc(2026, 10, 23, 22, 0),
            kind=SchoolDayKind.NORMAL,
            source=HA_SNAPSHOT_SOURCE,
        )

    def test_one_row_per_person(self):
        days = run(
            {
                HIGH_BELL: [timed("2026-10-23", "07:30", "14:30", "Regular Day")],
                DISTRICT: [],
            },
            schools=(HIGH,),
        )
        assert [d.person_id for d in days] == ["alex", "jordan"]
        assert {d.location_id for d in days} == {"high-school"}

    def test_bell_periods_on_one_day_span_the_day(self):
        day = only(
            run(
                {
                    BELL: [
                        timed("2026-10-23", "08:00", "11:00", "Regular Day"),
                        timed("2026-10-23", "11:30", "15:00", "Regular Day"),
                    ],
                    DISTRICT: [],
                }
            )
        )
        assert (day.start, day.end) == (utc(2026, 10, 23, 15), utc(2026, 10, 23, 22))

    def test_an_offset_other_than_local_is_mapped_to_the_local_day(self):
        event = {
            "start": "2026-10-23T15:00:00Z",
            "end": "2026-10-23T22:00:00Z",
            "summary": "Regular Day",
        }
        day = only(run({BELL: [event], DISTRICT: []}))
        assert day.date == FRIDAY
        assert day.start == utc(2026, 10, 23, 15)

    def test_a_normal_all_day_event_without_bell_times_is_unknown(self):
        day = only(
            run(
                {
                    BELL: [all_day("2026-10-23", "2026-10-24", "Regular Day")],
                    DISTRICT: [],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN

    def test_description_is_never_used(self):
        event = timed("2026-10-23", "08:00", "15:00", "Regular Day")
        event["description"] = "No school"
        event["location"] = "Early release"
        day = only(run({BELL: [event], DISTRICT: []}))
        assert day.kind is SchoolDayKind.NORMAL


class TestUnknownIsNeverNormal:
    def test_unrecognized_bell_event_is_unknown_with_its_times(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-23", "08:00", "15:00", "Spirit Week")],
                    DISTRICT: [],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN
        assert (day.start, day.end) == (utc(2026, 10, 23, 15), utc(2026, 10, 23, 22))

    def test_unrecognized_district_event_overrides_a_normal_bell_day(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-23", "08:00", "15:00", "Regular Day")],
                    DISTRICT: [all_day("2026-10-23", "2026-10-24", "Board Meeting")],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN

    def test_unknown_without_bell_times_is_a_zero_length_local_midnight(self):
        day = only(
            run({BELL: [], DISTRICT: [all_day("2026-10-23", "2026-10-24", "Fun Run")]})
        )
        assert day.kind is SchoolDayKind.UNKNOWN
        assert day.start == day.end == utc(2026, 10, 23, 7)

    def test_early_and_late_on_one_day_is_unknown(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-23", "10:00", "12:30", "Late Start")],
                    DISTRICT: [all_day("2026-10-23", "2026-10-24", "Early Release")],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN
        assert (day.start, day.end) == (
            utc(2026, 10, 23, 17),
            utc(2026, 10, 23, 19, 30),
        )

    def test_closure_beside_a_short_day_is_unknown(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-23", "08:00", "12:00", "Early Release")],
                    DISTRICT: [all_day("2026-10-23", "2026-10-24", "No School")],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN

    def test_a_day_without_events_has_no_row(self):
        days = run(
            {BELL: [timed("2026-10-23", "08:00", "15:00", "Regular Day")], DISTRICT: []}
        )
        assert {d.date for d in days} == {FRIDAY}


class TestAdjustedDays:
    def test_early_release_needs_a_matching_bell_event(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-23", "08:00", "12:30", "Early Release")],
                    DISTRICT: [all_day("2026-10-23", "2026-10-24", "Early Release")],
                }
            )
        )
        assert day.kind is SchoolDayKind.EARLY_RELEASE
        assert day.end == utc(2026, 10, 23, 19, 30)

    def test_early_release_with_only_regular_bells_is_unknown(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-23", "08:00", "15:00", "Regular Day")],
                    DISTRICT: [all_day("2026-10-23", "2026-10-24", "Early Release")],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN

    def test_late_start(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-23", "10:00", "15:00", "Late Start")],
                    DISTRICT: [],
                }
            )
        )
        assert day.kind is SchoolDayKind.LATE_START
        assert day.start == utc(2026, 10, 23, 17)


class TestNoSchool:
    def test_closure_is_zero_length_at_local_midnight(self):
        day = only(
            run(
                {BELL: [], DISTRICT: [all_day("2026-10-26", "2026-10-27", "No School")]}
            )
        )
        assert day.kind is SchoolDayKind.NO_SCHOOL
        assert day.date == date(2026, 10, 26)
        assert day.start == day.end == utc(2026, 10, 26, 7)

    def test_closure_wins_over_a_stale_regular_bell_event(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-26", "08:00", "15:00", "Regular Day")],
                    DISTRICT: [all_day("2026-10-26", "2026-10-27", "Teacher Work Day")],
                }
            )
        )
        assert day.kind is SchoolDayKind.NO_SCHOOL

    def test_shared_district_calendar_closes_every_school(self):
        days = run(
            {
                BELL: [],
                HIGH_BELL: [],
                DISTRICT: [all_day("2026-10-26", "2026-10-27", "No School")],
            },
            schools=(MIDDLE, HIGH),
        )
        assert [(d.person_id, d.location_id) for d in days] == [
            ("alex", "high-school"),
            ("jordan", "high-school"),
            ("sam", "middle-school"),
        ]
        assert {d.kind for d in days} == {SchoolDayKind.NO_SCHOOL}


class TestMultiDayAndDst:
    def test_all_day_break_across_fall_back(self):
        # 2026-11-01 is the end of daylight saving time in the Pacific zone.
        days = run(
            {BELL: [], DISTRICT: [all_day("2026-10-30", "2026-11-03", "Fall Break")]}
        )
        assert [(d.date, d.start) for d in days] == [
            (date(2026, 10, 30), utc(2026, 10, 30, 7)),
            (date(2026, 10, 31), utc(2026, 10, 31, 7)),
            (date(2026, 11, 1), utc(2026, 11, 1, 7)),
            (date(2026, 11, 2), utc(2026, 11, 2, 8)),
        ]
        assert {d.kind for d in days} == {SchoolDayKind.NO_SCHOOL}

    def test_bell_times_after_fall_back_use_standard_time(self):
        day = only(
            run(
                {
                    BELL: [
                        timed("2026-11-02", "08:00", "15:00", "Regular Day", "-08:00")
                    ],
                    DISTRICT: [],
                }
            )
        )
        assert (day.start, day.end) == (utc(2026, 11, 2, 16), utc(2026, 11, 2, 23))

    def test_spring_forward_midnight(self):
        days = run(
            {BELL: [], DISTRICT: [all_day("2027-03-14", "2027-03-16", "Spring Break")]},
            first_day=date(2027, 3, 13),
            last_day=date(2027, 3, 20),
        )
        assert [d.start for d in days] == [utc(2027, 3, 14, 8), utc(2027, 3, 15, 7)]

    def test_timed_multi_day_event_classifies_each_day_but_gives_no_times(self):
        event = {
            "start": "2026-10-26T00:00:00-07:00",
            "end": "2026-10-28T00:00:00-07:00",
            "summary": "No School",
        }
        days = run({BELL: [event], DISTRICT: []})
        assert [(d.date, d.kind) for d in days] == [
            (date(2026, 10, 26), SchoolDayKind.NO_SCHOOL),
            (date(2026, 10, 27), SchoolDayKind.NO_SCHOOL),
        ]

    def test_a_timed_event_crossing_midnight_gives_no_bell_times(self):
        event = timed("2026-10-23", "22:00", "23:00", "Regular Day")
        event["end"] = "2026-10-24T01:00:00-07:00"
        days = run({BELL: [event], DISTRICT: []})
        assert [(d.date, d.kind) for d in days] == [
            (date(2026, 10, 23), SchoolDayKind.UNKNOWN),
            (date(2026, 10, 24), SchoolDayKind.UNKNOWN),
        ]

    def test_an_instantaneous_event_belongs_to_its_day(self):
        moment = timed("2026-10-23", "12:00", "12:00", "Early Release")
        day = only(run({BELL: [moment], DISTRICT: []}))
        assert day.kind is SchoolDayKind.EARLY_RELEASE
        assert day.start == day.end == utc(2026, 10, 23, 19)


class TestHorizon:
    def test_days_outside_the_window_are_dropped(self):
        days = run(
            {BELL: [], DISTRICT: [all_day("2026-10-20", "2026-12-01", "Winter Break")]},
            first_day=date(2026, 10, 23),
            last_day=date(2026, 10, 25),
        )
        assert [d.date for d in days] == [
            date(2026, 10, 23),
            date(2026, 10, 24),
            date(2026, 10, 25),
        ]

    def test_output_is_sorted_by_day_then_person(self):
        days = run(
            {
                BELL: [timed("2026-10-26", "08:00", "15:00", "Regular Day")],
                HIGH_BELL: [timed("2026-10-23", "08:00", "15:00", "Regular Day")],
                DISTRICT: [],
            },
            schools=(MIDDLE, HIGH),
        )
        assert [(d.date, d.person_id) for d in days] == [
            (FRIDAY, "alex"),
            (FRIDAY, "jordan"),
            (date(2026, 10, 26), "sam"),
        ]

    def test_an_inverted_window_is_refused(self):
        with pytest.raises(ValidationError):
            DayWindow(FRIDAY, date(2026, 10, 1))

    def test_window_membership_is_inclusive_and_dates_only(self):
        window = DayWindow(FRIDAY, date(2026, 10, 24))
        assert FRIDAY in window
        assert date(2026, 10, 24) in window
        assert date(2026, 10, 25) not in window
        assert "2026-10-23" not in window


class TestEntities:
    def test_an_unconfigured_calendar_is_refused_without_echo(self):
        with pytest.raises(SnapshotMappingError) as caught:
            run({BELL: [], DISTRICT: [], "calendar.private_unlisted": []})
        assert caught.value.error_code == "UNKNOWN_CALENDAR"
        assert "private_unlisted" not in str(caught.value.to_dict())

    def test_a_configured_calendar_missing_from_the_payload_is_refused(self):
        with pytest.raises(SnapshotMappingError) as caught:
            run({BELL: []})
        assert caught.value.error_code == "MISSING_CALENDAR"
        assert "district" not in str(caught.value.to_dict())

    def test_the_error_is_a_validation_error(self):
        assert issubclass(SnapshotMappingError, ValidationError)

    def test_no_configured_school_is_refused(self):
        with pytest.raises(SnapshotMappingError):
            run({BELL: [], DISTRICT: []}, schools=())
