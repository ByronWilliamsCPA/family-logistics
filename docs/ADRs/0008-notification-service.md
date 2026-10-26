---
schema_type: planning
title: "ADR-008: Notification Service"
description: "How one assessment run becomes templated, de-duplicated pushes, with shadow routing and quiet hours."
tags:
  - architecture
  - decisions
  - adr
  - privacy
  - security
status: draft
owner: core-maintainer
component: Strategy
source: "Notification survey of 2026-10-26 (conflicts C1 to C3); independent review F1 to F14; privacy spec; runbook"
purpose: "Fix the M3 notification service design precisely enough to implement and test it."
---

> **Status**: `proposed`, revision 2. The owner delegated the design decisions below; each value that needs the
> owner's approval before `FL_NOTIFICATION_MODE=production` is marked `#ASSUME` and collected in
> [Owner-pending defaults](#owner-pending-defaults). Revision 2 answers an independent review (findings F1 to F14,
> nits N1 to N9); the finding numbers are cited where a decision changed. The front matter reads `draft` because the
> front-matter validator accepts only `draft`, `in-review` and `published` (see ADR 0006).

**Decision Date**: 2026-10-26
**Deciders**: Byron Williams (owner, delegated); Riley (co-approver for family-facing behaviour, ADR 0001)
**Affected Components**: services/notifications, services/assessments (`RunOutcome`), domain (new
`notification_policy`, `notification_subjects`, `notification_text`), db (new `notifications`, migration `0006`,
retention), core/database (shared `BEGIN IMMEDIATE` helper). No change to `Finding`, any rule module or
`RULES_VERSION`.
**Implementation Target**: M3, five PRs after this ADR (see [Implementation](#implementation))

## Context

### Problem Statement

The engine (ADR 0001) decides findings and the run status; the Apprise client sends one message to one dedicated
`fl-*` tag and never retries. Nothing in between decides which findings become pushes, what they say, who gets them,
when they may wake someone, and how a push is sent exactly once. `services/notifications.py` is a stub whose TODO names
the concerns: de-duplication "by issue ID and revision", quiet hours with an imminent-critical bypass, shadow and
production routing, and "could not verify" pushes when a failure blocks validation of an imminent ride.

Pushes reach phones. They are the only channel that can wake a parent before a child is stranded, and the only channel
where a careless message leaks another family's child's name into a notification history. Both failure directions
matter: a missed or delayed urgent push, and a push that says too much or wakes people for nothing.

### Current State (verified against `main` at 81c4e36)

- **C1, shadow routing.** The runbook ("Notification modes") says shadow mode sends *all* output to the operator-only
  `fl-shadow` tag. `adapters/apprise/__init__.py` and the `notification_mode` docstring in `core/config.py` say that in
  shadow mode "only the Sunday report goes to Morgan". The two cannot both hold.
- **C2, push content.** ADR 0001 says notifications carry "engine counts and statuses only". Privacy spec section 4
  allows, in pushes, a family child's first name, an external child's initial, ride times, the driver and venue
  names, and section 5 gives "Ride for E. at 3:15 needs a driver" as the model.
- **C3, imminence.** `AssessmentContext.is_imminent` (`domain/context.py`) returns `start - now <= imminent_window`
  with no lower bound, so a ride that has already started counts as imminent. Its only caller is DEST-004's severity
  (`domain/feasibility.py`, line 63), over `upcoming_trips`, which keeps only rides whose `end >= now`. Together they
  mean "imminent until the ride ends", which is the right behaviour for a child who may be waiting. The docstring
  ("starts between now and the window's end") and the configuration guide ("rides starting within this window") are
  what is wrong. This ADR does not change engine code.
- **Free text in findings.** `Finding.message` embeds names as written in calendar notes (`parser.py` formats them
  with `!r`) and `Finding.suggestion` can list registered names. `Finding.entity_keys` is no safer: it holds
  `SourceEventKey.as_str()` (calendar id, UID and RECURRENCE-ID), normalised raw names from notes (`family-name:`,
  `external-name:`, `external-ambiguous:`, `external-unknown:`) and location keys. None of the three may reach a push.
- **Structured subjects already exist.** Every rule's entity keys are structured and resolvable against data the run
  already holds (table in D7): trip keys, `passenger:<id>`, `driver:<id>`, and the transition keys `person:`, `day:`,
  `from:`, `to:`. The service can learn "when" and "who" without reading a message and without changing `Finding`.
- **Severity is per emission.** DEST-004, PASS-003, CAP-014 and DRIVER-006 can emit a severity other than the catalog
  default, so routing uses `Finding.severity`, never `RULES[rule_id].severity`. DEST-004 and DRIVER-006 are emitted
  once per passenger.
- **`RunOutcome`** holds `run_id` and the result only. A failed run keeps the previous findings open as `ongoing`
  (`failed_result`, with no timelines) and writes no finding state. `begin_run` and `complete_run` raise out of
  `AssessmentService.run`, in which case no `RunOutcome` exists at all.
- **Transactions.** `engine.begin()` issues a deferred `BEGIN`. `BEGIN IMMEDIATE` exists only in three private helpers
  (`db/route_relay.py`, `db/school_snapshots.py`, `db/migrations_api.py`); `core/database.is_lock_contention` exists.
- **Apprise client.** `AppriseClient.notify(tag, title, body) -> bool` truncates the title to 120 and the body to 2000
  characters, returns True only for HTTP 200, never retries, and raises `NotificationTagError` for any tag outside the
  six configured ones.
- **Audit.** `AuditRepository.record` raises `ConfigurationError` without `FL_AUDIT_KEY`; production `serve` refuses to
  start without the key (`cli.py`).

### Requirements

- **Must have**: a failed or incomplete run is never reported as clear on any tag, including by a late or out-of-order
  dispatch of an older run (design rule 4).
- **Must have**: shadow mode is the default and nothing reaches the other parent in it (design rule 5).
- **Must have**: only the six dedicated tags; recipients live in Apprise, never in the app.
- **Must have**: no free text from calendars, no addresses, notes, UIDs, calendar ids or surnames in any push or log.
- **Must have**: a push that was not delivered, or was held for quiet hours, is sent by a later dispatch; a push that
  was delivered is not sent again for the same episode, even if `dispatch` runs twice or concurrently.
- **Must have**: notification metadata can never make an assessment run fail.
- **Should have**: a critical problem on an imminent or in-progress ride reaches the parents at any hour, once.
- **Should have**: bounded volume and bounded night wakes.

## Decision

> **We will add a notification service that turns one stored assessment outcome into templated pushes. A pure policy
> decides them, a pure resolver derives each finding's subject (time, people, driver) from its entity keys and a ride
> index the outcome carries, and a per-subject state table with a global watermark (migration `0006`) makes every
> dispatch idempotent and order-safe. No push is ever rendered from a finding's message, suggestion or raw keys.**

### D1. Shadow routing (resolves C1)

In shadow mode **every** push, including ops alerts, status and the weekly report, is delivered to
`FL_APPRISE_TAG_SHADOW` (`fl-shadow`) only. The service still evaluates everything as production would (kind, intended
tag, quiet hours, de-duplication), so shadow is a faithful preview: the title starts with `[shadow]` and a space, and
the first body line states the tag production would have used, for example
`SHADOW: production would send this to fl-urgent (bypasses quiet hours).`

*Why*: the runbook rule is the conservative one and the one both parents agreed to; "only the Sunday report goes to
Morgan" would leave the operator blind to the very pushes the shadow period exists to evaluate. Routing is decided in
one function (`delivery_tag`), so "nothing reaches the other parent" depends only on how Apprise routes `fl-shadow`.

### D2. Push content (resolves C2; F8)

Pushes are rendered from templates. There is one template per rule ID in `domain/rules.RULES` (a test pins the two key
sets equal) plus fixed templates for the run-failed, status, report and ops pushes. The only inputs are:

| Input | Source | Rendered as |
|-------|--------|-------------|
| Rule, severity | `Finding.rule_id`, `Finding.severity` | Template; `[CRITICAL]`, `[WARNING]`, `[COULD NOT VERIFY]` |
| Lifecycle | `TrackedFinding.lifecycle`, state row | `new`, `back again`, `still open`, `now critical` |
| When | Resolved subject (D7), `FL_TIMEZONE` | `Fri 30 Oct 15:15`, or `Fri 30 Oct` for a day |
| Family child or parent | Resolved subject, kind `family` | First name (below) |
| External child | Resolved subject, kind `external` | Initial only: `E.` |
| Driver | Resolved subject `driver_id` | First name (below); a contact such as Driver S included |
| Source (DATA-011) | Closed map of entity keys (below) | `calendar`, `school schedule`, `any data source` |
| Reference | `Finding.issue_id`, run id | `ref 1a2b3c4d5e6f7a8b` (internal, class I) |
| Counts, run status | `AssessmentResult`, closed label maps | Numbers and fixed labels |

The DATA-011 map matches the exact entity keys `source:icloud`, `source:ha_snapshot` and `source:none`; any other key
renders `a data source`.

Masking rules (`NameDirectory`, F8). Names are looked up by **(kind, id)**, never by id alone: `person_id` and
`passenger_id` are separate primary keys with no cross-table uniqueness, so one namespace could render a person's name
for an external child with the same id.

- **Family** (`PassengerKind.FAMILY`, and coverage subjects, which are always monitored children): the first token
  of `Person.display_name`, from `person` rows with role `child` or `parent` only. `display_name` is the canonical name
  and may include a surname; only the first whitespace-separated token is used, with every character outside letters,
  apostrophe and hyphen removed, cut to 20 characters. A test renders a two-word display name and asserts the second
  word is absent.
- **External** (`PassengerKind.EXTERNAL`): always the first letter of `ExternalPassenger.display_first_name`,
  upper-cased, plus `.`, whatever else is looked up; no letter gives `a passenger`. Two external children with the same
  initial both render `E.`; this ambiguity is accepted (the status card has the detail). The initial is the first
  letter of `display_first_name`, so a pseudonymised external such as the household's `Passenger E` renders `P.`, while a
  deployment with real first names gets the real initial.
- **Driver** (`driver_id`): the first token of `Person.display_name` from the `person` table only (any role), filtered
  as above; unknown gives `the driver`.
- An id not found under its kind, any id starting with `unregistered:`, or a directory that could not be loaded renders
  as `a passenger` or `the driver`. A rendering failure never blocks a push; it degrades to these words.
- `NameDirectory` is built once per dispatch from a narrow query over the `person` and `external_passenger` tables
  (N7), not from `load_snapshot`, and needs no cipher.
- Never rendered: `Finding.message`, `Finding.suggestion`, `Finding.entity_keys` (except the closed DATA-011 map),
  `Trip.pickup`, `Destination.raw`, `Trip.free_notes`, any location name, address or coordinate, calendar ids, UIDs,
  `AssessmentResult.error`.
- **No location names in v1.** The privacy spec allows venue names, but a location's `name` can be a home ("Household
  E home") and the app cannot tell a venue from a home reliably. The push says what is wrong and when; the status card
  has the detail.

A fallback template exists for a rule ID that has no template at runtime (impossible while the key-set test passes):
`[<SEVERITY>] A ride check needs attention<, at {when}>. Rule <rule_id>, ref <issue_id>.` with `<rule_id>` printed
only if it is a key of `RULES`.

*Why*: ADR 0001's "counts and statuses only" predates the privacy spec, which is the binding data rule set and was
written with pushes in mind. Counts alone cannot say *which* ride needs a driver. Templates make the full set of
possible output enumerable, which makes the "no free text" test exhaustive.

### D3. Kinds, severity and tags (F9, F13, N1, N3, N4)

Every push has a **kind**; the kind alone decides the production tag. v1 has five kinds (reminders are deferred).

| Kind | Production tag | Produced by |
|------|----------------|-------------|
| `urgent` | `fl-urgent` | Imminent critical or incomplete finding; failed run, rides imminent or unknown |
| `notice` | `fl-warning` | Any other open finding; a failed run with no imminent ride |
| `status` | `fl-status` | A completed `daily` or `on_demand` run (clear, attention required or incomplete) |
| `report` | `fl-report` | A completed `weekly` run (clear, attention required or incomplete) |
| `ops` | `fl-ops` | One per failed episode; one per incomplete episode |

Per-finding decision table (completed runs; a failed run is D4):

| Rule and `Finding.severity` | Imminent (D7) | Kind | Production tag |
|-----------------------------|---------------|------|----------------|
| CAP-014, any severity | either | `notice` | `fl-warning` |
| critical (not CAP-014) | yes | `urgent` | `fl-urgent` |
| critical (not CAP-014) | no | `notice` | `fl-warning` |
| incomplete (not CAP-014) | yes | `urgent`, worded "could not verify" | `fl-urgent` |
| incomplete (not CAP-014) | no | `notice`, worded "could not verify" | `fl-warning` |
| warning | either | `notice` | `fl-warning` |

`#ASSUME` (owner-pending): CAP-014 is never urgent. Unknown capacity is a standing registry gap raised per ride, and a
known over-capacity ride is visible to the driver at pickup; neither justifies waking anyone. An incomplete finding on
an imminent ride stays urgent, because an unverifiable imminent ride is exactly the case the bypass exists for.

Run-level decision table:

| Run status | Trigger | Run-level pushes |
|------------|---------|------------------|
| `failed` | any | `run-failed` (`urgent` if a ride is imminent or unknown, else `notice`); `ops:run_failed` |
| `incomplete` | `daily`, `on_demand` | `status`, labelled "could not fully verify"; `ops:run_incomplete` |
| `incomplete` | `weekly` | `report`, labelled "could not fully verify"; `ops:run_incomplete` |
| `incomplete` | `incremental` | `ops:run_incomplete` only |
| `attention_required` | `daily`, `on_demand` | `status` with counts |
| `attention_required` | `weekly` | `report` with counts |
| `clear` | `daily`, `on_demand` | `status`: "No open findings; sources fresh; driver acceptance not verified." |
| `clear` | `weekly` | `report`, same wording |
| `attention_required`, `clear` | `incremental` | None |

Rules that hold across both tables:

- An incomplete `status` or `report` (F13) says "Ride check: could not fully verify" and lists the incomplete reasons
  as counts per rule from a closed label map (for example `data sources: 1`, `travel times or places: 3`,
  `vehicle capacities: 2`), plus the open critical and warning counts. It never uses clear wording. The shadow period
  is mostly incomplete runs (DATA-011 until Home Assistant posts, first-cycle ROUTE-010, CAP-014), and the M3 gate
  needs these reports.
- Clear wording (N3, N4) lives in one constant, `CLEAR_PHRASES`, used only by the clear template. A test asserts that
  no other template contains any word of it and that no non-clear push matches
  `\b(all[ -]clear|clear|no (open )?(issues|findings|problems)|all good|all rides (are )?verified)\b`
  (case-insensitive; "unclear" passes by the word boundary). The clear text does not claim every ride was routed or
  accepted: rides without deadlines are not routed and driver acceptance is never verified.
- The run status label comes from a closed map: `clear`, `attention required`, `could not fully verify`,
  `check failed`.
- A failed run never produces `status`, `report` or per-finding pushes other than the retries in D4.
- `ops:run_incomplete` reconciles with the roadmap ("`ops_alert()` wired to failed or incomplete runs"): one ops push
  per incomplete **episode** (opened by the first incomplete completed run, closed by the next completed run that is
  not incomplete), not one per run. In shadow mode it goes to `fl-shadow` like everything else, so the operator sees
  it once per episode next to the "could not fully verify" status.
- `fl-report` carries the weekly run's summary only. A full report document and any digest are out of scope.

### D4. Episodes and which lifecycle transitions notify (F4, F5, F6, F9, F10)

Notification is **owed per episode** of a subject, not per run, so it survives quiet hours and failed deliveries by
construction. An issue's episode starts when its finding is `new` or `regressed` and ends when it is resolved.

- **new, regressed**: owed. If the subject's state row is open but its `episode_run_id` differs from this outcome's
  run, the row is closed and reopened (F4): a regression after a missed or failed dispatch is a new episode, never a
  swallowed duplicate. A re-dispatch of the same run keeps the same episode.
- **ongoing**: not owed if this episode was delivered at least as severely and urgently as now. Owed only if it was
  never delivered (held, failed, or open before the service existed), or on **escalation**: the severity rose above the
  severity last delivered (`domain/lifecycle._RANK`), or the kind rose from `notice` to `urgent`. A downgrade sends
  nothing, so an urgent push is never followed by a notice for the same episode (F5).
- **One bypass per episode** (F9): at most one push per episode is sent inside quiet hours. A later escalation in the
  same episode during quiet hours is held until the window ends.
- **No reminders in v1** (F9). A delivered urgent push is not repeated while the issue stays open.
- **Run-wide urgent** (DATA-011 issues and the `run-failed` episode, which have no single ride): re-owed at most once
  per local calendar day while the episode is open and a ride is imminent or the ride set is unknown (F10). The daily
  re-arm does not grant a second bypass in the same episode.
- **resolved**: sends nothing in v1. It closes the issue's row, which clears de-duplication so a recurrence notifies
  again, subject to the notice cooldown (D5).
- **Failed run** (F6): no new per-finding episodes, nothing is closed. The `run-failed` and `ops:run_failed` subjects
  are planned, and every **open, undelivered** issue row is retried with the kind stored on the row (an urgent push
  that failed at 14:00 for a 15:30 ride is retried by a 15:00 failed run). Retries render from the carried finding with
  subjects resolved against the history rides; anything unresolved renders generically. The first completed run
  closes `run-failed` and `ops:run_failed`.
- **Run that never produced an outcome** (F6): `begin_run` or `complete_run` raised, or the scheduler could not start
  the run. The scheduler calls `NotificationService.dispatch_unavailable(trigger)`, which plans the `run-failed`
  episode with an unknown ride set (so `urgent`, once per day) and `ops:run_failed`. The family is told "could not
  verify" rather than nothing.

*Why*: "new and regressed notify" evaluated per run would lose a push held at 23:00, because the next run sees the
finding as `ongoing`. Owing per episode keeps the intent and makes the held push go out after quiet hours.

### D5. De-duplication key, escalation and cooldown (F5)

- **Key**: `(subject_key, mode)`. One row per subject per mode; the row stores the kind and severity last delivered.
  `subject_key` is one of:
  - `issue:<issue_id>` for a finding (`issue_id` is `sha256(rule_id + sorted entity keys)[:16]`);
  - `run-failed` for the failed-run episode;
  - `run:<run_id>` for a run's `status` or `report`;
  - `ops:run_failed` and `ops:run_incomplete`.
- **Escalation rule**: an open, delivered row is owed again only when the planned severity ranks above the delivered
  severity, or the planned kind is `urgent` and the delivered kind is `notice`. Nothing else re-notifies within an
  episode.
- **"Revision" is dropped.** The TODO's "issue ID and revision" assumed a content revision. Confirmation revisions are
  not stored (ADR 0002); a material change already changes the issue (a moved occurrence has a new key) or its
  severity.
- **Mode in the key.** Switching to production starts a fresh key space, so each open issue is pushed once to both
  parents on the first production dispatch. Handling that burst is deferred past the gate (it cannot happen in shadow).
  `#ASSUME` (owner-pending).
- **Notice cooldown**: `RENOTIFY_COOLDOWN = 4 hours`. A `notice` owed because a new episode started is held while the
  closed row's `last_sent_at` is younger than the cooldown, so a flapping issue does not re-notify every scan. `urgent`
  has no cooldown in v1; an urgent cooldown for flapping incomplete findings is a post-gate item. `#ASSUME`
  (owner-pending).
- **Constants, not settings.** The cooldown, claim lifetime, send deadline, retry delays and chunk size are module
  constants in `domain/notification_policy.py`; no new `FL_` setting is added.

### D6. Quiet hours (F9, N8)

- Window: `FL_QUIET_HOURS_START` to `FL_QUIET_HOURS_END` (defaults 22:00 and 06:00) in `FL_TIMEZONE`. An instant `now`
  is inside when its local wall time `t = now.astimezone(zone).time()` satisfies `start <= t < end` for a same-day
  window, or `t >= start or t < end` when `start > end`. `start == end` is refused by settings; the implementation PR
  also refuses a time-zone-aware `time` (N8), which would raise `TypeError` when compared.
- Only `urgent` bypasses, and at most once per episode (D4). `notice`, `status`, `report` and `ops` are held.
  `#ASSUME` (owner-pending): ops alerts are held because the operator can act on them only in the morning unless a
  ride is imminent, and that case already produces an `urgent` push.
- **Held means not claimed.** A held issue, `run-failed` or `ops` push has no delivered row, so the first dispatch
  after the window sends it if it is still owed. Holding writes nothing, with one exception. A held `notice` that
  starts a new episode on an **open** row of another run (a `new` or `regressed` finding after a missed, failed or
  stale resolve dispatch, F4) records the reopen: the row becomes undelivered with the new `episode_run_id`,
  `send_count = 0`, `bypass_used = 0`, no severity and no claim. Without it the next scan sees the finding as
  `ongoing` and the old row as delivered, so the push would read as a `DUPLICATE` and be lost. A **closed** row (held
  by the cooldown) and a **missing** row write nothing: the policy re-derives both from the persisted row, and
  reopening a closed row would bypass the cooldown. The same run re-dispatched inside the window writes nothing more.
  `ops`, `run-failed` and `urgent` pushes never reach this case.
- A held `status` or `report` is dropped, not deferred: a later run supersedes it. It is logged as
  `notification.suppressed` with `reason=quiet_hours_dropped`. A `FL_DAILY_CRON` or `FL_WEEKLY_CRON` inside quiet
  hours therefore loses that push; the scheduler PR adds a startup warning for it.
- **DST.** The policy never builds a local datetime from a boundary; it converts each real instant to local time, and
  every instant has exactly one local wall time. A boundary that does not exist on a spring-forward night (02:30 in
  `America/Los_Angeles`) takes effect at the first instant whose local time is at or after it (03:00); a boundary in
  a repeated hour applies to both passes. The night's window is then one hour shorter or longer. Imminence, cooldowns
  and the daily re-arm use aware UTC instants, except that "local calendar day" uses `FL_TIMEZONE`.

### D7. Subjects and imminence (F1, F3, F11)

**Imminence (F1).** A subject is imminent when

```text
due_at - imminent_window <= now <= due_end
```

with `imminent_window = FL_IMMINENT_WINDOW_MINUTES` (180) and `now` the dispatch clock. A ride that has started stays
imminent until it ends, so a stranded-child problem found during a pickup window is `urgent` and is not held by quiet
hours. This matches the engine (C3). A window of 0 is accepted and means "imminent only while in progress"; it cannot
loop because run-wide re-arms are daily (D4).

**Subjects are derived, not stored (F3).** `Finding`, `make_finding`, the rule modules and `RULES_VERSION` are not
changed. A pure resolver, `domain/notification_subjects.py`, maps each finding to a `Subject` from its entity keys, a
`RideIndex` that `RunOutcome` carries, and `result.timelines`. It runs outside `assess`, so it can never fail a run.

```python
# domain/notification_subjects.py (pure)
@dataclass(frozen=True, slots=True)
class RideWindow:
    start: datetime                 # aware UTC
    end: datetime
    driver_id: str | None
    passengers: tuple[tuple[str, PassengerKind], ...]  # unregistered:* dropped

@dataclass(frozen=True, slots=True)
class RideIndex:
    current: Mapping[str, RideWindow]   # SourceEventKey.as_str() -> this run's parsed trips
    unparsed: Mapping[str, RideWindow]  # transport events that did not parse (start, end only)
    previous: Mapping[str, RideWindow]  # history.trips (the last completed run's rides)

    @classmethod
    def build(
        cls, *, current: Sequence[Trip], previous: Sequence[Trip],
        unparsed: Sequence[tuple[SourceEventKey, datetime, datetime]],
    ) -> RideIndex: ...
    def windows(self) -> tuple[RideWindow, ...]: ...  # current and unparsed; previous only if both are empty

@dataclass(frozen=True, slots=True)
class Subject:
    due_at: datetime | None = None   # None: run-wide or day-level
    due_end: datetime | None = None
    day: date | None = None
    passengers: tuple[tuple[str, PassengerKind], ...] = ()
    driver_id: str | None = None

class Resolution(StrEnum):
    RESOLVED = "resolved"; RUN_WIDE = "run_wide"; DAY = "day"; UNRESOLVED = "unresolved"

def resolve_subject(
    finding: Finding, *, rides: RideIndex | None, timelines: Sequence[DayTimeline],
    pickup_tolerance: timedelta,
) -> tuple[Resolution, Subject]: ...
def transition_window(tr: Transition, pickup_tolerance: timedelta) -> tuple[datetime, datetime]: ...
def is_imminent(due_at: datetime, due_end: datetime, now: datetime, window: timedelta) -> bool: ...
def run_imminent(
    rides: RideIndex | None, timelines: Sequence[DayTimeline], now: datetime,
    window: timedelta, pickup_tolerance: timedelta,
) -> bool: ...  # rides is None -> True
```

Entity-key formats as emitted today, and how each resolves. A trip key is `SourceEventKey.as_str()`
(`calendar_id|uid|recurrence_id`, `-` for none). Resolution matches a key exactly against the ride index first, so a
calendar id that happens to start with `person:` cannot be mistaken for a prefixed key; prefixed keys are parsed next.

| Rule (module) | Entity keys | `due_at` .. `due_end` | People |
|---------------|-------------|-----------------------|--------|
| PARSE-001 (`parser.py`) | trip key, `parse:<detail>` or `parse:duplicate:<id>` | event start .. end | none |
| NAME-002 (`parser.py`) | trip key, `family-name:<name>` | event start .. end | none |
| EXT-013 (`parser.py`) | trip key, `external-name:`, `-ambiguous:`, `-unknown:`, `-id:` | event start .. end | none |
| PASS-003 (`change_impact.py`) | trip key, `passenger:<id>` | previous ride start .. end | passenger, driver |
| CONF-008 (`confirmation.py`) | trip key | current ride | none |
| CONF-008 (`change_impact.py`) | previous trip key | current ride, else previous | none |
| DEST-004 (`feasibility.py`) | trip key, `passenger:<id>`, `destination` | current ride | that passenger |
| TIME-007, ORDER-012 (`feasibility.py`) | trip key, `route` | current ride | passengers, driver |
| ROUTE-010 ride (`feasibility.py`) | trip key, `route` | current ride | passengers, driver |
| CAP-014 (`capacity.py`) | trip key, `capacity` | current ride | driver |
| DRIVER-006 ride (`driver.py`) | trip key, `passenger:<id>`, `driver:<id>` | current ride | passenger, driver |
| ROUTE-010 reach (`driver.py`) | trip key, `driver:<id>`, `reachability` | current ride | driver |
| RIDE-005 (`coverage.py`) | `person:`, `day:`, `from:`, `to:` | transition window | person |
| MODE-015 (`coverage.py`) | transition keys, then `bus` or `default-driver` | transition window | person |
| ROUTE-010 place (`coverage.py`) | transition keys, `unresolved` | transition window | person |
| DRIVER-006 default (`coverage.py`) | transition keys, `driver:<id>` | transition window | person, driver |
| SCHOOL-009 (`coverage.py`) | `person:<id>`, `day:<date>`, `school` | day from the key | person |
| DATA-011 (`engine.py`) | `source:<name>` or `source:none` | run-wide | none |

- The parser keys resolve against `current` (a parsed event, which is a trip) or `unparsed` (an event that did not
  parse, carried with its start and end). Raw names in `family-name:` and `external-*:` keys are never read.
- A transition is found in `result.timelines` by `person_id`, `day`, `from_key` and `to_key`. Its window is
  `transition_window`: `due_at = reference_time`; `due_end = arrive_by` when set, else
  `depart_after + pickup_tolerance` (the engine's `EngineSettings.pickup_tolerance`, 30 minutes by default).
  Coverage subjects are monitored children, so their person renders as `family`.
- PASS-003 and CONF-008 from change impact concern the previous leg; `previous` supplies its window, passengers and
  driver with their kinds.
- `passenger:<id>` takes its kind from the matching ride's passenger list; an id not on the ride renders generically.
- **Run-wide imminence (F11)**: true when any ride window from `RideIndex.windows()` **or any transition window** in
  `result.timelines` is imminent, so a stale school snapshot (DATA-011 `source:ha_snapshot`) counts bus and school
  moves, not only rides. An unknown ride set (`rides is None`) counts as imminent.
- **A missing subject fails loud (F11)**: a finding other than DATA-011 that resolves `UNRESOLVED` in a completed run
  counts as imminent and logs `notification.subject_unresolved` at error level with `rule_id` and `issue_id`. A test
  over the acceptance matrix requires zero unresolved findings, so a change to the entity-key grammar fails CI before
  it fails silently. (The grammar is already load-bearing: it defines `issue_id`.)
- `RunOutcome` gains two fields, set by `AssessmentService.run`; `_parse_rides` additionally returns each unparsed
  event's key, start and end:

```python
@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    result: AssessmentResult
    trigger: str = "on_demand"
    rides: RideIndex | None = None
    """Completed run: current trips, unparsed transport events and history.trips.
    Failed run: history.trips only when the history was read, else None.
    None means unknown, and unknown counts as imminent."""
```

`#ASSUME` (owner-pending): an unknown ride set counts as imminent, so a failed run with no readable history sends an
urgent "could not verify", at most once per local day (D4).

### D8. Delivery failure and retries (F7)

- `notify` returning False means not delivered. For an `urgent` push the service retries once within the dispatch
  after `URGENT_RETRY_DELAY` (5 seconds). If it still fails, the claim is released, `failed_attempts` is incremented and
  nothing is marked sent, so the next dispatch retries; `DispatchReport.urgent_failed` lets the scheduler run an extra
  dispatch about 5 minutes later. Other kinds are not retried within a dispatch.
- The claim, `complete` and `release` transactions each retry up to `CLAIM_LOCK_RETRIES` (3) times with a short
  backoff when `core.database.is_lock_contention` reports a busy database (for example while `complete_run` holds the
  write lock). A busy error only comes from `BEGIN IMMEDIATE`, before anything is written, so repeating is safe, and
  a delivered send is not re-sent after a lock held by another writer. After the last retry the claim raises
  `DatabaseError` to the scheduler with nothing sent; a `complete` that still fails leaves the claim to expire
  (at-least-once, D11).
- `dispatch` never raises for a delivery failure. It raises only `NotificationTagError` (a programming error) and
  `DatabaseError` (state not writable after retries). The run is already stored, so its status is unaffected.
- `FL_APPRISE_URL` unset (allowed in shadow mode): `dispatch` plans, logs `notification.not_configured`, writes nothing
  and returns `unconfigured=True`; everything stays owed.
- A dispatch stops starting new sends after `SEND_DEADLINE` (10 minutes), below `CLAIM_TTL`, so its own claims never
  expire under it (N6). Unsent claims are released.
- **Deferred past the gate** (N5): a per-tag failure counter and a "delivery failing" ops alert. In shadow mode every
  push goes to `fl-shadow`, the ops channel itself, so such an alert could never fire during the shadow period. Until
  then the external monitor of `/healthz` and the error logs are the backstop.

### D9. State tables (migration `0006_notification_state`; F2, F4, F12, N2, N9)

Revision `0006_notification_state`, `down_revision = "0005_route_relay"`. Neither table holds personal data: subject
keys are hashes, run ids and fixed names; tags are the configured `fl-*` values.

```sql
CREATE TABLE notification_state (
    subject_key      VARCHAR(48) NOT NULL,
    mode             VARCHAR(16) NOT NULL,
    kind             VARCHAR(16) NOT NULL,   -- kind last delivered, or planned while undelivered
    tag              VARCHAR(64) NOT NULL,   -- production tag of that kind
    rule_id          VARCHAR(16),            -- NULL for run, run-failed and ops subjects
    severity         VARCHAR(16),            -- severity last delivered; NULL until delivered
    is_open          BOOLEAN NOT NULL,
    delivered        BOOLEAN NOT NULL DEFAULT 0,
    bypass_used      BOOLEAN NOT NULL DEFAULT 0,   -- a quiet-hours push was sent this episode
    episode_run_id   VARCHAR(36) NOT NULL,   -- run that opened the episode
    send_count       INTEGER NOT NULL DEFAULT 0,
    failed_attempts  INTEGER NOT NULL DEFAULT 0,
    first_sent_at    DATETIME,
    last_sent_at     DATETIME,
    last_attempt_at  DATETIME,
    claim_token      VARCHAR(36),
    claimed_at       DATETIME,
    run_id           VARCHAR(36) NOT NULL,   -- last run that touched the row; no FK (runs are purged)
    evaluated_at     DATETIME NOT NULL,      -- evaluated_at of that run
    created_at       DATETIME NOT NULL,
    updated_at       DATETIME NOT NULL,
    PRIMARY KEY (subject_key, mode),
    CONSTRAINT ck_notification_state_kind
        CHECK (kind IN ('notice', 'urgent', 'status', 'report', 'ops')),
    CONSTRAINT ck_notification_state_mode CHECK (mode IN ('shadow', 'production')),
    CONSTRAINT ck_notification_state_tag CHECK (tag GLOB 'fl-[a-z0-9]*'),
    CONSTRAINT ck_notification_state_severity
        CHECK (severity IS NULL OR severity IN ('critical', 'warning', 'incomplete')),
    CONSTRAINT ck_notification_state_counts CHECK (send_count >= 0 AND failed_attempts >= 0),
    CONSTRAINT ck_notification_state_claim CHECK ((claim_token IS NULL) = (claimed_at IS NULL)),
    CONSTRAINT ck_notification_state_delivered
        CHECK (delivered = 0 OR (first_sent_at IS NOT NULL AND last_sent_at IS NOT NULL)),
    CONSTRAINT ck_notification_state_subject CHECK (
        (length(subject_key) = 22 AND subject_key GLOB 'issue:[0-9a-f]*'
            AND substr(subject_key, 7) NOT GLOB '*[^0-9a-f]*' AND kind IN ('notice', 'urgent'))
        OR (subject_key = 'run-failed' AND kind IN ('notice', 'urgent'))
        OR (subject_key GLOB 'run:?*' AND length(subject_key) <= 40 AND kind IN ('status', 'report'))
        OR (subject_key IN ('ops:run_failed', 'ops:run_incomplete') AND kind = 'ops'))
);
CREATE INDEX ix_notification_state_open ON notification_state (is_open);
CREATE INDEX ix_notification_state_updated_at ON notification_state (updated_at);

CREATE TABLE notification_watermark (
    id            INTEGER PRIMARY KEY,
    evaluated_at  DATETIME NOT NULL,   -- newest outcome evaluated_at dispatched so far
    run_id        VARCHAR(36) NOT NULL,
    updated_at    DATETIME NOT NULL,
    CONSTRAINT ck_notification_watermark_singleton CHECK (id = 1)
);
```

The DDL is written with `op.create_table` and mirrored in `db/schema.py` with the same constraint names. Timestamp
columns use `UtcDateTime`. Downgrade drops both tables.

**Write transactions (F12).** `engine.begin()` issues a deferred `BEGIN`, and in WAL mode a read that later upgrades to
a write fails with `SQLITE_BUSY_SNAPSHOT` without waiting. The three existing private `BEGIN IMMEDIATE` helpers are
extracted into one shared `core.database.immediate_transaction(engine)` context manager, and the claim, complete and
close steps and the watermark all use it. The existing callers move to it in the same PR without behaviour change.

Row rules:

- **Watermark (F2)**: inside the claim transaction, the outcome's `result.evaluated_at` (or `now` for
  `dispatch_unavailable`) is compared with the single watermark row. Older: every push and every close of this dispatch
  is `STALE` and nothing is written. Equal with the same `run_id`: a re-dispatch, allowed. Equal with a different
  `run_id`: `STALE`. Newer: allowed, and the watermark advances in the same transaction. So an older run can never
  send "Clear" after a newer failed run, nor push an issue a newer run already resolved.
- **Status freshness (F2)**: a `status` or `report` is also `STALE` when `now - result.evaluated_at` exceeds one scan
  interval (`FL_CHANGE_SCAN_MINUTES`), so a dispatch that runs late after a restart cannot send an old clear.
- **Claim** (one immediate transaction): read the watermark and the rows of every planned subject, apply `decide`,
  and for `SEND` upsert the row with `claim_token`, `claimed_at = now`, `kind`, `tag`, `rule_id`, `run_id`,
  `evaluated_at`, and `is_open = 1` (`0` for `run:*`). A new episode (D4) resets `delivered`, `severity`,
  `bypass_used`, `send_count` and sets `episode_run_id`. A claim older than `CLAIM_TTL` (15 minutes) is abandoned.
- **Complete** (one immediate transaction per push or chunk): delivered sets `delivered = 1`, `kind`, `severity`,
  `last_sent_at = last_attempt_at = now`, `first_sent_at` if NULL, `send_count + 1`, `failed_attempts = 0`, and
  `bypass_used = 1` if sent inside quiet hours; not delivered sets `failed_attempts + 1` and `last_attempt_at`. Both
  clear the claim, only where `claim_token` still equals this dispatch's token.
- **Close** (in the claim transaction of a completed, non-stale run): every open `issue:*` row whose issue is not open
  in this result, plus `run-failed` and `ops:run_failed`, plus `ops:run_incomplete` unless the run is incomplete, gets
  `is_open = 0`, `delivered = 0`, `severity = NULL`, `bypass_used = 0`, `run_id` and `evaluated_at` of this run (N2);
  `last_sent_at` is kept for the notice cooldown.
- **Retention (N9)**: `purge_expired_notification_state(connection, *, now) -> int` in `db/retention.py` deletes
  rows with `is_open = 0` whose `updated_at` is strictly more than `NOTIFICATION_STATE_RETENTION_DAYS` (90) days
  before `now`. Open rows are never purged, including open `ops:*` rows (purging one would re-notify it; an
  `ops:run_incomplete` row stays open while the run stays incomplete). Nothing else is touched, not the watermark and
  not the audit log. It is idempotent and refuses a naive `now`.
  `#ASSUME` (owner-pending). The scheduler's daily job calls it with the other purges.

### D10. Audit and chunking

- One audit row per **delivered** push when `FL_AUDIT_KEY` is configured:
  `record(actor="notifier", object_ref=subject_key, action="notification_sent", new_state={"kind", "tag",
  "delivery_tag", "mode", "rule_id", "severity", "run_id", "chunk_size"}, correlation_id=run_id)`. `object_ref` is a
  subject key, never personal data; the state is only hashed.
- No key: skipped, with one `notification.audit_skipped` debug line per dispatch. Production mode already requires the
  key (`cli.py`), so production pushes are always audited.
- The audit write runs in its own transaction **after** the delivered row is committed. An audit failure is logged as
  `notification.audit_failed` (error class only) and never undoes or repeats the send.
- Chunking: per dispatch, owed `notice` pushes are grouped into chunks of at most `MAX_ITEMS_PER_PUSH` (8) items, most
  severe and soonest first, one Apprise call per chunk; each item is claimed and completed individually, and a chunk's
  result applies to all its items. `urgent`, `status`, `report` and `ops` are one item per push.

### D11. Concurrency and idempotency

Guaranteed by the service:

- **Same run twice, sequentially**: the second dispatch finds every subject delivered (`DUPLICATE`); no push is sent
  twice.
- **Same run twice, concurrently**: claims are taken under `BEGIN IMMEDIATE` (D9), which serializes writers; the
  second dispatch sees a fresh claim (`IN_FLIGHT`) and skips. At most one Apprise call per subject per episode, except
  after a crash between a successful send and its completion (at-least-once: the claim expires after `CLAIM_TTL` and
  the next dispatch sends again). Never lost, rarely duplicated.
- **Different runs out of order**: the watermark (D9) makes the older outcome `STALE` as a whole.
- No lock is held while Apprise is called.

Not guaranteed (the scheduler's job): serializing or coalescing runs (the `#CRITICAL` in `services/assessments.py`),
calling `dispatch` after `AssessmentService.run` returns, calling `dispatch_unavailable` when it raises, calling the
purge, and the extra dispatch after `urgent_failed`.

### D12. Observability

Structured events through `family_logistics.utils.logging`. Allowed fields are internal only; a test captures every
event of a dispatch and asserts no title, body, name, message or entity key appears.

| Event | Level | Fields |
|-------|-------|--------|
| `notification.dispatch_started` | info | `run_id`, `trigger`, `run_status`, `mode`, `open_findings` |
| `notification.decided` | debug | `subject_key`, `kind`, `rule_id`, `severity`, `intended_tag`, `decision` |
| `notification.subject_unresolved` | error | `rule_id`, `issue_id` |
| `notification.suppressed` | info | `subject_key`, `kind`, `reason` (below) |
| `notification.stale_outcome` | warning | `run_id`, `evaluated_at`, `watermark_run_id` |
| `notification.sent` | info | `subject_key` (first item), `kind`, `intended_tag`, `delivery_tag`, `mode`, `items` |
| `notification.delivery_failed` | warning | `subject_key`, `kind`, `delivery_tag`, `failed_attempts`, `retried` |
| `notification.claim_skipped` | debug | `subject_key`, `kind`, `decision` (`in_flight`, `stale`) |
| `notification.lock_retry` | warning | `attempt` |
| `notification.not_configured` | warning | `run_id`, `owed` |
| `notification.audit_skipped` | debug | `run_id` |
| `notification.audit_failed` | error | `subject_key`, `error` (class name) |
| `notification.dispatch_finished` | info | `run_id`, the `DispatchReport` counts, `duration_ms` |

`reason` is one of `quiet_hours`, `quiet_hours_dropped`, `bypass_used` or `cooldown`. The client's existing
`notification.failed` and `notification.rejected` events stay as they are.

### Interfaces

```python
# domain/notification_policy.py: pure, no I/O, no clock
class PushKind(StrEnum):
    NOTICE = "notice"; URGENT = "urgent"; STATUS = "status"; REPORT = "report"; OPS = "ops"

class Decision(StrEnum):
    SEND = "send"; DUPLICATE = "duplicate"; QUIET_HOURS = "quiet_hours"; BYPASS_USED = "bypass_used"
    COOLDOWN = "cooldown"; IN_FLIGHT = "in_flight"; STALE = "stale"

RENOTIFY_COOLDOWN: Final = timedelta(hours=4)
CLAIM_TTL: Final = timedelta(minutes=15)
SEND_DEADLINE: Final = timedelta(minutes=10)
URGENT_RETRY_DELAY: Final = timedelta(seconds=5)
CLAIM_LOCK_RETRIES: Final = 3
MAX_ITEMS_PER_PUSH: Final = 8
CLEAR_PHRASES: Final = ("Clear", "No open findings", "sources fresh")

@dataclass(frozen=True, slots=True)
class TagSet:
    report: str; status: str; warning: str; urgent: str; ops: str; shadow: str
    @classmethod
    def from_settings(cls, settings: Settings) -> TagSet: ...

@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    zone: ZoneInfo
    quiet_start: time
    quiet_end: time
    imminent_window: timedelta
    status_max_age: timedelta          # FL_CHANGE_SCAN_MINUTES
    pickup_tolerance: timedelta        # EngineSettings default
    tags: TagSet
    mode: Literal["shadow", "production"]
    @classmethod
    def from_settings(cls, settings: Settings) -> NotificationPolicy: ...

@dataclass(frozen=True, slots=True)
class PlannedPush:
    subject_key: str
    kind: PushKind
    intended_tag: str
    episode_start: bool = False        # lifecycle new or regressed in this run
    retry_only: bool = False           # failed run: send only if the row is open and undelivered
    run_wide: bool = False             # daily re-arm applies
    rule_id: str | None = None
    severity: Severity | None = None
    tracked: TrackedFinding | None = None
    subject: Subject | None = None
    run_status: RunStatus | None = None

@dataclass(frozen=True, slots=True)
class StateView:  # one notification_state row as the policy sees it
    is_open: bool; delivered: bool; kind: PushKind; severity: Severity | None; bypass_used: bool
    episode_run_id: str; last_sent_at: datetime | None; claimed_at: datetime | None

@dataclass(frozen=True, slots=True)
class Plan:
    pushes: tuple[PlannedPush, ...]   # urgent first, then notice, status/report, ops
    open_issue_keys: frozenset[str]   # issue:* subjects open in this result
    completed: bool                   # False for a failed or unavailable run: close nothing
    incomplete: bool                  # keeps ops:run_incomplete open

def in_quiet_hours(now: datetime, start: time, end: time, zone: ZoneInfo) -> bool: ...
def finding_kind(finding: Finding, *, imminent: bool) -> PushKind: ...   # the D3 table
def production_tag(kind: PushKind, tags: TagSet) -> str: ...
def delivery_tag(kind: PushKind, policy: NotificationPolicy) -> str: ...  # shadow -> tags.shadow
def plan(
    *, run_id: str, trigger: str, result: AssessmentResult, rides: RideIndex | None,
    now: datetime, policy: NotificationPolicy,
) -> Plan: ...
def plan_unavailable(*, now: datetime, policy: NotificationPolicy) -> Plan: ...
def decide(
    push: PlannedPush, state: StateView | None, *, run_id: str, now: datetime,
    quiet: bool, policy: NotificationPolicy,
) -> Decision: ...  # order: IN_FLIGHT, DUPLICATE, COOLDOWN, QUIET_HOURS or BYPASS_USED, SEND

# domain/notification_text.py: pure rendering
@dataclass(frozen=True, slots=True)
class RuleTemplate:
    label: str   # title fragment, e.g. "destination missing or unverified"
    line: str    # str.format with only {who}, {when}, {driver}, {source}

RULE_TEMPLATES: Final[Mapping[str, RuleTemplate]]   # keys == set(RULES)

@dataclass(frozen=True, slots=True)
class NameDirectory:
    family: Mapping[str, str]      # person_id (role child or parent) -> first name
    drivers: Mapping[str, str]     # person_id (any role) -> first name
    externals: Mapping[str, str]   # passenger_id -> "E."
    @classmethod
    def from_rows(
        cls, people: Iterable[tuple[str, str, str]], passengers: Iterable[tuple[str, str]]
    ) -> NameDirectory: ...   # (person_id, display_name, role), (passenger_id, display_first_name)
    @classmethod
    def empty(cls) -> NameDirectory: ...
    def passenger(self, passenger_id: str, kind: PassengerKind) -> str: ...  # name, initial or "a passenger"
    def driver(self, person_id: str | None) -> str: ...                     # first name or "the driver"

@dataclass(frozen=True, slots=True)
class RenderedPush:
    title: str   # <= 120 characters
    body: str    # <= 2000 characters

def render(
    pushes: Sequence[PlannedPush], *, names: NameDirectory, policy: NotificationPolicy,
    counts: Mapping[str, int] | None = None,
) -> RenderedPush: ...  # one chunk; adds the shadow header in shadow mode

# core/database.py
@contextmanager
def immediate_transaction(engine: Engine) -> Iterator[Connection]: ...  # BEGIN IMMEDIATE; commit or roll back

# db/notifications.py
def load_name_rows(engine: Engine) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]: ...

class NotificationStateRepository:
    def __init__(self, engine: Engine) -> None: ...
    def claim(
        self, plan: Plan, *, mode: str, token: str, run_id: str, evaluated_at: datetime,
        now: datetime, quiet: bool, policy: NotificationPolicy,
    ) -> ClaimResult: ...   # watermark, decisions, upserts and closes in one immediate transaction
    def complete(
        self, pushes: Sequence[PlannedPush], *, mode: str, token: str, delivered: bool,
        now: datetime, quiet: bool,
    ) -> None: ...
    def release(self, pushes: Sequence[PlannedPush], *, mode: str, token: str) -> None: ...

# services/notifications.py
@dataclass(frozen=True, slots=True)
class DispatchReport:
    run_id: str | None
    mode: Literal["shadow", "production"]
    sent: int = 0            # Apprise calls delivered
    failed: int = 0
    urgent_failed: int = 0   # the scheduler may re-dispatch in about 5 minutes
    held: int = 0            # quiet hours, bypass used or cooldown; still owed
    dropped: int = 0         # status or report dropped in quiet hours
    duplicates: int = 0
    in_flight: int = 0
    stale: bool = False
    unresolved: int = 0
    unconfigured: bool = False

class NotificationService:
    def __init__(
        self, engine: Engine, settings: Settings, client: AppriseClient | None, *,
        audit: AuditRepository | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None: ...   # audit defaults to AuditRepository.from_settings when a key is set
    def dispatch(self, outcome: RunOutcome) -> DispatchReport: ...
    def dispatch_unavailable(self, trigger: str) -> DispatchReport: ...
```

`dispatch` flow: read `now` once; build the policy and the `NameDirectory` (an unreadable directory gives
`NameDirectory.empty()`); resolve subjects and `plan`; if the client is None, log and return; claim (with lock
retries); render and send each claimed push or chunk (one in-dispatch retry for `urgent`), stopping new sends at
`SEND_DEADLINE`; complete or release; audit delivered pushes; log `dispatch_finished`. `dispatch_unavailable` runs the
same path over `plan_unavailable`, using `now` as its `evaluated_at` and a fresh run id of the form `unavailable-<uuid>`
truncated to 36 characters for `run_id` and `episode_run_id`.

### RAD markers for the implementation

```text
#CRITICAL: Security: children's data. A push is rendered only from templates
over rule_id, severity, lifecycle, the derived Subject and NameDirectory, which
looks names up by (kind, id): family first name only, external initial only.
Never Finding.message, suggestion, raw entity keys, notes, addresses, places,
calendar ids or UIDs.
#VERIFY: T-text-1, T-text-2, T-text-3, T-text-5 and T-text-11.
#CRITICAL: Timing: a failed or incomplete run is never reported as clear, and an
older outcome can never send after a newer one (watermark, status freshness).
#VERIFY: T-svc-1, T-svc-2, T-svc-21, T-svc-24 and T-text-6.
#CRITICAL: Timing: a ride in progress is imminent until it ends; its critical
problem is urgent and is not held by quiet hours.
#VERIFY: T-pol-1 and T-svc-3b.
#CRITICAL: External resources: Apprise may be down; notify() returning False
records nothing as sent, an urgent push is retried once, and dispatch never
fails the run.
#VERIFY: T-svc-7, T-svc-7b and T-svc-17.
#ASSUME: Timing: quiet hours 22:00 to 06:00 and the 180-minute imminent window
suit the household; one quiet-hours push per episode; CAP-014 never urgent.
#VERIFY: owner approval before FL_NOTIFICATION_MODE=production.
#ASSUME: Timing: an unknown ride set counts as imminent, so a failed run with no
readable history sends one urgent "could not verify" per local day.
#VERIFY: owner approval before FL_NOTIFICATION_MODE=production; T-pol-9.
#ASSUME: Data integrity: entity-key formats stay as tabulated in D7.
#VERIFY: T-subj-1 (zero unresolved over the acceptance matrix); unresolved
counts as imminent and logs notification.subject_unresolved.
#EDGE: Timing: DST. Quiet hours are evaluated per real instant; a nonexistent
boundary takes effect at the next existing local time, a repeated one on both passes.
#VERIFY: T-pol-4 and T-pol-5.
#EDGE: Concurrency: a crash between a delivered send and its completion resends
the push once after CLAIM_TTL (at-least-once).
#VERIFY: T-svc-16.
#ASSUME: Security: Apprise routes fl-shadow and fl-ops to the operator only and
the other tags to the parents' private channels.
#VERIFY: M0 check 7 and the shadow period; recorded in the private deployment repository.
```

## Owner-pending defaults

Everything below is decided provisionally so the implementation can proceed. Each needs the owner's approval (and
Riley's for family-facing items) before `FL_NOTIFICATION_MODE=production`. The structural decisions (templates,
derived subjects, kinds, the state table, the watermark, idempotency) are not in this list.

| Item | Proposed default | Section |
|------|------------------|---------|
| Quiet hours | 22:00 to 06:00, `FL_TIMEZONE` (existing settings) | D6 |
| Who bypasses quiet hours | `urgent` only, at most once per episode; ops held | D4, D6 |
| Imminent window | 180 minutes before start until the ride or move ends (existing setting) | D7 |
| CAP-014 | always `notice`, including over capacity | D3 |
| Unknown ride set | counts as imminent; one urgent per local day | D4, D7 |
| Reminders | none in v1 | D4 |
| Notice cooldown | 4 hours after a delivered push, for a new episode | D5 |
| Urgent cooldown for flapping findings | none in v1 (post-gate) | D5 |
| Urgent retry | once after 5 s, then the next dispatch | D8 |
| Status pushes | `daily` and `on_demand`; `weekly` to `fl-report`; incomplete runs labelled | D3 |
| Incomplete runs and ops | one ops push per incomplete episode | D3 |
| Warnings pushed | every warning-severity finding, once per episode | D3, D4 |
| Resolved notice | none in v1 | D4 |
| Mode switch | open issues pushed once more to both parents | D5 |
| Push content | family first names, external initials, local times, driver first names; no places | D2 |
| Chunk size | 8 items per `notice` push | D10 |
| State retention | 90 days after the last change, closed rows only (open rows never) | D9 |
| Digest | not built | out of scope |

## Consequences

### Positive

1. The full set of push texts is enumerable, so the privacy test is exhaustive rather than sampled.
2. The engine, `Finding` and `RULES_VERSION` are untouched; notification metadata cannot fail a run.
3. Held and failed pushes are retried by construction; a double, concurrent or out-of-order dispatch cannot
   double-send or send a stale clear.
4. Shadow mode previews production exactly, including timing, so the shadow period measures the real volume.
5. No new setting and no Home Assistant or recipient detail enters the app.

### Negative

1. The entity-key grammar becomes a contract for notifications as well as for `issue_id`; T-subj-1 guards it.
2. A warning-heavy week (many tentative rides, CONF-008) produces many `fl-warning` items; chunking bounds the call
   count, not the item count. The shadow period decides whether warnings need a digest.
3. A persistent failure with no readable history sends one urgent "could not verify" per day until fixed.
4. A problem known in the evening for an early ride escalates to urgent when the window opens at night, once.
5. Switching to production sends every open issue once more (post-gate item).

### Neutral

1. `fl-report` carries a short weekly summary until the report document exists.
2. The service sends nothing until the scheduler PR calls `dispatch` and `dispatch_unavailable`.

## Alternatives Considered

- **Counts and statuses only (ADR 0001 wording)**: safe, but "1 critical" cannot say which ride. Rejected.
- **Push `Finding.message` after a redaction filter**: messages hold raw note text; a filter is a blacklist and fails
  open. Rejected.
- **Add a `FindingSubject` to `Finding`, set by every emitter** (revision 1): touches the parser, change impact and
  about 25 emit sites, can make `assess` raise on an unregistered id and fail the run, and is lost for carried
  findings. Rejected (F3) for derivation from entity keys, which the run already holds.
- **Imminent only before the ride starts** (revision 1): demotes a problem found during a pickup window to a held
  notice, the exact stranded-child case. Rejected (F1).
- **Per-row monotonic guard** (revision 1): a status push or a new issue has no row to compare, so an older run could
  send "Clear" after a newer failure. Replaced by the global watermark (F2).
- **Notify on `new` and `regressed` per run**: loses pushes held for quiet hours or failed. Replaced by owing per
  episode (D4).
- **Shadow sends only the Sunday report to the operator**: hides the pushes the shadow period must evaluate (C1).
- **Ops alerts bypass quiet hours**: the operator cannot act at night; imminent cases already go to `fl-urgent`.
- **In-memory de-duplication**: lost on restart; not safe for concurrent dispatch.

## Implementation

Six PRs, each with its own tests and kept reviewable (about 500 lines or fewer where possible) (F14).

1. **This ADR** (revision 2) and the documentation-only follow-up edits below that do not describe unbuilt behaviour
   (ADR 0001 sentence, roadmap rows). Owner approval of the defaults table is recorded in the private deployment
   repository.
2. **Pure policy and subjects**: `domain/notification_policy.py`, `domain/notification_subjects.py`,
   `RunOutcome.trigger` and `RunOutcome.rides`, `_parse_rides` returning unparsed event windows, and refusing aware
   quiet-hours times in settings (N8). Tests: T-pol-*, T-subj-*.
3. **Rendering** (parallel with 2 once `PlannedPush` is fixed): `domain/notification_text.py`, `NameDirectory`,
   `CLEAR_PHRASES`. Tests: T-text-*.
4. **State** (parallel with 2 and 3): migration `0006`, `db/schema.py` mirror, `core.database.immediate_transaction`
   with the three existing callers moved to it, `db/notifications.py` (claim, complete, release, close, watermark,
   `load_name_rows`), the retention purge. Tests: T-db-*.
5. **Service**: `services/notifications.py` (`dispatch`, `dispatch_unavailable`, retries, audit, logs,
   `DispatchReport`) and the follow-up edits that describe the built service. Tests: T-svc-*. The scheduler PR then
   wires it (the M3 gate).
6. **Post-gate, before production**: reminders, a per-tag delivery-failure counter and ops alert, scheduler ops events
   (`report_ops`), mode-switch burst handling, an urgent cooldown for flapping findings.

### Follow-up edits

PR 1 makes the first two; PR 5 makes the rest. This revision makes none of them.

- `docs/ADRs/0001-deterministic-authority-and-llm-boundary.md`: amend "Notifications carry engine counts and statuses
  only" to "Notifications carry engine-decided findings, counts and statuses, rendered from templates (ADR 0008)".
- `docs/project/roadmap.md`: the M3 notification row points at ADR 0008 and its PR split; the scheduler row says it
  calls `dispatch` and `dispatch_unavailable`, and that incomplete runs alert once per episode (D3).
- `src/family_logistics/adapters/apprise/__init__.py` and the `notification_mode` docstring in `core/config.py`:
  replace "In shadow mode only the Sunday report goes to Morgan, and nothing goes to Riley" with D1. Tag docstrings:
  `apprise_tag_report` "weekly run summary", `apprise_tag_warning` "non-urgent findings", `apprise_tag_urgent`
  "critical or unverifiable imminent ride; one quiet-hours push per episode". `FIELD_STATUS` stays `M3` for quiet
  hours and tags until the scheduler first calls `dispatch`.
- `src/family_logistics/domain/context.py`: the `is_imminent` docstring only, to say "starts within the window or has
  started" (C3). No behaviour change.
- `src/family_logistics/services/notifications.py` and `services/scheduler.py` docstrings: point at this ADR.
- `docs/runbook.md`, "Notification modes": shadow row as D1; production row naming the tags and quiet hours.
  "Ops alerting": failed and incomplete episodes through `NotificationService` (held in quiet hours), and
  `dispatch_unavailable` when a run raises.
- `docs/guides/configuration.md`: tag rows as in the D3 table; quiet hours "one urgent push per episode bypasses";
  imminent window "from this long before a ride or move starts until it ends".
- `docs/privacy/privacy-and-retention-spec.md`: section 4, pushes are rendered from templates (ADR 0008), first names
  only, no place names in v1; section 5, the masking example as rendered (`Fri 30 Oct 15:15`); section 7, a row for
  `notification_state` (90 days, closed rows).
- `docs/ADRs/README.md`: status of 0008 once approved.

## Test Plan

Names are indicative; each assertion is required. "Payload" means the JSON posted to the mock Apprise transport.

### PR 2: policy, `tests/unit/test_notification_policy.py`

- **T-pol-1** `test_imminent_window_bounds`: True at `due_at - window`, at `due_at`, mid-ride and at `due_end`; False
  1 s before `due_at - window` and 1 s after `due_end`; window 0 is true only in progress
- **T-pol-2** `test_quiet_hours_wrap_midnight`: 22:00 to 06:00: 21:59 out, 22:00 in, 03:00 in, 05:59 in, 06:00 out
- **T-pol-3** `test_quiet_hours_same_day_window`: 13:00 to 15:00 behaves without wrap
- **T-pol-4** `test_quiet_hours_spring_forward`: 2027-03-14 `America/Los_Angeles`, boundary 02:30: no exception; in
  from 03:00 local
- **T-pol-5** `test_quiet_hours_fall_back`: 2026-11-01: both instants of 01:30 local are in a 01:00 to 02:00 window;
  22:00 to 06:00 lasts 9 h
- **T-pol-6** `test_severity_kind_tag_table`: parametrized over the D3 per-finding table, in both modes
- **T-pol-7** `test_warning_and_cap_014_are_never_urgent`: imminent warning and imminent critical CAP-014 are `notice`
- **T-pol-8** `test_run_wide_imminence_includes_transitions`: no imminent ride but an imminent bus move: imminent
- **T-pol-9** `test_unknown_ride_set_is_imminent_once_per_day`: `rides=None`: `urgent`; delivered today: not owed
  again until the next local day
- **T-pol-10** `test_only_urgent_bypasses_and_only_once`: every kind in quiet hours; a second urgent escalation in the
  same episode is `BYPASS_USED`
- **T-pol-11** `test_notice_cooldown_after_regression`: new episode within 4 h of the last delivery: `COOLDOWN`;
  urgent: `SEND`
- **T-pol-12** `test_escalation_and_no_downgrade`: warning then critical re-owes; urgent delivered then notice is
  `DUPLICATE` (F5)
- **T-pol-13** `test_regression_from_another_run_starts_episode`: open delivered row with another `episode_run_id`
  and lifecycle regressed: `SEND` (F4)
- **T-pol-14** `test_failed_run_plans_retries_only`: only `run-failed`, `ops:run_failed` and `retry_only` pushes;
  `completed` False
- **T-pol-15** `test_status_and_report_by_trigger`: the D3 run-level table, every status and trigger
- **T-pol-16** `test_plan_unavailable`: `run-failed` urgent and `ops:run_failed`; nothing closed

### PR 2: subjects, `tests/unit/test_notification_subjects.py`

- **T-subj-1** `test_acceptance_matrix_resolves_every_finding`: every finding of every acceptance scenario resolves
  (`RESOLVED`, `DAY` or `RUN_WIDE` for DATA-011); zero `UNRESOLVED`
- **T-subj-2** `test_each_rule_key_shape_resolves`: one case per row of the D7 table, including DRIVER-006 from
  `coverage.py` and parse findings on an unparsed event
- **T-subj-3** `test_exact_trip_key_wins_over_prefix`: a calendar id starting with `person:` resolves as a trip key
- **T-subj-4** `test_unregistered_ids_dropped_and_kinds_kept`: no `unregistered:` id in any subject; kinds preserved
- **T-subj-5** `test_transition_window`: `arrive_by`, `depart_after` plus tolerance, and leaving home
- **T-subj-6** `test_unresolved_counts_as_imminent`: a broken key yields `UNRESOLVED` and an imminent urgent plan
- **T-subj-7** `test_run_outcome_carries_trigger_and_rides`: completed; failed with history; failed without (None)
- **T-subj-8** `test_engine_unchanged`: `issue_id`, severities and `RULES_VERSION` equal before and after

### PR 3: rendering, `tests/unit/test_notification_text.py`

- **T-text-1** `test_every_rule_has_a_template`: `set(RULE_TEMPLATES) == set(RULES)`; placeholders only the four
- **T-text-2** `test_no_free_text_reaches_a_payload`: Hypothesis over every rule id, severity, lifecycle, kind and
  mode: sentinels in `message`, `suggestion`, `entity_keys`, `pickup`, `Destination.raw`, `free_notes`, location
  names, addresses, calendar id and UID never appear in title or body
- **T-text-3** `test_external_child_rendered_as_initial`: `E.` present, the full first name absent
- **T-text-4** `test_unregistered_and_unknown_ids_are_generic`: `a passenger`, `the driver`
- **T-text-5** `test_family_name_is_first_token_only`: a two-word display name renders only its first word
- **T-text-6** `test_clear_wording_only_in_clear_pushes`: the D3 regex never matches outside a clear status or
  report; no other template contains a `CLEAR_PHRASES` word
- **T-text-7** `test_shadow_header_names_production_tag`: `[shadow]` title prefix; first line names the intended tag
- **T-text-8** `test_lengths_within_client_limits`: title at most 120, body at most 2000, chunk of 8 at maximum
  name lengths
- **T-text-9** `test_data_011_source_map_is_closed`: known keys map; any other renders `a data source`
- **T-text-10** `test_failed_and_incomplete_texts`: no `AssessmentResult.error` text; incomplete status lists
  reasons as counts and says "could not fully verify"
- **T-text-11** `test_names_looked_up_by_kind`: an external id equal to a person id renders the initial, never the
  person's name

### PR 4: state, migration and retention

- **T-db-1** `test_migration_0006_upgrade_and_downgrade`: tables, indexes and every named CHECK, including the
  subject and kind pairing
- **T-db-2** `test_schema_mirror_matches_migration`: `db/schema.py` parity
- **T-db-3** `test_purge_expired_notification_state`: closed and old ops rows deleted, open issue rows kept,
  idempotent, naive `now` refused
- **T-db-4** `test_immediate_transaction_serializes_writers`: two threads; the second waits, then sees the first
  commit; the three migrated callers still pass their tests
- **T-db-5** `test_watermark_refuses_older_outcome`: older is stale, equal same run allowed, equal other run stale
- **T-db-6** `test_claim_retries_on_lock_contention`: a held write lock released within the retries: claim succeeds

### PR 5: service, `tests/unit/test_notification_service.py`

These run on SQLite at head with `httpx.MockTransport`, a fixed clock and a no-op sleep.

- **T-svc-1** `test_failed_run_sends_could_not_verify_never_clear`: `fl-warning` (`fl-urgent` with an imminent
  ride); no `fl-status` or `fl-report`; wording rule
- **T-svc-2** `test_incomplete_daily_status_is_labelled`: `fl-status` says "could not fully verify"; never clear
- **T-svc-3** `test_imminent_critical_bypasses_quiet_hours`: 23:30: one `fl-urgent` payload
- **T-svc-3b** `test_ride_in_progress_is_urgent_at_night`: critical found 5 minutes into a pickup window during
  quiet hours: one `fl-urgent` payload
- **T-svc-4** `test_held_notice_sent_after_quiet_hours`: 23:30 nothing posted, no delivered row; 06:10 posts once
- **T-svc-5** `test_same_run_dispatched_twice_sends_once`: one payload; the second report counts duplicates
- **T-svc-6** `test_concurrent_double_dispatch_is_idempotent`: two threads, one outcome: one payload per subject
- **T-svc-7** `test_failed_delivery_retried_next_dispatch`: 500 then 200 on the next dispatch: delivered once
- **T-svc-7b** `test_urgent_retried_once_in_dispatch`: 500 then 200: delivered in one dispatch; two 500s:
  `urgent_failed` 1
- **T-svc-8** `test_failed_run_retries_undelivered_urgent`: urgent failed in run 1, run 2 failed: urgent retried
- **T-svc-9** `test_dispatch_unavailable_tells_the_family`: `run-failed` urgent and `ops:run_failed`; a second
  call the same day sends nothing new
- **T-svc-10** `test_shadow_mode_routes_everything_to_shadow_tag`: every kind: payload tags equal `{"fl-shadow"}`
- **T-svc-11** `test_tags_never_outside_the_six`: production, all kinds: tags within the configured six
- **T-svc-12** `test_resolved_closes_state_and_recurrence_notifies`: resolve, then regress after the cooldown
- **T-svc-13** `test_ongoing_not_renotified`: three runs: one payload
- **T-svc-14** `test_urgent_then_notice_not_duplicated`: imminent urgent delivered; later non-imminent run: nothing
- **T-svc-15** `test_logs_carry_no_text_or_names`: captured events hold no sentinel, title or body
- **T-svc-16** `test_crash_after_send_resends_after_claim_ttl`: no resend before `CLAIM_TTL`, one resend after
- **T-svc-17** `test_dispatch_never_raises_on_delivery_failure`: transport errors and non-200s: report, no exception
- **T-svc-18** `test_unconfigured_client_writes_nothing`: no rows; `unconfigured` True
- **T-svc-19** `test_audit_per_delivered_push`: with key: one row per delivered push; none for held or failed
- **T-svc-20** `test_no_audit_without_key`: no row, no error
- **T-svc-21** `test_older_clear_after_newer_failed_sends_nothing`: newer failed dispatched first; older clear: stale
- **T-svc-22** `test_notices_are_chunked`: 20 owed notices: 3 payloads (8, 8, 4), each item delivered once
- **T-svc-23** `test_incomplete_episode_one_ops_push`: three incomplete runs: one `ops:run_incomplete` payload;
  a clear run closes it
- **T-svc-24** `test_late_status_refused`: outcome older than one scan interval: no status payload
- **T-svc-25** `test_regression_after_missed_dispatch_notifies`: resolve run not dispatched; regression pushes again

## References

- [ADR-001: Deterministic Engine Is the Sole Validation Authority](0001-deterministic-authority-and-llm-boundary.md)
- [ADR-002: Event Contract and Confirmation Code](0002-event-contract-and-confirmation-code.md)
- [ADR-004: Passenger-Level Commitments](0004-passenger-level-commitments.md)
- [ADR-005: SQLite WAL, Single Writer](0005-sqlite-wal-single-writer.md)
- [ADR-006: Route Provider Abstraction](0006-route-provider-abstraction.md)
- [ADR-007: Home Assistant Initiates Every Exchange](0007-ha-initiated-data-exchange.md)
- [Privacy and retention specification](../privacy/privacy-and-retention-spec.md), sections 4, 5 and 7
- [Runbook](../runbook.md), "Notification modes" and "Ops alerting"
- Site-specific recipients, Apprise routing and the defaults register are kept in the private deployment repository.

---

**Last Updated**: 2026-10-26 (revision 2)
**Reviewed By**: independent review 2026-10-26 (rework on D7, D9, D11, imminence; addressed in revision 2)
**Next Review**: before `FL_NOTIFICATION_MODE=production`
