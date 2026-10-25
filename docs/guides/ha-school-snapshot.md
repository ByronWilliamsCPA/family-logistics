---
title: "Home Assistant School Snapshot"
schema_type: common
status: published
owner: core-maintainer
purpose: "How Home Assistant delivers school schedules to Family Logistics, and how they are classified."
tags:
  - guide
  - home_assistant
  - configuration
---

Home Assistant pushes the school and district calendars to Family Logistics
([ADR 0007](../ADRs/0007-ha-initiated-data-exchange.md)). The app never calls
Home Assistant and holds no Home Assistant credential. Each push replaces the
stored school snapshot: when each child's school day starts and ends, or that
there is no school. Storing the snapshot does not by itself feed an
assessment; see [Using the snapshot in an assessment](#using-the-snapshot-in-an-assessment).

Every name, entity ID and time on this page belongs to the fictional household
(Morgan, Riley, Alex, Sam, Jordan and Casey). Keep real entity IDs in the
private deployment repository.

## Configure the schools

`FL_HA_SCHOOLS` is a JSON list. Each entry maps Home Assistant calendar
entities to a registry location and the registry people who attend it:

```json
[
  {
    "location_id": "middle-school",
    "person_ids": ["sam"],
    "bell_calendars": ["calendar.middle_school_bell"],
    "district_calendars": ["calendar.district"]
  },
  {
    "location_id": "high-school",
    "person_ids": ["alex", "jordan"],
    "bell_calendars": ["calendar.high_school_bell"],
    "district_calendars": ["calendar.district"]
  }
]
```

- **Bell calendars** carry the timed bell schedule. They are the only source of
  start and end times.
- **District calendars** carry closures and short days. They classify a day
  but never supply times. One district calendar may serve several schools.
- Only `calendar.*` entity IDs are accepted, so a person tracker, device
  tracker or sensor can never be configured. A calendar has one role; a person
  belongs to one school; a location is listed once; at most 8 schools.
- `location_id` and every `person_ids` entry must exist in the registry when a
  snapshot arrives, or the ingest answers 503 and stores nothing.

With `FL_HA_SCHOOLS` empty (the default) the ingest answers 503. `FL_AUDIT_KEY`
must also be set: every accepted snapshot writes an audit row.

## Send the snapshot

`POST /api/v1/ingest/ha-snapshot` with `Authorization: Bearer <FL_HA_TOKEN>`
and `Content-Type: application/json`. Request events from today through at
least `FL_COLLECT_DAYS_AHEAD` plus 2 days, and push at least once a day: an
assessment given a snapshot older than 26 hours raises DATA-011 and leaves
school-dependent checks incomplete.

A Home Assistant automation can call `calendar.get_events` once for every
configured calendar and post the combined response. A sketch, to adapt and
test in the deployment:

```yaml
rest_command:
  fl_school_snapshot:
    url: !secret fl_school_snapshot_url
    method: POST
    headers:
      Authorization: !secret fl_bearer_header
    content_type: "application/json"
    payload: "{{ body | to_json }}"

automation:
  - alias: "Push school snapshot to Family Logistics"
    triggers:
      - trigger: time_pattern
        hours: "/6"
    actions:
      - action: calendar.get_events
        target:
          entity_id:
            - calendar.middle_school_bell
            - calendar.high_school_bell
            - calendar.district
        data:
          start_date_time: "{{ today_at() }}"
          duration:
            days: 17
        response_variable: calendars
      - action: rest_command.fl_school_snapshot
        data:
          body:
            fetched_at: "{{ now().isoformat() }}"
            calendars: "{{ calendars }}"
            zones: ["zone.home", "zone.middle_school"]
```

!!! warning "Deployment check: confirm Home Assistant's timed format"
    Timed `start` and `end` values must be ISO 8601 date-times with a `T`
    separator and a UTC offset (`2026-10-23T08:00:00-07:00`). This format has
    not yet been confirmed against a real `calendar.get_events` response.
    `#VERIFY`: before relying on the push, capture one real response in the
    deployment and check its timed values. If Home Assistant sends a space
    instead of `T`, or no offset, every snapshot fails closed with 422 and the
    stored snapshot goes stale (DATA-011); fix the automation's template, not
    the app's parser, which stays strict on purpose.

### Payload

```json
{
  "fetched_at": "2026-10-23T06:00:00-07:00",
  "calendars": {
    "calendar.middle_school_bell": {
      "events": [
        {
          "start": "2026-10-23T08:00:00-07:00",
          "end": "2026-10-23T15:00:00-07:00",
          "summary": "Regular Day"
        },
        {
          "start": "2026-10-28T08:00:00-07:00",
          "end": "2026-10-28T12:30:00-07:00",
          "summary": "Early Release"
        }
      ]
    },
    "calendar.high_school_bell": {
      "events": [
        {
          "start": "2026-10-23T07:30:00-07:00",
          "end": "2026-10-23T14:30:00-07:00",
          "summary": "Regular Bell Schedule"
        }
      ]
    },
    "calendar.district": {
      "events": [
        {
          "start": "2026-10-26",
          "end": "2026-10-27",
          "summary": "No School - Staff Development Day",
          "description": "Ignored by Family Logistics"
        },
        {
          "start": "2026-10-28",
          "end": "2026-10-29",
          "summary": "Early Release"
        }
      ]
    }
  },
  "zones": ["zone.home", "zone.middle_school"]
}
```

| Field | Rule |
|-------|------|
| `fetched_at` | ISO 8601 date-time with a UTC offset; no more than 5 minutes ahead of the server clock |
| `calendars` | 1 to 16 entries, keyed by `calendar.*` entity ID; exactly the calendars in `FL_HA_SCHOOLS` (an extra one or a missing one is refused) |
| `calendars.*.events` | Up to 1000 events per calendar |
| `start`, `end` | Both `YYYY-MM-DD` (all-day, `end` exclusive and after `start`) or both date-times with a UTC offset (`end` not before `start`); at most 40 characters; years 2000 to 2100 |
| `summary` | Required string, up to 500 characters; the only text the app reads |
| `description`, `location` | Optional, up to 4000 and 500 characters; accepted and ignored, never logged or stored |
| `zones` | Optional, up to 64 `zone.*` entity IDs; each must be the `ha_zone_entity_id` of a registry location. Coordinates are not accepted |

Unknown keys are refused at every level. The body is limited to 1 MiB.

With the configuration above, this payload gives nine rows: a normal Friday
for all three children, a closure on Monday 26 October for all three, an early
release for Sam on Wednesday 28 October, and an unknown day for Alex and Jordan
on the 28th, because the district announces an early release that the high
school's bell calendar does not schedule.

### Responses

| Status | Body | Meaning |
|--------|------|---------|
| 200 | `{"status": "stored", "schools": 2, "days": 9, "fetched_at": "...", "received_at": "..."}` | Stored. `fetched_at` is the stored freshness, `min(fetched_at, received_at)` |
| 200 | same, with `"status": "unchanged"` | Identical re-post; nothing written, the stored freshness is returned |
| 401 / 429 | `{"detail": "Unauthorized"}` | Bad token, or the client is locked out after repeated failures |
| 409 | `{"detail": "A newer school snapshot is already stored"}` | `fetched_at` is not newer than the stored snapshot |
| 413 | `{"detail": "Payload too large"}` | Body over 1 MiB |
| 415 | `{"detail": "Expected application/json"}` | Wrong content type |
| 422 | `{"detail": "Invalid snapshot", "errors": [{"loc": ["calendars", "*", "events", 0, "start"], "type": "value_error"}]}` | Schema failure; locations keep only field names and indexes (`*` replaces an entity ID) |
| 422 | `{"detail": "Invalid snapshot", "code": "UNKNOWN_ZONE"}` | A zone is not any registry location's zone |
| 422 | same, `"code": "EMPTY_SNAPSHOT"` | No configured calendar has a single event overlapping the window |
| 422 | same, `"code": "UNKNOWN_CALENDAR"` or `"MISSING_CALENDAR"` | A calendar is not in `FL_HA_SCHOOLS`, or a configured one is missing |
| 422 | same, `"code": "FETCHED_AT_IN_FUTURE"` | `fetched_at` is more than 5 minutes ahead of the server clock |
| 422 | same, `"code": "INVALID_TIME"` | A time cannot be placed in `FL_TIMEZONE` or stored |
| 503 | `{"detail": "School snapshot ingest is not available"}` | No school configured, no audit key, or `FL_HA_SCHOOLS` names a location or person the registry lacks |
| 503 | `{"detail": "School snapshot store is busy"}`, header `Retry-After: 5` | Another writer held the database past the lock timeout; retry later |

When a snapshot has several problems, the first check that fails decides the
answer, in this order:

1. 401 or 429 (token), then 415 (content type), 413 (size) and 422 with
   `errors` (schema).
2. 503 for no school or no audit key, then 422 `FETCHED_AT_IN_FUTURE`.
3. 422 `UNKNOWN_CALENDAR`, `MISSING_CALENDAR` or `INVALID_TIME` while mapping
   the calendars, then 422 `EMPTY_SNAPSHOT`, then 422 `INVALID_TIME` for a
   time that cannot be stored.
4. Under the write lock (503 `Retry-After` if it is not granted): 503 for a
   registry mismatch, then 422 `UNKNOWN_ZONE`.
5. Only then the comparison with the stored snapshot: 200 unchanged for an
   identical re-post, else 409 if it is not newer.

So an older snapshot with an unknown zone answers 422, and one sent under a
stale configuration answers 503, never 409.

No response and no log line repeats any part of the payload. On every error
nothing is stored and no audit row is written.

### Empty snapshots

A Home Assistant calendar entity that is unavailable returns no events, and an
outage usually hits every calendar at once. A snapshot in which **no**
configured calendar has an event overlapping the window (today through
`FL_COLLECT_DAYS_AHEAD` plus 2, after clipping) is refused with
`EMPTY_SNAPSHOT`. The stored snapshot is kept and ages until DATA-011, rather
than being replaced by an empty one. An event that spans the window counts: a
district calendar holding one all-day "Summer Break" across the whole window
is accepted and gives no-school days. A snapshot in which only some calendars
are empty is accepted.

## How days are classified

Each event summary is case-folded, every character other than a letter or
digit becomes a space, and the result is matched against whole phrases:

| Kind | Phrases |
|------|---------|
| No school | no school, school closed, schools closed, district closed, no students, non student day, student free day, teacher work day, teacher workday, professional development day, staff development day, winter break, spring break, fall break, thanksgiving break, mid winter break, summer break, holiday break, school holiday, no classes, snow day, emergency closure |
| Early release | early release, early dismissal, minimum day, half day, short day, early out, shortened day |
| Late start | late start, late arrival, delayed start, delayed opening, two hour delay, 2 hour delay |
| No school (more) | no school for students, no school for all students |
| Normal | regular day, regular schedule, regular bell schedule, normal day, normal schedule, normal bell schedule, school day, full day, regular bells |

A false "no school" is the dangerous direction: it would let a ride check
pass on a day a child is at school. A wrong "unknown" only raises SCHOOL-009.
So every ambiguous summary is unknown:

- A summary naming exactly one kind takes that kind ("No School Day" is no
  school: a normal phrase that overlaps a closure phrase is ignored).
- A summary naming two kinds is unknown, including a normal phrase beside a
  separate closure or short-day phrase ("Regular Day (no school Friday)").
- A summary containing a context word outside the matched phrases is unknown,
  whatever it names. Context words mark negation, a period boundary or a
  subset of students:

  | Group | Words |
  |-------|-------|
  | Negation and cancellation | no, not, non, without, cancelled, canceled, closed, closure, lifted, open, except, unless, instead, rescheduled, postponed |
  | Period boundary | resume, resumes, resuming, end, ends, ending, before, after, back, return, returns, make up, makeup, in session |
  | Some students or part of the day | early, late, for, only, attend, attends |

  A context word inside a matched phrase does not count: "no" in "No School",
  "early" in "Early Release", "for" in "No School for Students". So "Winter
  Break Ends", "Early release cancelled", "No school for seniors" and "Snow
  Day Make-Up Day" are unknown.
- Anything else is unknown. Plain "holiday" is not a closure ("Holiday
  Concert" is unknown), and a plain "bell schedule" (as in "Modified Bell
  Schedule") is not a normal day. An unrecognized event is never read as a
  normal day.

Then each school's events are clipped to the window and grouped by local day
(`FL_TIMEZONE`). All-day events cover each date up to their exclusive end;
timed events cover every local day they overlap, end exclusive, across
daylight-saving changes. An event that reaches outside the window only
contributes the days inside it, so a long break costs no more than the window.

| Events on the day | Kind | Times |
|-------------------|------|-------|
| Any unknown event | unknown | span of the bell calendar's timed events that day, else zero length at local midnight |
| No school covering the whole local day, alone or with normal events | no school | zero length at local midnight |
| No school that covers only part of the day (a timed closure) | unknown | as for unknown |
| No school with early release or late start | unknown | as for unknown |
| Early release and late start | unknown | as for unknown |
| Early release (with or without normal events) | early release | span of the bell calendar's timed early-release events |
| Late start (with or without normal events) | late start | span of the bell calendar's timed late-start events |
| Normal only | normal | span of the bell calendar's timed normal events |
| None | no row | the engine reports SCHOOL-009 if the child has school transport that day |

A normal, early-release or late-start day needs a timed bell-calendar event of
its own kind, longer than zero, that starts and ends on that local day; without
one it becomes unknown rather than borrowing another kind's times. Unknown days raise
SCHOOL-009 ("the school schedule is uncertain"). Days outside today through
`FL_COLLECT_DAYS_AHEAD` plus 2 are dropped.

## What is stored

Only one row per person and day (person, date, school location, start, end,
kind) and the snapshot's freshness (`fetched_at`, `received_at`, a content
hash and a day count). Event text, descriptions, locations, entity IDs and
zones are never stored. Each accepted snapshot writes one audit row whose
hashed state holds counts and freshness only. School-day rows are kept for 30
days after the day ends (proposed; see the
[privacy spec](../privacy/privacy-and-retention-spec.md)). When retention
purges rows, the stored day count and content hash are updated with them, so a
re-post of the purged snapshot answers 409 rather than "unchanged".

## Using the snapshot in an assessment

Today the snapshot is stored and nothing more: nothing in the app yet passes
it to an assessment. Each run must be given it explicitly:

```python
repo = SchoolSnapshotRepository(engine)
assessment_service.run(trigger="daily", school=repo.latest())
```

Only a run given the snapshot sees school days and the Home Assistant
freshness (and so DATA-011 for a snapshot older than 26 hours). A run given no
snapshot has no school-day data; after the missing-snapshot fix lands (PR 26,
`fix/missing-school-snapshot-incomplete`), such a run is reported incomplete
(DATA-011) instead of possibly clear.

Roadmap:

- **M3 (scheduler)**: every scheduled run passes `school=repo.latest()`.
- **M4 (on-demand API)**: every on-demand run passes `school=repo.latest()`.
- **PR 26**: a run given no snapshot is incomplete (DATA-011).
