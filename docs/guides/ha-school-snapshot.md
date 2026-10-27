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
  tracker or sensor can never be configured. A calendar has one role, and a bell
  calendar belongs to one school (only a district calendar may serve several);
  a person belongs to one school; a location is listed once; at most 8
  schools. A repeated JSON key in the setting is rejected.
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

### Secure the endpoint

The bearer token and the children's school schedules cross the network, so
protect both:

- Generate `FL_HA_TOKEN` randomly, for example with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`. The app needs
  at least 32 printable ASCII characters with no spaces.
- Store the header in Home Assistant's `secrets.yaml` as the full value,
  `Bearer <token>`, not the bare token.
- Send the snapshot over HTTPS only, through the reverse proxy that admits the
  Home Assistant host alone (see [ADR 0007](../ADRs/0007-ha-initiated-data-exchange.md)).
  Over plain HTTP the token and the schedules travel in cleartext, and a
  captured request could be replayed: the payload carries no nonce or
  signature, so TLS and the network rule are the controls.
- To rotate, change `FL_HA_TOKEN` and the Home Assistant secret together. Until
  both match, every push answers 401 and the stored snapshot ages.

### What the sender must guarantee

The app cannot tell a calendar that is empty from one that failed to load, so
the sender carries that contract:

- An empty `events` list means the calendar was read successfully and has no
  events in the window.
- Home Assistant must **not** post a snapshot if any calendar fetch failed. A
  failed fetch that is posted as an empty list is stored as "no school" for that
  school's days. Skip the push and let the stored snapshot age (DATA-011)
  rather than send a partial one.
- A duplicate JSON key anywhere in the body is refused, because most parsers
  keep only the last value and would silently drop the first.
- The body is at most 1 MiB, with at most 128 calendars and 20,000 events in
  total (and 1000 per calendar).

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
| `calendars` | 1 to 128 entries, keyed by `calendar.*` entity ID; exactly the calendars in `FL_HA_SCHOOLS` (an extra one or a missing one is refused) |
| `calendars.*.events` | Up to 1000 events per calendar and 20,000 in all calendars; an empty list means the calendar was read and had no events |
| `start`, `end` | Both `YYYY-MM-DD` (all-day, `end` exclusive and after `start`) or both date-times with a UTC offset (`end` not before `start`); at most 40 characters; years 2000 to 2100 |
| `summary` | Required string, up to 500 characters; the only text the app reads |
| `description`, `location` | Optional, up to 4000 and 500 characters; accepted and ignored, never logged or stored |
| `zones` | Optional, up to 64 `zone.*` entity IDs; each must be the `ha_zone_entity_id` of a registry location. Coordinates are not accepted |

Unknown keys are refused at every level, and so is a duplicate key. The body is limited to 1 MiB.

With the configuration above, this payload gives nine rows: a normal Friday
for all three children, a closure on Monday 26 October for all three, an early
release for Sam on Wednesday 28 October, and an unknown day for Alex and Jordan
on the 28th, because the district announces an early release that the high
school's bell calendar does not schedule.

### Responses

| Status | Body | Meaning |
|--------|------|---------|
| 200 | `{"status": "stored", "schools": 2, "days": 9, "schools_without_days": [], "fetched_at": "...", "received_at": "..."}` | Stored. `fetched_at` is the stored freshness, `min(fetched_at, received_at)`. `schools_without_days` lists the `location_id` of each configured school that got no day rows, in configuration order (see [Schools without days](#schools-without-days)) |
| 200 | same, with `"status": "unchanged"` | Identical re-post; no school rows are rewritten and the stored freshness is returned. An audit row (`ha_snapshot_unchanged`) is still written |
| 400 | `{"detail": "Bad Request"}` | `Content-Length` is not a string of ASCII digits |
| 401 | `{"detail": "Unauthorized"}`, header `WWW-Authenticate: Bearer` | Missing or wrong token; the body never says which |
| 429 | `{"detail": "Too many failed attempts"}`, header `Retry-After: <seconds>` | The client is locked out after repeated failures (5 in 60 seconds, then a 60 second lockout); the token is not evaluated while locked out |
| 409 | `{"detail": "The snapshot is not newer than the stored one"}` | The snapshot differs from the stored one and its freshness, `min(fetched_at, received_at)`, is not after the stored freshness (an equal timestamp also conflicts) |
| 413 | `{"detail": "Payload too large"}` | Body over 1 MiB |
| 415 | `{"detail": "Expected application/json"}` | Wrong content type |
| 422 | `{"detail": "Invalid snapshot", "errors": [{"loc": ["calendars", "*", "events", 0, "start"], "type": "value_error"}]}` | Schema failure; locations keep only field names and indexes (`*` replaces an entity ID) |
| 422 | `{"detail": "Invalid snapshot", "code": "UNKNOWN_ZONE"}` | A zone is not any registry location's zone |
| 422 | same, `"code": "EMPTY_SNAPSHOT"` | No configured calendar has a single event overlapping the window |
| 422 | `{"detail": "Invalid snapshot", "errors": [{"loc": [], "type": "json_invalid"}]}` | The body is not valid JSON |
| 422 | `{"detail": "Invalid snapshot", "code": "DUPLICATE_JSON_KEY"}` | A JSON object repeats a key, at any depth |
| 422 | same, `"code": "UNKNOWN_CALENDAR"` or `"MISSING_CALENDAR"` | A calendar is not in `FL_HA_SCHOOLS`, or a configured one is missing |
| 422 | same, `"code": "DUPLICATE_PERSON"` | A person is listed under two schools (the settings already refuse this, so it only signals a bypassed check) |
| 422 | same, `"code": "FETCHED_AT_IN_FUTURE"` | `fetched_at` is more than 5 minutes ahead of the server clock |
| 422 | same, `"code": "INVALID_TIME"` | A time cannot be placed in `FL_TIMEZONE` or stored |
| 503 | `{"detail": "School snapshot ingest is not available"}` | No school configured, no audit key, or `FL_HA_SCHOOLS` names a location or person the registry lacks |
| 503 | `{"detail": "School snapshot store is busy"}`, header `Retry-After: 5` | Another writer held the database past the lock timeout; retry later |
| 500 | `{"detail": "School snapshot ingest failed"}` | A storage or internal fault other than lock contention (a read-only file, an I/O error). Nothing is stored; the response carries no `Retry-After`, so do not retry blindly: an operator must look at the database |

These are the only 422 codes: `EMPTY_SNAPSHOT`, `FETCHED_AT_IN_FUTURE`,
`UNKNOWN_ZONE`, `UNKNOWN_CALENDAR`, `MISSING_CALENDAR`, `DUPLICATE_PERSON`,
`INVALID_TIME` and `DUPLICATE_JSON_KEY`, plus schema `errors`. Any other
internal validation error is logged by class and answers the generic 500, never
a 422. The route's OpenAPI entry documents the request schema and the 200 to
503 responses.

When a snapshot has several problems, the first check that fails decides the
answer, in this order:

1. 401 or 429 (token), then 415 (content type), 400 (malformed
   `Content-Length`) and 413 (size); then the body is parsed: 422
   `DUPLICATE_JSON_KEY` for a repeated key, else 422 with `errors` (invalid JSON
   or schema). A route built without the ingest service answers the 503 below
   straight after the token check, before the content type is read.
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
nothing is stored and no audit row is written. Every accepted post, including
an `unchanged` one, writes one audit row.

### Empty snapshots

A Home Assistant calendar entity that is unavailable can return no events, and
an outage can hit every calendar at once. The app cannot tell that from a
calendar with no events, so an empty list is trusted only under the sender
contract above (do not post if any fetch failed). A snapshot in which **no**
configured calendar has an event overlapping the window (today through
`FL_COLLECT_DAYS_AHEAD` plus 2, after clipping) gives no day rows for any
school and is refused with `EMPTY_SNAPSHOT`. The stored snapshot is kept and
ages until DATA-011, rather than being replaced by an empty one. An event that
spans the window counts: a district calendar holding one all-day "Summer Break"
across the whole window is accepted and gives no-school days.

### Schools without days

A snapshot in which only some schools have events is accepted and stored. Each
configured school that got no day rows at all (every calendar it uses was
empty in the window) is listed by `location_id` in the acknowledgement's
`schools_without_days`, in configuration order; the list is empty when every
school has rows.

Such a school is not dropped silently. It has no `school_day` rows, so the
engine reports each affected day as uncertain through SCHOOL-009 ("School
baseline uncertain") at assessment time, but only on days where the child has a
transport default for school; a child with no transport default gets no warning
on those days. SCHOOL-009 is
a warning and never blocks the ingest, so a school on a summer break whose
calendars hold no events in the window still ingests. Treat a school that
appears in `schools_without_days` outside a real break as a sign that its
calendar failed to load, which the sender must prevent (the app also logs
`ha_snapshot.schools_without_days` and adds the `location_id`s to the
`ha_snapshot.ingested` event; see
[What the sender must guarantee](#what-the-sender-must-guarantee)).

## How days are classified

Each event summary is normalized (Unicode NFKC), case-folded, and split into
words at every character that is not a letter, digit or underscore. The words
are matched against whole phrases:

| Kind | Phrases |
|------|---------|
| No school | no school, no school for students, no school for all students, school closed, schools closed, district closed, no students, non student day, student free day, teacher work day, teacher workday, professional development day, staff development day, winter break, spring break, fall break, thanksgiving break, mid winter break, summer break, holiday break, school holiday, no classes, snow day, emergency closure |
| Early release | early release, early dismissal, minimum day, half day, short day, early out, shortened day |
| Late start | late start, late arrival, delayed start, delayed opening, two hour delay, 2 hour delay |
| Normal | regular day, regular schedule, regular bell schedule, normal day, normal schedule, normal bell schedule, school day, full day, regular bells |

A false "no school" is the dangerous direction: it would let a ride check
pass on a day a child is at school. A wrong "unknown" only raises SCHOOL-009.
So every ambiguous summary is unknown:

- A summary naming exactly one kind takes that kind ("No School Day" is no
  school: a normal phrase that overlaps a closure phrase is ignored).
- A summary naming two kinds is unknown, including a normal phrase beside a
  separate closure or short-day phrase ("Regular Day (no school Friday)").
- Two matched phrases that share a word without one containing the other are
  unknown: "No School Closed" reads "no school" and "school closed", and each
  hides the other's context word.
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
- A summary with **any** other word outside the matched phrases is unknown,
  unless that word is neutral (next section). This is the default, not an
  exception: a list of words that disqualify a title can never be complete
  ("Tentative", "Grades 6-8", "Drill", "Field Trip"), so the app instead lists
  the few words that may sit beside a phrase without changing its meaning.
- Anything else is unknown. Plain "holiday" is not a closure ("Holiday
  Concert" is unknown), and a plain "bell schedule" (as in "Modified Bell
  Schedule") is not a normal day. An unrecognized event is never read as a
  normal day.

### Neutral words

Only these words may remain beside a matched phrase (`NEUTRAL_WORDS` in
`adapters/home_assistant/school_days.py`). Weekdays, dates and clock times are
read separately, below.

| Group | Words |
|-------|-------|
| Filler | a, an, the, of, and, or, all, day, days |
| Holiday names | labor, columbus, indigenous, peoples, veterans, thanksgiving, christmas, new, year, years, eve, martin, luther, king, jr, dr, mlk, presidents, president, memorial, independence, juneteenth, easter, good |
| Possessive fragment | s ("New Year's" reads "new year s") |

"school", "student", "district", "break", "holiday" and the season names are
deliberately not neutral: the phrase table covers them where they name a
closure, and outside a phrase they name another program ("No School Summer
School") or would let "Regular Day Holiday" read as a regular day. A bare
number or ordinal that is not part of a date ("No School 6-8", "No School
9th") names grades as often as a day, so it is never neutral. A letter outside
ASCII stays inside its word, so a non-English or accented title is unknown
rather than losing a letter and matching by accident.

### Weekdays, dates and clock times

A weekday ("Monday", "Mon"), a month and day ("Dec 22", "22nd" beside a
month) or a numeric date ("12/22") that a summary names is checked against the
days the event itself covers, so an announcement of another day is never read
as a closure of this one:

- every named date must be one of the event's days, and every named weekday
  must be the weekday of one of them;
- together they must name the event's first and last day.

So "No School Monday" on an event that covers only that Monday keeps its kind,
while the same summary on a Thursday event, or on a Monday to Friday event,
is unknown. "Winter Break Dec 22 - Jan 2" keeps its kind only on an event that
covers Dec 22 to Jan 2.

A clock time ("12:30 PM", "noon") is accepted only beside an early release or
a late start ("Early Release 12:30 PM"). Beside a closure or a normal day it
means a partial day, and the summary is unknown.

So "No School - Labor Day", "Early Release 12:30 PM" and "Late Start Monday"
(on a Monday event) keep their kind, while "No School (Tentative)", "No School
Tomorrow", "Emergency Closure Drill", "No School 12:30 PM" and "Regular Day
Weather Permitting" are unknown.

### The tradeoff, and tuning before the shadow period

The neutral-word rule trades false closures for unknown days. Some titles a
person would call plain are now unknown, for example:

- "No School - Veterans Day (Observed)", "No School - Labor Day Holiday",
  "No School - Christmas Break", "Holiday - No School";
- "Winter Break Dec 22 through Jan 2", "Winter Break Begins", "Spring Break
  Week", and any "No School Monday" whose event does not start and end on the
  weekday or date it names;
- "No School - Conferences", "No School - Teacher In-Service", "No School K-12",
  "No School - Lunar New Year", "No School - Inclement Weather";
- "Early Release - Conferences", "Early Release at 12:30", "Minimum Day
  Schedule", "2 Hour Delay - Weather", "Regular Day Schedule".

Each one only raises SCHOOL-009 for that day; none can hide a school day.
Before the shadow period the owner runs the deployment's real calendars
through `classify_summary` and reviews **both** word tables, `CONTEXT_WORDS`
and `NEUTRAL_WORDS`, together with the phrase table: add the district's exact
phrase to the phrase table, or a word to `NEUTRAL_WORDS` only when it cannot
change which kind of day a title names. Never remove a context word to make a
title classify. Both tables are pinned by equality tests, so every change is a
reviewed diff.

Then each school's events are clipped to the window and grouped by local day
(`FL_TIMEZONE`). All-day events cover each date up to their exclusive end;
timed events cover every local day they overlap, end exclusive, across
daylight-saving changes. An event that reaches outside the window only
contributes the days inside it, so a long break costs no more than the window.
A closure sent as timed UTC midnight to UTC midnight covers two partial local
days outside UTC, so both are unknown; only an all-day event, or a timed one
from local midnight to local midnight, closes a whole day.

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
| None | no row | the engine reports SCHOOL-009 if the child has school transport that day; a school with no row on any day is also listed in `schools_without_days` |

A normal, early-release or late-start day needs a timed bell-calendar event of
its own kind, longer than zero, that starts and ends on that local day and lies
inside the window; without one it becomes unknown rather than borrowing another
kind's times. An early-release or late-start day also needs exactly one
distinct span of its kind (a stale and an updated "Early Release" give two
dismissal times), and a normal day's bell periods must not overlap; otherwise
the day is unknown. Unknown days raise SCHOOL-009 ("School baseline
uncertain"). Days outside today through `FL_COLLECT_DAYS_AHEAD` plus 2 are
dropped.

## What is stored

Only one row per person and day (person, date, school location, start, end,
kind) and the snapshot's freshness (`fetched_at`, `received_at`, a content
hash and a day count). Event text, descriptions, locations, entity IDs and
zones are never stored. Each accepted post, including an identical re-post
(`unchanged`), writes one audit row whose hashed state holds counts, freshness and the
`location_id`s of schools without days only. Every ingest replaces all school-day rows, so past days leave
the table at the next push; days that ended more than 30 days ago are deleted
by a purge that has no caller until the scheduler lands (M3; proposed, see the
[privacy spec](../privacy/privacy-and-retention-spec.md)). When retention
purges rows, the stored day count and content hash are updated with them, so a
re-post of the purged snapshot answers 409 rather than "unchanged".

## Using the snapshot in an assessment

!!! note "Revise when the scheduler lands (M3)"
    This section describes the gap that closes with M3 and M4; see the
    [roadmap](../project/roadmap.md) for the current state.

Today the snapshot is stored and nothing more: nothing in the app yet passes
it to an assessment. Each run must be given it explicitly:

```python
repo = SchoolSnapshotRepository(engine)
assessment_service.run(trigger="daily", school=repo.latest())
```

Only a run given the snapshot sees school days and the Home Assistant
freshness (and so DATA-011 for a snapshot older than 26 hours). A run given no
snapshot has no school-day data and is reported incomplete (DATA-011), never
clear.

Roadmap:

- **M3 (scheduler)**: every scheduled run passes `school=repo.latest()`.
- **M4 (on-demand API, built)**: every on-demand run passes `school=repo.latest()`; `GET /api/v1/status` reports the snapshot's fetch time and flags it stale after 26 hours.
