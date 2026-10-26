"""Mapping Home Assistant calendar events to school days (ADR 0007)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError as PydanticValidationError

from family_logistics.adapters.home_assistant import school_days
from family_logistics.adapters.home_assistant.schemas import (
    MAX_EVENTS_PER_CALENDAR,
    HaCalendar,
    HaCalendarEvent,
    HaSnapshotPayload,
)
from family_logistics.adapters.home_assistant.school_days import (
    CONTEXT_WORDS,
    MAX_WINDOW_DAYS,
    NEUTRAL_WORDS,
    SCHOOL_DAY_KEYWORDS,
    DayWindow,
    SchoolDayMapping,
    SnapshotMappingCode,
    SnapshotMappingError,
    classify_summary,
    map_school_snapshot,
)
from family_logistics.core.config import HaSchool
from family_logistics.core.exceptions import ValidationError
from family_logistics.domain.context import HA_SNAPSHOT_SOURCE
from family_logistics.domain.events import SchoolDay, SchoolDayKind

pytestmark = pytest.mark.unit

PACIFIC = ZoneInfo("America/Los_Angeles")
TOKYO = ZoneInfo("Asia/Tokyo")
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


def mapped(
    calendars: dict[str, list[Event]],
    schools: tuple[HaSchool, ...] = (MIDDLE,),
    first_day: date = FRIDAY,
    last_day: date = date(2026, 11, 8),
) -> SchoolDayMapping:
    return map_school_snapshot(
        snapshot(calendars),
        schools,
        tz=PACIFIC,
        window=DayWindow(first_day, last_day),
    )


def run(
    calendars: dict[str, list[Event]],
    schools: tuple[HaSchool, ...] = (MIDDLE,),
    first_day: date = FRIDAY,
    last_day: date = date(2026, 11, 8),
) -> tuple[SchoolDay, ...]:
    return mapped(calendars, schools, first_day, last_day).days


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

    @pytest.mark.parametrize(
        "summary",
        [
            "Last Day Before Winter Break",
            "Winter Break Ends",
            "Welcome back from Spring Break",
            "Spring Break ends",
            "Classes resume after spring break",
            "Snow Day Make-Up Day",
            "Snow day makeup (school in session)",
            "School Closed Early",
            "Normal day, no snow day",
            "Teacher work day cancelled",
            "No School - CANCELLED",
            "No school for seniors",
            "No school closure",
            "Professional development day for staff only; students attend",
            "Regular Day (no school Friday)",
            "School closed? No - open as normal",
            "No early release",
            "Not an early release day",
            "Early release cancelled",
            "Late start cancelled",
            "Early out cancelled",
            "School day (no early release)",
            "Modified Bell Schedule",
            "Assembly Bell Schedule",
            "Exam bell schedule",
            "Minimum Bell Schedule",
            "Snow day lifted",
            "Late start rescheduled",
            "Spring break postponed",
            "Regular day except grade 8",
            "No school unless weather improves",
            "Early release instead of late start",
        ],
    )
    def test_a_qualified_summary_is_unknown(self, summary):
        assert classify_summary(summary) is SchoolDayKind.UNKNOWN

    @pytest.mark.parametrize(
        ("summary", "kind"),
        [
            ("No School", SchoolDayKind.NO_SCHOOL),
            ("Winter Break", SchoolDayKind.NO_SCHOOL),
            ("Snow Day", SchoolDayKind.NO_SCHOOL),
            ("School Closed", SchoolDayKind.NO_SCHOOL),
            ("Student Free Day", SchoolDayKind.NO_SCHOOL),
            ("Teacher Work Day", SchoolDayKind.NO_SCHOOL),
            ("Non-Student Day", SchoolDayKind.NO_SCHOOL),
            ("Emergency Closure", SchoolDayKind.NO_SCHOOL),
            ("No School for Students", SchoolDayKind.NO_SCHOOL),
            ("Early Release", SchoolDayKind.EARLY_RELEASE),
            ("Early Dismissal", SchoolDayKind.EARLY_RELEASE),
            ("Two Hour Delay", SchoolDayKind.LATE_START),
            ("Late Arrival", SchoolDayKind.LATE_START),
            ("Regular Day", SchoolDayKind.NORMAL),
            ("Normal Bell Schedule", SchoolDayKind.NORMAL),
        ],
    )
    def test_a_plain_summary_still_classifies(self, summary, kind):
        assert classify_summary(summary) is kind

    @pytest.mark.parametrize(
        "summary",
        [
            # uncertainty
            "No School (Tentative)",
            "Early Release TBD",
            "Snow Day (Pending Board Approval)",
            "Regular Day - Tentative",
            "Winter Break (TBD)",
            # grade or group limits, relative days
            "No School Grades 6-8",
            "No School Preschool",
            "No School Tomorrow",
            # an event that only mentions the day
            "School Holiday Celebration",
            "School Holiday Assembly",
            "Winter Break Information Session",
            "Winter Break Camp Registration Opens",
            "Emergency Closure Drill",
            "Full Day Field Trip",
            "Full Day Kindergarten Begins",
            "Regular Day Weather Permitting",
            "Early Release Volunteer Briefing",
            "Late Start Bus Driver Training",
            # a closure word beside another program or a season
            "No School Summer School",
            "No School Winter",
            "School Day Fall",
            "No School All District",
            "No School Students",
            # overlapping phrases that each hide the other's context word
            "No School Closed",
            # already unknown; must stay so
            "Regular Day - No Buses",
            "School Closed Early",
            "No early release",
            "No School for Veterans Day",
        ],
    )
    def test_a_word_outside_the_tables_makes_it_unknown(self, summary):
        assert classify_summary(summary) is SchoolDayKind.UNKNOWN

    @pytest.mark.parametrize(
        "summary",
        [
            " ",
            "   \t\n ",
            "-",
            "!!! ... ???",
            "()[]{}/\\",
            "D\u00eda sin escuela",
            "No School \u00dc",
            "No School \u00f1",
            "No Scho\u00f3l",
            "No Scho\u006f\u0301l",
            "\u5b66\u6821\u4f11\u307f",
            "No_School",
            "No School \u0663",
        ],
    )
    def test_blank_punctuation_and_non_ascii_summaries_are_unknown(self, summary):
        assert classify_summary(summary) is SchoolDayKind.UNKNOWN

    @pytest.mark.parametrize(
        ("summary", "kind"),
        [
            ("No School", SchoolDayKind.NO_SCHOOL),
            ("No School - Labor Day", SchoolDayKind.NO_SCHOOL),
            ("No School \u2013 Labor Day", SchoolDayKind.NO_SCHOOL),
            ("No School - Thanksgiving Break", SchoolDayKind.NO_SCHOOL),
            (
                "No School: Columbus Day / Indigenous Peoples' Day",
                SchoolDayKind.NO_SCHOOL,
            ),
            ("No School - Veterans Day", SchoolDayKind.NO_SCHOOL),
            ("No School - Martin Luther King Jr. Day", SchoolDayKind.NO_SCHOOL),
            ("No School - Dr. Martin Luther King, Jr. Day", SchoolDayKind.NO_SCHOOL),
            ("No School - MLK Day", SchoolDayKind.NO_SCHOOL),
            ("No School - Presidents' Day", SchoolDayKind.NO_SCHOOL),
            ("No School - President's Day", SchoolDayKind.NO_SCHOOL),
            ("No School - Memorial Day", SchoolDayKind.NO_SCHOOL),
            ("No School - Juneteenth", SchoolDayKind.NO_SCHOOL),
            ("No School - Independence Day", SchoolDayKind.NO_SCHOOL),
            ("No School - New Year's Day", SchoolDayKind.NO_SCHOOL),
            ("No School - Christmas Eve", SchoolDayKind.NO_SCHOOL),
            ("No School for All Students", SchoolDayKind.NO_SCHOOL),
            ("No School for Students", SchoolDayKind.NO_SCHOOL),
            ("No Students", SchoolDayKind.NO_SCHOOL),
            ("Winter Break", SchoolDayKind.NO_SCHOOL),
            ("Spring Break", SchoolDayKind.NO_SCHOOL),
            ("Fall Break", SchoolDayKind.NO_SCHOOL),
            ("Mid-Winter Break", SchoolDayKind.NO_SCHOOL),
            ("Summer Break", SchoolDayKind.NO_SCHOOL),
            ("Holiday Break", SchoolDayKind.NO_SCHOOL),
            ("Snow Day", SchoolDayKind.NO_SCHOOL),
            ("Student Free Day", SchoolDayKind.NO_SCHOOL),
            ("Teacher Work Day", SchoolDayKind.NO_SCHOOL),
            ("Teacher Workday", SchoolDayKind.NO_SCHOOL),
            ("Professional Development Day", SchoolDayKind.NO_SCHOOL),
            ("Staff Development Day", SchoolDayKind.NO_SCHOOL),
            ("School Closed", SchoolDayKind.NO_SCHOOL),
            ("Emergency Closure", SchoolDayKind.NO_SCHOOL),
            ("No Classes", SchoolDayKind.NO_SCHOOL),
            ("Early Release", SchoolDayKind.EARLY_RELEASE),
            ("Early Release 12:30 PM", SchoolDayKind.EARLY_RELEASE),
            ("Early Release - 12:30pm", SchoolDayKind.EARLY_RELEASE),
            ("Early Release Noon", SchoolDayKind.EARLY_RELEASE),
            ("Early Dismissal - 1:00 p.m.", SchoolDayKind.EARLY_RELEASE),
            ("Minimum Day", SchoolDayKind.EARLY_RELEASE),
            ("Half Day", SchoolDayKind.EARLY_RELEASE),
            ("Two Hour Delay", SchoolDayKind.LATE_START),
            ("2 Hour Delay", SchoolDayKind.LATE_START),
            ("Late Start 10:00 AM", SchoolDayKind.LATE_START),
            ("Delayed Opening", SchoolDayKind.LATE_START),
            ("Regular Day", SchoolDayKind.NORMAL),
            ("Regular Bell Schedule", SchoolDayKind.NORMAL),
            ("Normal Schedule", SchoolDayKind.NORMAL),
            ("School Day", SchoolDayKind.NORMAL),
            ("Full Day", SchoolDayKind.NORMAL),
        ],
    )
    def test_a_realistic_plain_summary_keeps_its_kind(self, summary, kind):
        assert classify_summary(summary) is kind

    def test_the_neutral_table_holds_exactly_the_reviewed_words(self):
        # Weekdays, months and clock fragments left this table: they are read
        # as dates and times and checked against the event. School, student,
        # district and season words left it too ("No School Summer School").
        reviewed = {
            # filler
            "a", "an", "the", "of", "and", "or", "all", "day", "days",
            # holiday names
            "labor", "columbus", "indigenous", "peoples", "veterans",
            "thanksgiving", "christmas", "new", "year", "years", "eve",
            "martin", "luther", "king", "jr", "dr", "mlk", "presidents",
            "president", "memorial", "independence", "juneteenth", "easter",
            "good",
            # possessive fragment ("New Year's" reads "new year s")
            "s",
        }  # fmt: skip
        assert reviewed == NEUTRAL_WORDS

    @pytest.mark.parametrize(
        "word",
        [
            "celebration", "program", "assembly", "concert", "party", "camp",
            "registration", "session", "information", "drill", "training",
            "briefing", "meeting", "trip", "field", "testing", "kindergarten",
            "preschool", "prek", "grade", "grades", "tentative", "tbd",
            "pending", "proposed", "updated", "revised", "tomorrow", "today",
            "buses", "permitting", "weather", "volunteer", "break", "holiday",
            "school", "schools", "student", "students", "district", "spring",
            "winter", "fall", "summer", "mid", "midwinter", "monday", "fri",
            "dec", "may", "am", "pm", "p", "m", "noon", "6", "9th",
        ],
    )  # fmt: skip
    def test_an_excluded_word_is_not_neutral(self, word):
        assert word not in NEUTRAL_WORDS

    def test_neutral_words_are_normalized_and_disjoint_from_context(self):
        for word in NEUTRAL_WORDS:
            assert word.isascii()
            assert word.isalnum()
            assert word == word.casefold()
        assert NEUTRAL_WORDS.isdisjoint(CONTEXT_WORDS)

    @pytest.mark.parametrize(
        ("summary", "kind"),
        [
            # a clock time qualifies a short or late day
            ("Early Release 1230PM", SchoolDayKind.EARLY_RELEASE),
            ("Late Start 9am", SchoolDayKind.LATE_START),
            ("Late Start 9 a.m.", SchoolDayKind.LATE_START),
            # ...but not a closure (a partial day) or a regular day
            ("No School 12:30 PM", SchoolDayKind.UNKNOWN),
            ("School Closed Noon", SchoolDayKind.UNKNOWN),
            ("Regular Day 8:00 AM", SchoolDayKind.UNKNOWN),
            # an impossible clock time is not a time
            ("Early Release 13:75 pm", SchoolDayKind.UNKNOWN),
            ("Early Release 0 am", SchoolDayKind.UNKNOWN),
            # bare numbers, ordinals and ranges name grades as often as days
            ("No School 6-8", SchoolDayKind.UNKNOWN),
            ("No School 9-12", SchoolDayKind.UNKNOWN),
            ("No School 9th", SchoolDayKind.UNKNOWN),
            ("No School 11", SchoolDayKind.UNKNOWN),
            ("Early Release 6-8", SchoolDayKind.UNKNOWN),
            ("Early Release 1st", SchoolDayKind.UNKNOWN),
            ("Early Release 22nd", SchoolDayKind.UNKNOWN),
            ("Early Release 12x", SchoolDayKind.UNKNOWN),
            ("Early Release 8th grade", SchoolDayKind.UNKNOWN),
            ("Winter Break 2026", SchoolDayKind.UNKNOWN),
            # a slash date cannot be confirmed without the event's days
            ("No School 5/12", SchoolDayKind.UNKNOWN),
        ],
    )
    def test_a_number_is_neutral_only_as_a_clock_time(self, summary, kind):
        assert classify_summary(summary) is kind

    @pytest.mark.parametrize("word", sorted(CONTEXT_WORDS))
    def test_the_context_veto_holds_if_a_context_word_were_neutral(
        self, monkeypatch, word
    ):
        # The neutral allow-list already rejects every context word; this pins
        # the second line on its own by wrongly making the word neutral.
        widened = NEUTRAL_WORDS | frozenset(word.split())
        monkeypatch.setattr(school_days, "NEUTRAL_WORDS", widened)
        assert classify_summary(f"Snow Day {word}") is SchoolDayKind.UNKNOWN

    def test_full_width_letters_fold_to_their_ascii_reading(self):
        full_width = "\uff2e\uff2f \uff33\uff23\uff28\uff2f\uff2f\uff2c"
        assert classify_summary(full_width) is SchoolDayKind.NO_SCHOOL

    def test_bare_bell_schedule_is_not_a_phrase(self):
        assert "bell schedule" not in SCHOOL_DAY_KEYWORDS[SchoolDayKind.NORMAL]
        assert classify_summary("Bell Schedule") is SchoolDayKind.UNKNOWN

    def test_the_context_table_holds_the_reviewed_words(self):
        reviewed = {
            "no", "not", "non", "cancelled", "canceled", "closed", "closure",
            "without", "resume", "resumes", "resuming", "ends", "end", "ending",
            "before", "after", "back", "return", "returns", "make up", "makeup",
            "only", "attend", "attends", "in session", "open", "lifted",
            "except", "unless", "instead", "rescheduled", "postponed",
            "early", "late", "for",
        }  # fmt: skip
        assert reviewed == CONTEXT_WORDS

    def test_context_words_are_already_normalized(self):
        for word in CONTEXT_WORDS:
            assert word == " ".join(word.split())
            assert word.replace(" ", "").isalnum()
            assert word == word.casefold()

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


NO_SCHOOL = SchoolDayKind.NO_SCHOOL
EARLY = SchoolDayKind.EARLY_RELEASE
LATE = SchoolDayKind.LATE_START
NORMAL = SchoolDayKind.NORMAL
UNKNOWN = SchoolDayKind.UNKNOWN
MONDAY = date(2026, 10, 26)
THURSDAY = date(2026, 10, 29)


class TestDatedSummaries:
    """A weekday or date in a summary must be one of the event's own days."""

    @pytest.mark.parametrize(
        ("summary", "first", "last", "kind"),
        [
            ("No School Monday", MONDAY, MONDAY, NO_SCHOOL),
            ("No School - Good Friday", date(2026, 4, 3), date(2026, 4, 3), NO_SCHOOL),
            (
                "Snow Day - Tuesday, Jan 27th",
                date(2026, 1, 27),
                date(2026, 1, 27),
                NO_SCHOOL,
            ),
            (
                "No School Monday Dec 22",
                date(2025, 12, 22),
                date(2025, 12, 22),
                NO_SCHOOL,
            ),
            ("Early Release Wednesday", date(2026, 10, 28), date(2026, 10, 28), EARLY),
            ("Early Release Fri 11/26", date(2027, 11, 26), date(2027, 11, 26), EARLY),
            ("Early Release 11/26/27", date(2027, 11, 26), date(2027, 11, 26), EARLY),
            ("Late Start Monday", MONDAY, MONDAY, LATE),
            ("Regular Day Thursday", THURSDAY, THURSDAY, NORMAL),
            (
                "Winter Break Dec 22 - Jan 2",
                date(2025, 12, 22),
                date(2026, 1, 2),
                NO_SCHOOL,
            ),
            (
                "Thanksgiving Break Nov. 25-27",
                date(2026, 11, 25),
                date(2026, 11, 27),
                NO_SCHOOL,
            ),
            (
                "Spring Break March 23 - 27",
                date(2026, 3, 23),
                date(2026, 3, 27),
                NO_SCHOOL,
            ),
            (
                "Thanksgiving Break Wed - Fri",
                date(2026, 11, 25),
                date(2026, 11, 27),
                NO_SCHOOL,
            ),
        ],
    )
    def test_a_summary_naming_its_own_days_keeps_its_kind(
        self, summary, first, last, kind
    ):
        assert classify_summary(summary, (first, last)) is kind

    @pytest.mark.parametrize(
        ("summary", "first", "last"),
        [
            # an announcement or banner placed on another day
            ("No School Monday", THURSDAY, THURSDAY),
            ("No School Friday", THURSDAY, THURSDAY),
            ("Early Release Thursday", date(2026, 10, 28), date(2026, 10, 28)),
            ("Winter Break Dec 22 - Jan 2", date(2025, 11, 3), date(2025, 11, 3)),
            ("Early Release Fri 11/26", date(2027, 11, 25), date(2027, 11, 25)),
            ("No School 5/12", date(2026, 5, 13), date(2026, 5, 13)),
            # the weekday and the date disagree with each other
            ("No School Monday Dec 22", date(2026, 12, 22), date(2026, 12, 22)),
            # the year disagrees
            ("Early Release 11/26/26", date(2027, 11, 26), date(2027, 11, 26)),
            # a multi-day event that names only one of its ends
            ("No School Monday", MONDAY, date(2026, 10, 30)),
            ("Winter Break Dec 22", date(2025, 12, 22), date(2026, 1, 2)),
            # an impossible date, a bare month, an event longer than a year
            ("No School Feb 30", date(2026, 2, 28), date(2026, 3, 2)),
            ("Spring Break March", date(2026, 3, 23), date(2026, 3, 27)),
            ("Winter Break Dec 22 - Jan 2", date(2025, 12, 22), date(2027, 1, 2)),
        ],
    )
    def test_a_summary_naming_another_day_is_unknown(self, summary, first, last):
        assert classify_summary(summary, (first, last)) is UNKNOWN

    @pytest.mark.parametrize(
        "summary", ["No School Monday", "Winter Break Dec 22 - Jan 2", "No School 5/12"]
    )
    def test_a_dated_summary_without_its_days_is_unknown(self, summary):
        assert classify_summary(summary) is UNKNOWN

    def test_a_notice_posted_on_another_day_is_unknown_on_that_day(self):
        notice = all_day("2026-10-29", "2026-10-30", "No School Friday")
        day = only(run({BELL: [], DISTRICT: [notice]}))
        assert (day.date, day.kind) == (THURSDAY, UNKNOWN)

    def test_a_closure_on_the_day_it_names_is_a_closure(self):
        closure = all_day("2026-10-30", "2026-10-31", "No School Friday")
        day = only(run({BELL: [], DISTRICT: [closure]}))
        assert (day.date, day.kind) == (date(2026, 10, 30), NO_SCHOOL)

    def test_a_dated_break_is_checked_against_the_whole_event(self):
        # The window starts after the break does; the dates still match the
        # unclipped event, so every window day of it is a closure.
        brk = all_day("2026-12-22", "2027-01-03", "Winter Break Dec 22 - Jan 2")
        days = run(
            {BELL: [], DISTRICT: [brk]},
            first_day=date(2026, 12, 28),
            last_day=date(2027, 1, 5),
        )
        assert [d.date for d in days] == [
            date(2026, 12, 28) + timedelta(days=n) for n in range(6)
        ]
        assert {d.kind for d in days} == {NO_SCHOOL}

    def test_a_recurring_dated_bell_event_needs_its_weekday(self):
        days = run(
            {
                BELL: [
                    timed("2026-10-26", "10:00", "15:00", "Late Start Monday"),
                    timed("2026-10-27", "10:00", "15:00", "Late Start Monday"),
                ],
                DISTRICT: [],
            }
        )
        assert [(d.date, d.kind, d.start) for d in days] == [
            (MONDAY, LATE, utc(2026, 10, 26, 17)),
            (date(2026, 10, 27), UNKNOWN, utc(2026, 10, 27, 17)),
        ]


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

    def test_a_duplicated_bell_event_is_one_schedule(self):
        regular = timed("2026-10-23", "08:00", "15:00", "Regular Day")
        day = only(run({BELL: [regular, dict(regular)], DISTRICT: []}))
        assert (day.kind, day.start, day.end) == (
            NORMAL,
            utc(2026, 10, 23, 15),
            utc(2026, 10, 23, 22),
        )

    def test_overlapping_regular_bell_events_are_unknown(self):
        # A stale and an updated schedule overlap; neither end is trustworthy.
        day = only(
            run(
                {
                    BELL: [
                        timed("2026-10-23", "08:00", "15:00", "Regular Day"),
                        timed("2026-10-23", "08:30", "14:00", "Regular Day"),
                    ],
                    DISTRICT: [],
                }
            )
        )
        assert (day.kind, day.start, day.end) == (
            UNKNOWN,
            utc(2026, 10, 23, 15),
            utc(2026, 10, 23, 22),
        )

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


class TestMapperClaims:
    """Review claims about the mapper, pinned whether or not they were real."""

    def test_a_utc_midnight_closure_is_two_partial_local_days(self):
        # A closure sent as UTC midnight to UTC midnight covers 17:00 Sunday to
        # 17:00 Monday in Pacific time: neither local day is whole, so both are
        # UNKNOWN, never NO_SCHOOL, and the all-day rule does not apply.
        event = {
            "start": "2026-10-26T00:00:00Z",
            "end": "2026-10-27T00:00:00Z",
            "summary": "No School",
        }
        days = run({BELL: [], DISTRICT: [event]})
        assert [(d.date, d.kind) for d in days] == [
            (date(2026, 10, 25), SchoolDayKind.UNKNOWN),
            (date(2026, 10, 26), SchoolDayKind.UNKNOWN),
        ]

    def test_a_timed_normal_district_event_gives_no_bell_times(self):
        day = only(
            run(
                {
                    BELL: [],
                    DISTRICT: [timed("2026-10-23", "08:00", "15:00", "Regular Day")],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN
        assert day.start == day.end == utc(2026, 10, 23, 7)

    def test_an_all_day_normal_district_event_is_unknown(self):
        day = only(
            run(
                {
                    BELL: [],
                    DISTRICT: [all_day("2026-10-23", "2026-10-24", "Regular Day")],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN

    def test_a_bell_event_clipped_at_the_window_start_gives_no_times(self):
        event = {
            "start": "2026-10-22T20:00:00-07:00",
            "end": "2026-10-23T15:00:00-07:00",
            "summary": "Regular Day",
        }
        day = only(run({BELL: [event], DISTRICT: []}))
        assert (day.date, day.kind) == (FRIDAY, SchoolDayKind.UNKNOWN)
        assert day.start == day.end == utc(2026, 10, 23, 7)

    def test_a_bell_event_clipped_at_the_window_end_gives_no_times(self):
        event = {
            "start": "2026-10-23T08:00:00-07:00",
            "end": "2026-10-24T02:00:00-07:00",
            "summary": "Regular Day",
        }
        day = only(run({BELL: [event], DISTRICT: []}, last_day=FRIDAY))
        assert (day.date, day.kind) == (FRIDAY, SchoolDayKind.UNKNOWN)
        assert day.start == day.end == utc(2026, 10, 23, 7)

    def test_a_zero_day_all_day_event_never_reaches_the_mapper(self):
        calendars = {
            BELL: [],
            DISTRICT: [all_day("2026-10-23", "2026-10-23", "No School")],
        }
        with pytest.raises(PydanticValidationError):
            snapshot(calendars)


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

    def test_timed_early_and_late_bell_events_on_one_day_are_unknown(self):
        # Both kinds carry their own bell span, so only the early-plus-late
        # rule (not the missing-bell downgrade) can make this day UNKNOWN.
        day = only(
            run(
                {
                    BELL: [
                        timed("2026-10-23", "10:00", "15:00", "Late Start"),
                        timed("2026-10-23", "08:00", "12:00", "Early Release"),
                    ],
                    DISTRICT: [],
                }
            )
        )
        assert (day.kind, day.start, day.end) == (
            UNKNOWN,
            utc(2026, 10, 23, 15),
            utc(2026, 10, 23, 22),
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

    def test_two_different_early_release_bell_events_are_unknown(self):
        # A stale and an updated dismissal time: the later one must not win.
        day = only(
            run(
                {
                    BELL: [
                        timed("2026-10-23", "08:00", "12:00", "Early Release"),
                        timed("2026-10-23", "08:00", "13:30", "Early Release"),
                    ],
                    DISTRICT: [],
                }
            )
        )
        assert (day.kind, day.start, day.end) == (
            UNKNOWN,
            utc(2026, 10, 23, 15),
            utc(2026, 10, 23, 20, 30),
        )

    def test_two_separate_late_start_bell_events_are_unknown(self):
        day = only(
            run(
                {
                    BELL: [
                        timed("2026-10-23", "10:00", "11:00", "Late Start"),
                        timed("2026-10-23", "12:00", "15:00", "Late Start"),
                    ],
                    DISTRICT: [],
                }
            )
        )
        assert day.kind is UNKNOWN

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

    def test_a_timed_closure_beside_a_regular_bell_day_is_unknown(self):
        day = only(
            run(
                {
                    BELL: [timed("2026-10-26", "08:00", "15:00", "Regular Day")],
                    DISTRICT: [timed("2026-10-26", "08:00", "12:00", "No School")],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN

    def test_a_partial_day_closure_alone_is_unknown(self):
        day = only(
            run(
                {
                    BELL: [],
                    DISTRICT: [timed("2026-10-26", "12:00", "15:00", "School Closed")],
                }
            )
        )
        assert day.kind is SchoolDayKind.UNKNOWN

    def test_a_timed_closure_covering_the_whole_day_is_a_closure(self):
        closure = {
            "start": "2026-10-26T00:00:00-07:00",
            "end": "2026-10-27T00:00:00-07:00",
            "summary": "Snow Day",
        }
        day = only(
            run(
                {
                    BELL: [timed("2026-10-26", "08:00", "15:00", "Regular Day")],
                    DISTRICT: [closure],
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

    def test_a_timed_closure_over_the_whole_fall_back_day_is_a_closure(self):
        # 2026-11-01 lasts 25 hours: midnight -07:00 to midnight -08:00.
        closure = {
            "start": "2026-11-01T00:00:00-07:00",
            "end": "2026-11-02T00:00:00-08:00",
            "summary": "No School",
        }
        day = only(run({BELL: [], DISTRICT: [closure]}))
        assert (day.date, day.kind) == (date(2026, 11, 1), NO_SCHOOL)

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

    def test_an_instantaneous_event_gives_no_bell_times(self):
        # A zero-length bell event carries no usable dismissal time, so the
        # day is downgraded to UNKNOWN and carries the zero-length local
        # midnight placeholder (07:00 UTC in daylight time), not a school
        # block at 12:00.
        moment = timed("2026-10-23", "12:00", "12:00", "Early Release")
        day = only(run({BELL: [moment], DISTRICT: []}))
        assert day.kind is SchoolDayKind.UNKNOWN
        assert day.start == day.end == utc(2026, 10, 23, 7)

    def test_an_instantaneous_regular_event_is_unknown(self):
        moment = timed("2026-10-23", "08:00", "08:00", "Regular Day")
        day = only(run({BELL: [moment], DISTRICT: []}))
        assert day.kind is SchoolDayKind.UNKNOWN


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

    @pytest.mark.parametrize("days_before", [1, 22])
    def test_an_inverted_window_is_refused(self, days_before):
        last = FRIDAY - timedelta(days=days_before)
        with pytest.raises(ValidationError, match="ends before it starts"):
            DayWindow(FRIDAY, last)

    def test_a_one_day_window_is_accepted(self):
        assert FRIDAY in DayWindow(FRIDAY, FRIDAY)

    def test_the_longest_window_is_accepted_and_one_more_day_refused(self):
        longest = FRIDAY + timedelta(days=MAX_WINDOW_DAYS - 1)
        assert longest in DayWindow(FRIDAY, longest)
        too_long = longest + timedelta(days=1)
        with pytest.raises(ValidationError, match="too many days"):
            DayWindow(FRIDAY, too_long)

    @pytest.mark.parametrize(
        ("first", "last"),
        [
            (datetime(2026, 10, 23, tzinfo=UTC), datetime(2026, 10, 24, tzinfo=UTC)),
            (FRIDAY, datetime(2026, 10, 24, tzinfo=UTC)),
            (datetime(2026, 10, 23, tzinfo=UTC), date(2026, 10, 24)),
        ],
    )
    def test_a_window_of_instants_is_refused(self, first, last):
        with pytest.raises(ValidationError, match="local days, not instants"):
            DayWindow(first, last)

    def test_window_membership_is_inclusive_and_dates_only(self):
        window = DayWindow(FRIDAY, date(2026, 10, 24))
        assert FRIDAY in window
        assert date(2026, 10, 24) in window
        assert date(2026, 10, 25) not in window
        assert "2026-10-23" not in window
        assert datetime(2026, 10, 23, 5, tzinfo=UTC) not in window


def constructed(
    bell_events: tuple[HaCalendarEvent, ...],
) -> HaSnapshotPayload:
    """Build a payload without validation (times the schema would refuse)."""
    return HaSnapshotPayload.model_construct(
        fetched_at=utc(2026, 10, 23, 13),
        calendars={
            BELL: HaCalendar.model_construct(events=bell_events),
            DISTRICT: HaCalendar.model_construct(events=()),
        },
        zones=(),
    )


EXTREME_ALL_DAY = HaCalendarEvent.model_construct(
    start=date(1, 1, 1), end=date(9999, 12, 31), summary="Winter Break"
)
EXTREME_TIMED = HaCalendarEvent.model_construct(
    start=datetime(1, 1, 1, tzinfo=UTC),
    end=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
    summary="Winter Break",
)


@pytest.fixture
def listed_days(monkeypatch) -> list[int]:
    """Count every local day the mapper lists for an event (work done)."""
    counted = [0]
    original = school_days._days_between

    def counting(first: date, last: date):
        for day in original(first, last):
            counted[0] += 1
            yield day

    monkeypatch.setattr(school_days, "_days_between", counting)
    return counted


class TestExtremeEvents:
    WINDOW = DayWindow(FRIDAY, date(2026, 11, 8))
    WINDOW_DAYS = 17

    @pytest.mark.parametrize("tz", [PACIFIC, TOKYO], ids=["west", "east"])
    @pytest.mark.parametrize(
        "event", [EXTREME_ALL_DAY, EXTREME_TIMED], ids=["all_day", "timed"]
    )
    def test_an_event_spanning_every_year_maps_only_the_window(
        self, tz, event, listed_days
    ):
        days = map_school_snapshot(
            constructed((event,)), (MIDDLE,), tz=tz, window=self.WINDOW
        ).days
        assert listed_days[0] == self.WINDOW_DAYS
        assert [d.date for d in days] == [
            date.fromordinal(n)
            for n in range(FRIDAY.toordinal(), date(2026, 11, 8).toordinal() + 1)
        ]
        assert {d.kind for d in days} == {SchoolDayKind.NO_SCHOOL}

    @pytest.mark.parametrize("tz", [PACIFIC, TOKYO], ids=["west", "east"])
    def test_a_full_calendar_of_extreme_events_stays_bounded(self, tz, listed_days):
        events = (EXTREME_ALL_DAY, EXTREME_TIMED) * (MAX_EVENTS_PER_CALENDAR // 2)
        result = map_school_snapshot(
            constructed(events), (MIDDLE,), tz=tz, window=self.WINDOW
        )
        assert listed_days[0] == len(events) * self.WINDOW_DAYS
        assert len(result.days) == self.WINDOW_DAYS

    def test_events_wholly_outside_the_window_give_no_rows(self):
        before = HaCalendarEvent.model_construct(
            start=date(1, 1, 1), end=date(2, 1, 1), summary="No School"
        )
        after = HaCalendarEvent.model_construct(
            start=datetime(9999, 12, 30, tzinfo=UTC),
            end=datetime(9999, 12, 31, tzinfo=UTC),
            summary="No School",
        )
        result = map_school_snapshot(
            constructed((before, after)), (MIDDLE,), tz=TOKYO, window=self.WINDOW
        )
        assert result == SchoolDayMapping(
            days=(), schools_without_days=(MIDDLE,), events_total=2, events_in_window=0
        )

    def test_a_window_at_the_end_of_time_is_a_mapping_error(self):
        window = DayWindow(date(9999, 12, 30), date(9999, 12, 31))
        payload = constructed((EXTREME_ALL_DAY,))
        with pytest.raises(SnapshotMappingError) as info:
            map_school_snapshot(payload, (MIDDLE,), tz=TOKYO, window=window)
        assert info.value.error_code == "INVALID_TIME"


class TestWindowEdges:
    """An instantaneous event belongs to the window from start to exclusive end."""

    def test_an_instant_at_the_window_start_is_kept(self):
        moment = timed("2026-10-23", "00:00", "00:00", "No School")
        day = only(run({BELL: [], DISTRICT: [moment]}))
        assert (day.date, day.kind) == (FRIDAY, UNKNOWN)

    def test_an_instant_at_the_exclusive_window_end_is_dropped(self):
        moment = timed("2026-11-09", "00:00", "00:00", "No School", "-08:00")
        assert run({BELL: [], DISTRICT: [moment]}) == ()

    def test_an_instant_just_before_the_window_end_is_kept(self):
        moment = timed("2026-11-08", "23:59", "23:59", "No School", "-08:00")
        day = only(run({BELL: [], DISTRICT: [moment]}))
        assert day.date == date(2026, 11, 8)


class TestSnapshotMapping:
    """A school without rows is reported, never raised and never silent."""

    def test_a_quiet_period_names_every_school_without_rows(self):
        result = mapped({BELL: [], HIGH_BELL: [], DISTRICT: []}, schools=(MIDDLE, HIGH))
        assert result.is_empty
        assert result.days == ()
        assert result.schools_without_days == (MIDDLE, HIGH)
        assert (result.events_total, result.events_in_window) == (0, 0)

    def test_only_the_school_without_rows_is_named(self):
        result = mapped(
            {
                BELL: [timed("2026-10-23", "08:00", "15:00", "Regular Day")],
                HIGH_BELL: [],
                DISTRICT: [],
            },
            schools=(MIDDLE, HIGH),
        )
        assert not result.is_empty
        assert result.schools_without_days == (HIGH,)

    def test_events_outside_the_window_are_counted_but_give_no_rows(self):
        result = mapped(
            {
                BELL: [timed("2027-01-05", "08:00", "15:00", "Regular Day")],
                DISTRICT: [all_day("2026-10-26", "2026-10-27", "No School")],
            }
        )
        assert (result.events_total, result.events_in_window) == (2, 1)
        assert result.schools_without_days == ()

    def test_a_shared_district_event_is_counted_once(self):
        result = mapped(
            {
                BELL: [],
                HIGH_BELL: [],
                DISTRICT: [all_day("2026-10-26", "2026-10-27", "No School")],
            },
            schools=(MIDDLE, HIGH),
        )
        assert (result.events_total, result.events_in_window) == (1, 1)

    def test_kind_counts_count_rows(self):
        result = mapped(
            {
                HIGH_BELL: [timed("2026-10-23", "08:00", "15:00", "Regular Day")],
                DISTRICT: [all_day("2026-10-26", "2026-10-27", "Fun Run")],
            },
            schools=(HIGH,),
        )
        assert result.kind_counts() == {NORMAL: 2, UNKNOWN: 2}


class TestEntities:
    def test_an_unconfigured_calendar_is_refused_without_echo(self):
        payload = snapshot({BELL: [], DISTRICT: [], "calendar.private_unlisted": []})
        with pytest.raises(SnapshotMappingError) as caught:
            map_school_snapshot(
                payload, (MIDDLE,), tz=PACIFIC, window=TestExtremeEvents.WINDOW
            )
        assert caught.value.error_code == "UNKNOWN_CALENDAR"
        assert "private_unlisted" not in str(caught.value.to_dict())

    def test_a_configured_calendar_missing_from_the_payload_is_refused(self):
        payload = snapshot({BELL: []})
        with pytest.raises(SnapshotMappingError) as caught:
            map_school_snapshot(
                payload, (MIDDLE,), tz=PACIFIC, window=TestExtremeEvents.WINDOW
            )
        assert caught.value.error_code == "MISSING_CALENDAR"
        assert "district" not in str(caught.value.to_dict())

    def test_the_error_is_a_validation_error(self):
        assert issubclass(SnapshotMappingError, ValidationError)

    def test_no_configured_school_is_refused(self):
        payload = snapshot({BELL: [], DISTRICT: []})
        with pytest.raises(SnapshotMappingError) as caught:
            map_school_snapshot(
                payload, (), tz=PACIFIC, window=TestExtremeEvents.WINDOW
            )
        assert caught.value.error_code == SnapshotMappingCode.NO_SCHOOLS

    def test_a_person_in_two_schools_is_refused_without_echo(self):
        twice = HaSchool(
            location_id="high-school",
            person_ids=("sam",),
            bell_calendars=(HIGH_BELL,),
            district_calendars=(DISTRICT,),
        )
        payload = snapshot({BELL: [], HIGH_BELL: [], DISTRICT: []})
        with pytest.raises(SnapshotMappingError) as caught:
            map_school_snapshot(
                payload, (MIDDLE, twice), tz=PACIFIC, window=TestExtremeEvents.WINDOW
            )
        assert caught.value.error_code == "DUPLICATE_PERSON"
        assert "sam" not in str(caught.value.to_dict())

    def test_every_code_is_its_own_name(self):
        assert {code.value for code in SnapshotMappingCode} == {
            code.name for code in SnapshotMappingCode
        }
