"""Minimal local stubs for recurring-ical-events (the package ships none)."""

from datetime import date, datetime

from icalendar import Calendar, Component

class CalendarQuery:
    def between(
        self, start: date | datetime, stop: date | datetime
    ) -> list[Component]: ...

def of(a_calendar: Calendar) -> CalendarQuery: ...
