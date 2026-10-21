"""iCloud CalDAV collector.

TODO(M1): discover calendars, run sync-token/ETag incremental sync with a safe
full-window fallback (14 days ahead, 2 back), expand recurrences preserving
UID + RECURRENCE-ID, classify occurrences (single, series instance, override)
and hand notes to ``domain.parser``. Every request goes through
``adapters.caldav.readonly_transport``. A network failure is recorded as a
failed source attempt, never as an empty calendar.
"""
