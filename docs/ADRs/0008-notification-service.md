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
source: "Notification survey of 2026-10-26 (conflicts C1 to C3); ADR 0001; privacy spec sections 4, 5 and 7; runbook"
purpose: "Fix the M3 notification service design precisely enough to implement and test it."
---

> **Status**: `proposed`. The owner delegated the design decisions below; each
> value that needs the owner's approval before `FL_NOTIFICATION_MODE=production`
> is marked `#ASSUME` and collected in [Owner-pending defaults](#owner-pending-defaults).
> The front matter reads `draft` because the front-matter validator accepts only
> `draft`, `in-review` and `published` (see ADR 0006).

**Decision Date**: 2026-10-26
**Deciders**: Byron Williams (owner, delegated); Riley (co-approver for family-facing behaviour, ADR 0001)
**Affected Components**: services/notifications, domain (new `notification_policy`, `notification_text`;
`FindingSubject` on `Finding`), db (new `notifications`, migration `0006`, retention), services/assessments
(`RunOutcome`), adapters/apprise (docstring only)
**Implementation Target**: M3, one PR after this ADR; the scheduler PR wires `dispatch` afterwards

## Context

### Problem Statement

The engine (ADR 0001) decides findings and the run status; the Apprise client sends one message to one dedicated
`fl-*` tag and never retries. Nothing in between decides which findings become pushes, what they say, who gets them,
when they may wake someone, and how a push is sent exactly once. `services/notifications.py` is a stub whose TODO names
the concerns: de-duplication "by issue ID and revision", quiet hours with an imminent-critical bypass, shadow and
production routing, and "could not verify" pushes when a failure blocks validation of an imminent ride.

Pushes reach phones. They are the only channel that can wake a parent before a child is stranded, and the only channel
where a careless message leaks another family's child's name into a notification history. Both failure directions
matter: a missed or delayed urgent push, and a push that says too much.

### Current State (verified against `main` at 81c4e36)

- **C1, shadow routing.** The runbook ("Notification modes") says shadow mode sends *all* output to the operator-only
  `fl-shadow` tag. `adapters/apprise/__init__.py` and the `notification_mode` docstring in `core/config.py` say that in
  shadow mode "only the Sunday report goes to Morgan". The two cannot both hold.
- **C2, push content.** ADR 0001 says notifications carry "engine counts and statuses only". Privacy spec section 4
  allows, in pushes, a family child's first name, an external child's initial, ride times, the driver and venue
  names, and section 5 gives "Ride for E. at 3:15 needs a driver" as the model.
- **C3, imminence.** `AssessmentContext.is_imminent` (`domain/context.py`, lines 258 to 267) returns
  `start - now <= imminent_window` with no lower bound, so a ride that has already started is imminent, while its own
  docstring and the configuration guide say "starts within the window". Its only caller is DEST-004's severity
  (`domain/feasibility.py`, line 63).
- **Free text in findings.** `Finding.message` embeds names as written in calendar notes (`parser.py` formats them
  with `!r`) and `Finding.suggestion` can list registered names. `Finding.entity_keys` is no safer: it holds
  `SourceEventKey.as_str()` (calendar id, UID and RECURRENCE-ID), normalised raw names from notes
  (`family-name:<name>`, `external-unknown:<name>`) and location keys. None of the three may reach a push.
- **No ride time on a finding.** A `Finding` carries no structured time or passenger. A parse finding has no `Trip` at
  all, and a coverage finding (RIDE-005, MODE-015) concerns a move with no ride. The service cannot learn "when" from
  existing data without parsing messages, which is guessing.
- **Severity is per emission.** Several rules emit a severity other than the catalog default (DEST-004 warning or
  critical, PASS-003, CAP-014 incomplete or critical, DRIVER-006), so routing must use `Finding.severity`, never
  `RULES[rule_id].severity`.
- **`RunOutcome`** holds `run_id` and the result only; the trigger and the run's rides are not exposed. A failed run
  keeps the previous findings open as `ongoing` (`failed_result`) and writes no finding state.
- **Apprise client.** `AppriseClient.notify(tag, title, body) -> bool` truncates the title to 120 and the body to 2000
  characters, returns True only for HTTP 200, never retries, and raises `NotificationTagError` for any tag outside the
  six configured ones.
- **Audit.** `AuditRepository.record` raises `ConfigurationError` without `FL_AUDIT_KEY`; production `serve` refuses to
  start without the key (`cli.py`).

### Requirements

- **Must have**: a failed or incomplete run is never reported as clear on any tag (design rule 4).
- **Must have**: shadow mode is the default and nothing reaches the other parent in it (design rule 5).
- **Must have**: only the six dedicated tags; recipients live in Apprise, never in the app.
- **Must have**: no free text from calendars, no addresses, notes, UIDs or calendar ids in any push or log.
- **Must have**: a push that was not delivered, or was held for quiet hours, is sent by a later dispatch; a push that
  was delivered is not sent again for the same event, even if `dispatch` runs twice or concurrently.
- **Must have**: notification problems never block or fail an assessment run.
- **Should have**: a critical problem on an imminent ride reaches the parents at any hour.
- **Should have**: bounded volume: one push per issue per episode, a bounded reminder for urgent ones.

## Decision

> **We will add a notification service that turns one stored assessment outcome into templated pushes, decided by a
> pure policy module and made idempotent by a per-issue state table (migration `0006`), and we will render every push
> from the rule ID, severity, lifecycle and a new structured `FindingSubject`, never from a finding's message,
> suggestion or entity keys.**

The twelve delegated decisions follow, each with its reasoning. Values the owner must approve carry `#ASSUME`.

### D1. Shadow routing (resolves C1)

In shadow mode **every** push, including ops alerts, status, the weekly report and urgent pushes, is delivered to
`FL_APPRISE_TAG_SHADOW` (`fl-shadow`) only. The service still evaluates everything as production would (kind, intended
tag, quiet hours, de-duplication), so shadow is a faithful preview: the title starts with `[shadow]` and a space,
and the first body line states the tag production would have used, for example
`SHADOW: production would send this to fl-urgent (bypasses quiet hours).`

*Why*: the runbook rule is the conservative one and the one both parents agreed to; "only the Sunday report goes to
Morgan" would leave the operator blind to the very pushes the shadow period exists to evaluate. Routing is decided in
one function (`delivery_tag`), so "nothing reaches the other parent" depends only on how Apprise routes `fl-shadow`.

### D2. Push content (resolves C2)

Pushes are rendered from templates. There is one template per rule ID in `domain/rules.RULES` (a test pins the two key
sets equal) plus fixed templates for run, status, report and ops pushes. The only inputs a template may use are:

| Input | Source | Rendered as |
|-------|--------|-------------|
| Rule, severity | `Finding.rule_id`, `Finding.severity` | Template; `[CRITICAL]`, `[WARNING]`, `[COULD NOT VERIFY]` |
| Lifecycle | `TrackedFinding.lifecycle`, state row | `new`, `back again`, `still open`, `now critical` |
| When | `FindingSubject.due_at` or `.day`, `FL_TIMEZONE` | `Fri 30 Oct 15:15`, or `Fri 30 Oct` for a day |
| Family person | `FindingSubject.passenger_ids`, registry | Display name, filtered (below) |
| External child | same | Initial only: `E.` |
| Driver | `FindingSubject.driver_id`, registry | Display name, filtered (a contact such as Driver S) |
| Source (DATA-011) | Closed map of entity keys (below) | `calendar`, `school schedule`, `any data source` |
| Reference | `Finding.issue_id`, run id | `ref 1a2b3c4d5e6f7a8b` (internal, class I) |
| Counts, run status | `AssessmentResult.counts()`, `run_status` | Numbers and fixed labels |

The DATA-011 map matches the exact entity keys `source:icloud`, `source:ha_snapshot` and `source:none`; any other key
renders `a data source`.

Masking rules (`NameDirectory`, built once per dispatch from `db.registry.load_snapshot(engine)` with no cipher):

- A person's name is the registry `display_name` with every character outside letters, space, apostrophe and hyphen
  removed, spaces collapsed, cut to 30 characters. An external passenger is the first letter of `display_first_name`,
  upper-cased, plus `.`; no letter gives `a passenger`.
- An ID not in the registry, any ID starting with `unregistered:`, or a registry that cannot be read renders as
  `a passenger` or `the driver`. A rendering failure never blocks a push; it degrades to these words.
- Never rendered: `Finding.message`, `Finding.suggestion`, `Finding.entity_keys` (except the closed DATA-011 map),
  `Trip.pickup`, `Destination.raw`, `Trip.free_notes`, any location name, address or coordinate, calendar ids, UIDs,
  `AssessmentResult.error`.
- **No location names in v1.** The privacy spec allows venue names, but a location's `name` can be a home ("Household
  E home") and the app cannot tell a venue from a home reliably (ADR 0006 treats a passenger default as a venue).
  Omitting places costs little: the push says what is wrong and when, and the status card has the detail.

A fallback template exists for a rule ID that has no template at runtime (impossible while the key-set test passes):
`[<SEVERITY>] A ride check needs attention<, at {when}>. Rule <rule_id>, ref <issue_id>.` with `<rule_id>` printed
only if it is a key of `RULES`. It contains no free text.

*Why*: ADR 0001's "counts and statuses only" predates the privacy spec, which is the binding data rule set and was
written with pushes in mind. Counts alone cannot say *which* ride needs a driver, so they fail the purpose of a push.
Templates make the full set of possible output enumerable, which is what makes the "no free text" test exhaustive.
ADR 0001's sentence is corrected as a follow-up (see [Follow-up edits](#follow-up-edits)).

### D3. Kinds, severity and tags (the routing table)

Every push has a **kind**; the kind alone decides the production tag.

| Kind | Production tag | Produced by |
|------|----------------|-------------|
| `urgent` | `fl-urgent` | Critical or incomplete finding on an imminent ride; failed run, ride imminent or unknown |
| `reminder` | `fl-urgent` | A still-open critical finding on an imminent ride, after its `urgent` push (D4) |
| `notice` | `fl-warning` | Any other open finding; failed run, no imminent ride; incomplete run summary |
| `status` | `fl-status` | A `daily` or `on_demand` run that completed `clear` or `attention_required` |
| `report` | `fl-report` | A `weekly` run that completed `clear` or `attention_required` |
| `ops` | `fl-ops` | Failed run (once per episode); failing channel (D8); scheduler events via `report_ops` |

The incomplete run summary is sent only for `daily`, `weekly` and `on_demand` runs (run-level table below).

Per-finding decision table (completed runs only; a failed run sends no per-finding pushes, see D4):

| `Finding.severity` | Imminent (D7) | Kind | Production tag |
|--------------------|---------------|------|----------------|
| critical | yes | `urgent` | `fl-urgent` |
| critical | no | `notice` | `fl-warning` |
| incomplete | yes | `urgent` (worded "could not verify") | `fl-urgent` |
| incomplete | no | `notice` (worded "could not verify") | `fl-warning` |
| warning | either | `notice` | `fl-warning` |

Run-level decision table:

| Run status | Trigger | Push |
|------------|---------|------|
| `failed` | any | `run-failed` push (`urgent` if a ride is imminent or unknown, else `notice`); one `ops` |
| `incomplete` | `daily`, `weekly`, `on_demand` | Summary `notice` to `fl-warning`: "Could not verify every ride" |
| `incomplete` | `incremental` | None; the incomplete findings (DATA-011, ROUTE-010, CAP-014 ...) carry it |
| `attention_required` | `daily`, `on_demand` | `status` with counts |
| `attention_required` | `weekly` | `report` with counts |
| `clear` | `daily`, `on_demand` | `status`: "Clear: every ride in the window was checked against fresh data" |

A `failed` run sends one `ops` push per failed episode. An `incomplete` run never sends `fl-status` or `fl-report`.
| `clear` | `weekly` | `report`, same wording |
| `attention_required`, `clear` | `incremental` | None |

Rules that hold across both tables:

- The words "clear", "all clear", "no issues" and "all rides verified" (case-insensitive, whole words) appear only in a
  `status` or `report` push of a `clear` run. A test enforces this for every other kind and status (T-text-6).
- The status label of a run is rendered from a closed map: `clear`, `attention required`, `could not verify`
  (incomplete), `check failed` (failed).
- An incomplete run never produces `status` or `report`; a failed run never produces any `status`, `report` or
  per-finding push.
- Incomplete runs raise no `ops` push: the family-facing "could not verify" pushes cover them, and source failures
  reach `ops` through the scheduler's `COLLECTION_FAILED` event. `#ASSUME` (owner-pending).
- `fl-report` carries the weekly run's summary only. The full report document (e-mail, HTML) and any digest are out
  of scope; when they arrive they replace this summary on the same tag.

*Why*: urgency follows the ride, not the rule: a warning on an imminent ride does not justify waking anyone, an
unverifiable imminent ride does. Ops goes to the operator because only the operator can act on it.

### D4. Which lifecycle transitions notify

Notification is **owed per episode**, not per run, so it survives quiet hours and failed deliveries by construction.
An episode of an issue starts when its finding is `new` or `regressed` and ends when it is resolved.

- **new, regressed**: owed. The push is sent by the first dispatch that is allowed to send it.
- **ongoing**: not owed if this episode's push was delivered. Owed only if it was never delivered (held, failed, or
  open before the service existed), or if the severity rose above the severity last delivered (warning to incomplete
  to critical; ranking as in `domain/lifecycle._RANK`), or if the kind escalated from `notice` to `urgent` (the ride
  became imminent). A downgrade sends nothing.
- **Reminder**: for a critical finding with a `due_at` whose `urgent` push was delivered and which is still open and
  still imminent, a `reminder` is owed when `REMINDER_INTERVAL` (60 minutes) has passed since the later of the
  `urgent` push and the previous reminder, at most `REMINDER_LIMIT` (2) times per episode. Incomplete and run-wide
  urgent pushes get no reminder. `#ASSUME` (owner-pending).
- **Run-wide urgent re-arm**: an `urgent` push for a subject with no `due_at` (DATA-011, the `run-failed` episode) is
  owed again while the episode is open when its last delivery is older than the imminent window and a ride is imminent
  now. This gives one urgent "could not verify" per imminent window, not one per run and not one per episode only.
- **resolved**: sends nothing in v1. It closes the issue's state rows (D9), which clears de-duplication so a recurrence
  notifies again, subject to the cooldown (D5).
- **Failed run**: per-finding processing is skipped (its findings are the previous run's, carried as `ongoing`, with
  no `FindingSubject`), nothing is closed, and only the `run-failed` and `ops:run_failed` subjects are planned. The
  first completed run closes both.

*Why*: "new and regressed notify" evaluated per run would lose a push held at 23:00, because the next run sees the
finding as `ongoing`. Owing per episode keeps the intent and makes the held push go out after quiet hours.

### D5. De-duplication key, cooldown and "revision"

- **Key**: `(subject_key, kind, mode)`. `subject_key` is `issue:<issue_id>` for a finding (`issue_id` is already
  `sha256(rule_id + sorted entity keys)[:16]`), `run-failed` for the failed-run episode, `run:<run_id>` for a run-scoped
  push (status, report, incomplete summary), `ops:<event>` for an ops event and `ops:delivery_failing:<tag>` for a
  failing channel.
- **"Revision" is dropped.** The TODO's "issue ID and revision" assumed a content revision. Confirmation revisions are
  not stored (ADR 0002), and a changed ride that matters already changes the issue (a moved occurrence has a new key)
  or its severity. What may re-notify within an episode is exactly: severity escalation and kind escalation (D4). No
  revision column exists.
- **Mode in the key.** Switching to production starts a fresh key space, so every open issue is owed once to both
  parents on the first production dispatch. `#ASSUME` (owner-pending): this is a one-off burst of at most the open
  issue count, chunked (D10).
- **Cooldown**: `RENOTIFY_COOLDOWN = 4 hours`, a constant. A `notice` owed because an issue regressed is held while its
  last delivery for that key is younger than the cooldown, and sent by the first dispatch after it if still open.
  `urgent` and `reminder` have no cooldown: a problem that comes back on an imminent ride is new information.
  `#ASSUME` (owner-pending).
- **Constants, not settings.** Cooldown, reminder interval and limit, claim lifetime, failure threshold and chunk size
  are module constants in `domain/notification_policy.py`. No new `FL_` setting is added; quiet hours and the imminent
  window already are settings. A constant is promoted to a setting only if the shadow period shows the owner needs to
  tune it.

### D6. Quiet hours

- Window: `FL_QUIET_HOURS_START` to `FL_QUIET_HOURS_END` (defaults 22:00 and 06:00) in `FL_TIMEZONE`. An instant `now`
  is inside when its local wall time `t = now.astimezone(zone).time()` satisfies `start <= t < end` for a same-day
  window, or `t >= start or t < end` when `start > end` (wrapping midnight). `start == end` is refused by settings.
- Only `urgent` and `reminder` bypass. `notice`, `status`, `report` and `ops` are held. `#ASSUME` (owner-pending):
  ops alerts are held because the operator can act on them only in the morning unless a ride is imminent, and that
  case already produces an `urgent` push.
- **Held means not claimed and not recorded.** A held per-issue, `run-failed` or `ops` push has no delivered row, so
  the first dispatch after the window sends it if it is still owed. Idempotent by construction: holding writes nothing.
- A held **run-scoped** push (`status`, `report`, incomplete summary) is dropped, not deferred: a later run supersedes
  it and the HA card shows the current status. It is logged as `notification.suppressed` with
  `reason=quiet_hours_dropped`. A `FL_DAILY_CRON` or `FL_WEEKLY_CRON` inside quiet hours therefore loses that push;
  the scheduler PR adds a startup warning for it.
- **DST.** The policy never builds a local datetime from a boundary; it converts each real instant to local time, and
  every instant has exactly one local wall time. A boundary that does not exist on a spring-forward night (02:30 in
  `America/Los_Angeles`) takes effect at the first instant whose local time is at or after it (03:00); a boundary in a
  repeated hour on a fall-back night applies to both passes. The night's window is then one hour shorter or longer.
  Imminence and cooldowns are computed on aware UTC instants and are unaffected by DST.
- Shadow mode applies quiet hours exactly as production would (D1).

### D7. Imminence on the notification side (resolves C3 for notifications only)

- A finding's ride is **imminent** when `0 <= due_at - now <= imminent_window`, with `imminent_window =
  FL_IMMINENT_WINDOW_MINUTES` (180) and `now` the dispatch clock. A ride that has started is not imminent: its
  findings go to `fl-warning` as `notice`. `#EDGE`: a problem first found after a ride starts during quiet hours waits
  until morning; before the start it would have been `urgent`.
- **How the service learns `due_at`**: from a new field set where the finding is emitted, never from text.

```python
# domain/models.py (additions)
UNREGISTERED_PREFIX: Final = "unregistered:"  # moved here; parser.py re-exports it


@dataclass(frozen=True, slots=True)
class FindingSubject:
    """Push-safe facts about what a finding concerns (registry IDs and times only).

    Attributes:
        due_at: Ride start, the move's ``Transition.reference_time`` or the lost
            leg's start (aware, UTC); None for run-wide findings.
        day: Local day for day-level findings (SCHOOL-009); None otherwise.
        passenger_ids: Registry person or passenger IDs; never ``unregistered:*``.
        driver_id: Registry person ID of the driver, if the finding concerns one.
    """

    due_at: datetime | None = None
    day: date | None = None
    passenger_ids: tuple[str, ...] = ()
    driver_id: str | None = None

    def __post_init__(self) -> None:
        """Raise ValueError for a naive ``due_at`` or an ``unregistered:`` ID."""

    @classmethod
    def for_trip(
        cls, trip: Trip, passenger_ids: Iterable[str] | None = None
    ) -> FindingSubject:
        """Subject of a ride: its start, its driver and the given (default: all)
        passengers, dropping ``unregistered:`` IDs."""


# Finding gains one field, excluded from equality and from issue_id:
#     subject: FindingSubject | None = field(default=None, compare=False)
# domain/rules.make_finding gains a keyword-only ``subject: FindingSubject | None = None``.
```

| Rule | Emitted in | `due_at` | `day` | `passenger_ids` | `driver_id` |
|------|------------|----------|-------|-----------------|-------------|
| PARSE-001, NAME-002, EXT-013 | `parser.py` | the occurrence start (`parse_trip(start=...)`) | | none | none |
| PASS-003 | `change_impact.py` | the lost leg's start | | the lost passenger | the leg's driver |
| CONF-008 | `change_impact.py`; `confirmation.py` via its caller in `feasibility.py` | the trip start | | none | none |
| DEST-004 | `feasibility.py` | the trip start | | the affected passenger | none |
| TIME-007, ORDER-012, ROUTE-010 (ride) | `feasibility.py` | the trip start | | the trip's passengers | the driver |
| CAP-014 | `capacity.py` | the trip start | | none | the driver |
| DRIVER-006, ROUTE-010 (reach) | `driver.py` | the ride's start | | the passenger (DRIVER-006) | the driver |
| RIDE-005, MODE-015, ROUTE-010 (place) | `coverage.py` | the move's `reference_time` | | the person | default driver |
| SCHOOL-009 | `coverage.py` | none | the school day | the person | none |
| DATA-011 | `engine.py` | none (run-wide) | | none | none |

- A finding with no `due_at` (DATA-011, SCHOOL-009, or a subject missing by mistake) uses the **run-wide** rule: it is
  imminent when any ride of the run is imminent. `RunOutcome` gains the run's ride starts for this:

```python
@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    result: AssessmentResult
    trigger: str = "on_demand"
    ride_starts: tuple[datetime, ...] | None = None
    """Starts of the rides the run accepted. A failed run carries the stored
    snapshot's starts when the history was read, else None. None means unknown,
    and unknown counts as imminent."""
```

- `#ASSUME` (owner-pending): an unknown ride set (a failed run whose history could not be read) counts as imminent,
  so its "could not verify" push is `urgent`. This fails toward attention; a permanently failing run re-arms it at most
  once per imminent window (D4).
- **The engine is not changed.** C3 is recorded as an owner follow-up: adding the lower bound to
  `AssessmentContext.is_imminent` changes DEST-004 severity for rides in progress, which is an engine rule change and
  needs `RULES_VERSION` and the owner. Until then a DEST-004 on a started ride is `critical` (engine) but is sent as a
  `notice` (not imminent here); both behaviours are documented and tested.
- `FindingSubject` is additive: it changes no `issue_id`, severity or lifecycle, and it is not stored in the `finding`
  table. The change touches the parser and change-impact modules, so the PR gets an Opus review (CLAUDE.md, Model
  Selection).

### D8. Delivery failure

- `notify` returning False means not delivered. The claim is released, the row's `failed_attempts` is incremented and
  nothing is marked sent, so the next dispatch retries. The client never retries and the service does not retry within
  a dispatch. `#ASSUME` (owner-pending): an urgent push that fails waits for the next dispatch (the incremental scan,
  60 minutes by default).
- Per **delivery tag**, `notification_channel.consecutive_failures` counts failed sends and resets on a success. When
  it reaches `CHANNEL_FAILURE_THRESHOLD` (3) and the channel has not been alerted in this failing streak, an `ops`
  push `ops:delivery_failing:<tag>` is planned (owed until delivered, closed by the next success on that tag).
- If the failing tag is the one the ops alert would use (`fl-ops` in production, `fl-shadow` in shadow mode), no alert
  is attempted; `notification.ops_unreachable` is logged at error level once per streak. Apprise being down entirely
  therefore produces log lines only, and the external monitor of `/healthz` and logs remain the backstop.
- `dispatch` never raises for a delivery failure. It raises only `NotificationTagError` (a programming error) and
  `DatabaseError` (state not writable). Either reaches the scheduler, which logs it; the run is already stored, so its
  status is unaffected.
- `FL_APPRISE_URL` unset (allowed in shadow mode): `dispatch` plans, logs `notification.not_configured`, writes nothing
  and returns `unconfigured=True`; everything stays owed.

### D9. State tables (migration `0006_notification_state`)

Revision `0006_notification_state`, `down_revision = "0005_route_relay"`. Neither table holds personal data: subject
keys are hashes, run ids and fixed event names; tags are the configured `fl-*` values.

```sql
CREATE TABLE notification_state (
    subject_key      VARCHAR(96) NOT NULL,
    kind             VARCHAR(16) NOT NULL,
    mode             VARCHAR(16) NOT NULL,
    tag              VARCHAR(64) NOT NULL,   -- intended production tag
    rule_id          VARCHAR(16),            -- NULL for run and ops subjects
    severity         VARCHAR(16),            -- severity last delivered; NULL until delivered
    is_open          BOOLEAN NOT NULL,
    delivered        BOOLEAN NOT NULL DEFAULT 0,
    send_count       INTEGER NOT NULL DEFAULT 0,
    failed_attempts  INTEGER NOT NULL DEFAULT 0,
    first_sent_at    DATETIME,
    last_sent_at     DATETIME,
    last_attempt_at  DATETIME,
    claim_token      VARCHAR(36),
    claimed_at       DATETIME,
    run_id           VARCHAR(36),            -- last run that touched the row; no FK (runs are purged)
    evaluated_at     DATETIME NOT NULL,      -- evaluated_at of that run's result (monotonic guard)
    created_at       DATETIME NOT NULL,
    updated_at       DATETIME NOT NULL,
    PRIMARY KEY (subject_key, kind, mode),
    CONSTRAINT ck_notification_state_kind
        CHECK (kind IN ('notice', 'urgent', 'reminder', 'status', 'report', 'ops')),
    CONSTRAINT ck_notification_state_mode CHECK (mode IN ('shadow', 'production')),
    CONSTRAINT ck_notification_state_tag CHECK (tag GLOB 'fl-[a-z0-9]*'),
    CONSTRAINT ck_notification_state_severity
        CHECK (severity IS NULL OR severity IN ('critical', 'warning', 'incomplete')),
    CONSTRAINT ck_notification_state_counts CHECK (send_count >= 0 AND failed_attempts >= 0),
    CONSTRAINT ck_notification_state_claim CHECK ((claim_token IS NULL) = (claimed_at IS NULL)),
    CONSTRAINT ck_notification_state_delivered
        CHECK (delivered = 0 OR (first_sent_at IS NOT NULL AND last_sent_at IS NOT NULL)),
    CONSTRAINT ck_notification_state_subject CHECK (
        subject_key GLOB 'issue:[0-9a-f]*' OR subject_key = 'run-failed'
        OR subject_key GLOB 'run:*' OR subject_key GLOB 'ops:*')
);
CREATE INDEX ix_notification_state_open ON notification_state (is_open);
CREATE INDEX ix_notification_state_updated_at ON notification_state (updated_at);

CREATE TABLE notification_channel (
    tag                   VARCHAR(64) PRIMARY KEY,   -- delivery tag
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_failure_at       DATETIME,
    last_success_at       DATETIME,
    alerted_at            DATETIME,                  -- set when the streak's ops alert was planned
    updated_at            DATETIME NOT NULL,
    CONSTRAINT ck_notification_channel_tag CHECK (tag GLOB 'fl-[a-z0-9]*'),
    CONSTRAINT ck_notification_channel_failures CHECK (consecutive_failures >= 0)
);
```

The DDL is written with `op.create_table` and mirrored in `db/schema.py` with the same constraint names (as the
pilot-core tables are). Timestamp columns use `UtcDateTime`. Downgrade drops both tables.

Row rules:

- **Claim** (one write transaction): for each planned push, read its row and its related row (the `urgent` row for a
  `reminder`), call the pure `decide`, and for `SEND` upsert the row with `claim_token`, `claimed_at = now`, `tag`,
  `rule_id`, `run_id`, `evaluated_at`, `is_open = 1` for issue, `run-failed` and ops subjects and `0` for `run:*`. A
  claim older than `CLAIM_TTL` (15 minutes) is treated as abandoned.
- **Complete** (one write transaction per push): delivered sets `delivered = 1`, `severity`, `last_sent_at = now`,
  `first_sent_at` if NULL, `send_count + 1`, `failed_attempts = 0`; not delivered sets `failed_attempts + 1`,
  `last_attempt_at`; both clear the claim, but only where `claim_token` still equals this dispatch's token.
- **Close** (in the claim transaction of a completed run): every `issue:*` row with `is_open = 1` whose issue is not
  open in this result, plus `run-failed` and `ops:run_failed`, gets `is_open = 0`, `delivered = 0`, `severity = NULL`,
  `send_count = 0`; `last_sent_at` is kept for the cooldown. `ops:delivery_failing:<tag>` closes on a success to
  `<tag>`; scheduler ops subjects close through `report_ops(event, active=False)`.
- **Monotonic guard**: claim and close skip a row whose `evaluated_at` is newer than the outcome's
  `result.evaluated_at` (`Decision.STALE`), so a late dispatch of an older run cannot reopen or close what a newer run
  decided.
- **Retention**: `purge_expired_notification_state(connection, *, now) -> int` in `db/retention.py` deletes
  `notification_state` rows with `is_open = 0` and `updated_at` more than `NOTIFICATION_STATE_RETENTION_DAYS` (90)
  days before `now`, and `notification_channel` rows not updated for 90 days; it is idempotent and refuses a naive
  `now`. 90 days matches resolved findings and run logs (privacy spec section 7). `#ASSUME` (owner-pending). The
  scheduler's daily job calls it with the other purges.

### D10. Audit

- One audit row per **delivered** push when `FL_AUDIT_KEY` is configured:
  `record(actor="notifier", object_ref=subject_key, action="notification_sent", new_state={"kind", "tag",
  "delivery_tag", "mode", "rule_id", "severity", "run_id", "chunk_size"}, correlation_id=run_id)`. `object_ref` is
  `issue:<issue_id>`, `run:<run_id>`, `run-failed` or `ops:...`, never personal data; the state is only hashed.
- No key: skipped, with one `notification.audit_skipped` debug line per dispatch. Production mode already requires the
  key (`cli.py`), so production pushes are always audited.
- The audit write runs in its own transaction **after** the delivered row is committed. An audit failure is logged as
  `notification.audit_failed` (error class only) and never undoes or repeats the send: a rolled-back delivered mark
  would make the push go out again on every dispatch.
- Holds, duplicates and failures are not audited; they are logged (D12).

Chunking: per dispatch, owed `notice` pushes on the same delivery tag are grouped into chunks of at most
`MAX_ITEMS_PER_PUSH` (8) items, most severe and soonest first, one Apprise call per chunk; each item is claimed and
completed individually, and a chunk's result applies to all its items. `urgent`, `reminder`, `status`, `report` and
`ops` are one item per push.

### D11. Concurrency and idempotency

Guaranteed by the service:

- **Same run twice, sequentially**: the second dispatch finds every key delivered (`DUPLICATE`) or still owed for a
  reason it re-evaluates; no push is sent twice.
- **Same run twice, concurrently**: claims are taken in one SQLite write transaction (`BEGIN IMMEDIATE` through the
  single-writer engine, ADR 0005); the second dispatch sees a fresh claim (`IN_FLIGHT`) and skips. At most one Apprise
  call per key per episode, except after a crash between a successful send and its completion (at-least-once: the
  claim expires after `CLAIM_TTL` and the next dispatch sends again). Never lost, rarely duplicated.
- **Different runs out of order**: the monotonic guard (D9) keeps the newer run's state.
- No lock is held while Apprise is called.

Not guaranteed (the scheduler's job): serializing or coalescing runs (the `#CRITICAL` in `services/assessments.py`),
calling `dispatch` after `AssessmentService.run` returns, calling the purge, and calling `report_ops` for collection,
database and budget events. `dispatch` assumes nothing about run order beyond the guard.

### D12. Observability

Structured events through `family_logistics.utils.logging`. Allowed fields are internal only; a test captures every
event of a dispatch and asserts no title, body, name, message or entity key appears (T-svc-15).

| Event | Level | Fields |
|-------|-------|--------|
| `notification.dispatch_started` | info | `run_id`, `trigger`, `run_status`, `mode`, `open_findings` |
| `notification.decided` | debug | `subject_key`, `kind`, `rule_id`, `severity`, `intended_tag`, `decision` |
| `notification.suppressed` | info | `subject_key`, `kind`, `reason` (below) |
| `notification.sent` | info | `subject_key` (first item), `kind`, `intended_tag`, `delivery_tag`, `mode`, `items` |
| `notification.delivery_failed` | warning | `subject_key` (first), `kind`, `delivery_tag`, `failed_attempts`, `items` |
| `notification.claim_skipped` | debug | `subject_key`, `kind`, `decision` (`in_flight`, `stale`) |
| `notification.channel_failing` | warning | `tag`, `consecutive_failures` |
| `notification.ops_unreachable` | error | `tag`, `consecutive_failures` |
| `notification.not_configured` | warning | `run_id`, `owed` |
| `notification.audit_skipped` | debug | `run_id` |
| `notification.audit_failed` | error | `subject_key`, `error` (class name) |
| `notification.dispatch_finished` | info | `run_id`, the `DispatchReport` counts, `duration_ms` |

`reason` is one of `quiet_hours`, `quiet_hours_dropped` or `cooldown`. The client's existing `notification.failed`
and `notification.rejected` events stay as they are.

### Interfaces

```python
# domain/notification_policy.py: pure, no I/O, no clock
class PushKind(StrEnum):
    NOTICE = "notice"; URGENT = "urgent"; REMINDER = "reminder"
    STATUS = "status"; REPORT = "report"; OPS = "ops"

class OpsEvent(StrEnum):
    RUN_FAILED = "run_failed"; DELIVERY_FAILING = "delivery_failing"
    COLLECTION_FAILED = "collection_failed"; DATABASE_DEGRADED = "database_degraded"
    ROUTING_BUDGET_WARNING = "routing_budget_warning"

class Decision(StrEnum):
    SEND = "send"; DUPLICATE = "duplicate"; NOT_DUE = "not_due"; QUIET_HOURS = "quiet_hours"
    COOLDOWN = "cooldown"; IN_FLIGHT = "in_flight"; STALE = "stale"

RENOTIFY_COOLDOWN: Final = timedelta(hours=4)
REMINDER_INTERVAL: Final = timedelta(minutes=60)
REMINDER_LIMIT: Final = 2
CLAIM_TTL: Final = timedelta(minutes=15)
CHANNEL_FAILURE_THRESHOLD: Final = 3
MAX_ITEMS_PER_PUSH: Final = 8

@dataclass(frozen=True, slots=True)
class TagSet:  # report, status, warning, urgent, ops, shadow
    report: str; status: str; warning: str; urgent: str; ops: str; shadow: str
    @classmethod
    def from_settings(cls, settings: Settings) -> TagSet: ...

@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    zone: ZoneInfo
    quiet_start: time
    quiet_end: time
    imminent_window: timedelta
    tags: TagSet
    mode: Literal["shadow", "production"]
    @classmethod
    def from_settings(cls, settings: Settings) -> NotificationPolicy: ...

@dataclass(frozen=True, slots=True)
class PlannedPush:
    subject_key: str
    kind: PushKind
    intended_tag: str
    rule_id: str | None = None
    severity: Severity | None = None
    tracked: TrackedFinding | None = None    # render input for issue subjects
    run_status: RunStatus | None = None      # run-level pushes
    ops_event: OpsEvent | None = None
    due_at: datetime | None = None           # None: run-wide

@dataclass(frozen=True, slots=True)
class StateView:  # one notification_state row as the policy sees it
    is_open: bool; delivered: bool; severity: Severity | None; send_count: int
    last_sent_at: datetime | None; claimed_at: datetime | None; evaluated_at: datetime

@dataclass(frozen=True, slots=True)
class Plan:
    pushes: tuple[PlannedPush, ...]   # urgent first, then reminder, notice, status/report, ops
    open_issue_keys: frozenset[str]   # issue:* subjects open in this result
    completed: bool                   # False for a failed run: close nothing

def is_imminent(due_at: datetime, now: datetime, window: timedelta) -> bool: ...
def any_ride_imminent(
    ride_starts: Sequence[datetime] | None, now: datetime, window: timedelta
) -> bool: ...  # None -> True
def in_quiet_hours(now: datetime, start: time, end: time, zone: ZoneInfo) -> bool: ...
def bypasses_quiet_hours(kind: PushKind) -> bool: ...  # URGENT, REMINDER
def finding_kind(
    finding: Finding, *, now: datetime, window: timedelta, run_imminent: bool
) -> PushKind: ...  # NOTICE or URGENT, by the D3 table
def production_tag(kind: PushKind, tags: TagSet) -> str: ...
def delivery_tag(kind: PushKind, policy: NotificationPolicy) -> str: ...  # shadow -> tags.shadow
def plan(
    *, run_id: str, trigger: str, result: AssessmentResult,
    ride_starts: tuple[datetime, ...] | None, now: datetime, policy: NotificationPolicy,
) -> Plan: ...
def decide(
    push: PlannedPush, state: StateView | None, *, related: StateView | None,
    now: datetime, evaluated_at: datetime, quiet: bool,
) -> Decision: ...  # order: STALE, IN_FLIGHT, DUPLICATE/NOT_DUE, QUIET_HOURS, COOLDOWN, SEND

# domain/notification_text.py: pure rendering
@dataclass(frozen=True, slots=True)
class RuleTemplate:
    label: str   # title fragment, e.g. "destination missing or unverified"
    line: str    # str.format with only {who}, {when}, {driver}, {source}

RULE_TEMPLATES: Final[Mapping[str, RuleTemplate]]   # keys == set(RULES)

@dataclass(frozen=True, slots=True)
class NameDirectory:
    names: Mapping[str, str]       # person_id -> filtered display name
    initials: Mapping[str, str]    # passenger_id -> "E."
    @classmethod
    def from_registry(cls, registry: Registry) -> NameDirectory: ...
    @classmethod
    def empty(cls) -> NameDirectory: ...
    def passenger(self, passenger_id: str) -> str: ...   # name, initial or "a passenger"
    def driver(self, person_id: str | None) -> str: ...  # name or "the driver"

@dataclass(frozen=True, slots=True)
class RenderedPush:
    title: str   # <= 120 characters
    body: str    # <= 2000 characters

def render(
    pushes: Sequence[PlannedPush], *, names: NameDirectory, policy: NotificationPolicy,
    counts: Mapping[str, int] | None = None,
) -> RenderedPush: ...  # one chunk; adds the shadow header in shadow mode

# db/notifications.py
class NotificationStateRepository:
    def __init__(self, engine: Engine) -> None: ...
    def claim(
        self, plan: Plan, *, mode: str, token: str, run_id: str | None,
        evaluated_at: datetime, now: datetime, quiet: bool,
    ) -> ClaimResult: ...   # claimed pushes plus per-decision counts; closes rows of a completed run
    def complete(
        self, pushes: Sequence[PlannedPush], *, mode: str, token: str,
        delivered: bool, now: datetime,
    ) -> None: ...
    def record_channel(self, tag: str, *, delivered: bool, now: datetime) -> ChannelState: ...
    def close_subject(self, subject_key: str, *, now: datetime) -> int: ...

# services/notifications.py
@dataclass(frozen=True, slots=True)
class DispatchReport:
    run_id: str | None
    mode: Literal["shadow", "production"]
    sent: int = 0          # pushes (Apprise calls) delivered
    failed: int = 0
    held: int = 0          # quiet hours or cooldown, still owed
    dropped: int = 0       # run-scoped pushes dropped in quiet hours
    duplicates: int = 0
    in_flight: int = 0
    unconfigured: bool = False

class NotificationService:
    def __init__(
        self, engine: Engine, settings: Settings, client: AppriseClient | None, *,
        audit: AuditRepository | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None: ...   # audit defaults to AuditRepository.from_settings when a key is set
    def dispatch(self, outcome: RunOutcome) -> DispatchReport: ...
    def report_ops(self, event: OpsEvent, *, active: bool = True) -> DispatchReport: ...
```

`dispatch` flow: read `now` once; build the policy and the `NameDirectory` (an unreadable registry gives
`NameDirectory.empty()`); `plan`; if the client is None, log and return; claim; render and send each claimed push or
chunk; complete it; update the channel and plan a `delivery_failing` alert if the threshold is reached (sent in the
same dispatch); audit delivered pushes; log `dispatch_finished`. `report_ops` runs the same claim, send and complete
path for one `ops:<event>` subject, or closes it when `active=False`.

### RAD markers for the implementation

```text
#CRITICAL: Security: children's data. A push is rendered only from templates
over rule_id, severity, lifecycle, FindingSubject and registry names; never from
Finding.message, Finding.suggestion, entity_keys, notes, addresses, location
names, calendar ids or UIDs. An external child appears only as an initial.
#VERIFY: T-text-2 (Hypothesis over every rule id, severity and lifecycle with
sentinel strings in every forbidden field) and T-text-1 (template keys equal RULES).
#CRITICAL: Timing: a failed or incomplete run is never reported as clear, and a
push held for quiet hours or not delivered stays owed.
#VERIFY: T-svc-1, T-svc-2, T-svc-4, T-svc-7 and T-text-6.
#CRITICAL: External resources: Apprise may be down; notify() returning False
records nothing as sent, and dispatch never fails the run.
#VERIFY: T-svc-7, T-svc-8, T-svc-9 and T-svc-17.
#ASSUME: Timing: quiet hours 22:00 to 06:00 and the 180-minute imminent window
suit the household; only urgent and reminder pushes bypass quiet hours.
#VERIFY: owner approval before FL_NOTIFICATION_MODE=production.
#ASSUME: Timing: an unknown ride set counts as imminent, so a failed run with no
readable history sends an urgent "could not verify".
#VERIFY: owner approval before FL_NOTIFICATION_MODE=production; T-pol-9.
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

Everything below is decided provisionally by this ADR so the implementation can proceed. Each needs the owner's
approval (and Riley's for family-facing items) before `FL_NOTIFICATION_MODE=production`. The ADR's structural
decisions (templates, kinds, the state table, idempotency) are not in this list.

| Item | Proposed default | Where it lives | Section |
|------|------------------|----------------|---------|
| Quiet hours | 22:00 to 06:00, `FL_TIMEZONE` | `FL_QUIET_HOURS_START`, `FL_QUIET_HOURS_END` (existing) | D6 |
| Who bypasses quiet hours | `urgent` and `reminder` only; ops held | policy | D6 |
| Imminent window | 180 minutes, `0 <= start - now <= window` | `FL_IMMINENT_WINDOW_MINUTES` (existing) | D7 |
| Unknown ride set | counts as imminent (urgent) | policy | D7 |
| Re-notify cooldown | 4 hours, `notice` only | `RENOTIFY_COOLDOWN` | D5 |
| Reminder | every 60 minutes, at most 2, imminent critical only | `REMINDER_INTERVAL`, `REMINDER_LIMIT` | D4 |
| Delivery-failure alert | after 3 consecutive failures per tag | `CHANNEL_FAILURE_THRESHOLD` | D8 |
| Retry of a failed urgent push | next dispatch only (no in-dispatch retry) | policy | D8 |
| Status pushes | `daily` and `on_demand` runs; `weekly` to `fl-report` | policy | D3 |
| Warnings pushed | every warning-severity finding, once per episode | policy | D3, D4 |
| Incomplete runs and ops | no ops alert | policy | D3 |
| Resolved notice | none in v1 | policy | D4 |
| Mode switch | open issues are pushed once more to both parents | key includes `mode` | D5 |
| Push content | family first names, external initials, local times, driver names; no places | templates | D2 |
| Chunk size | 8 items per `notice` push | `MAX_ITEMS_PER_PUSH` | D10 |
| Digest | not built | | out of scope |
| State retention | 90 days after the row's last change, closed rows only | `NOTIFICATION_STATE_RETENTION_DAYS` | D9 |
| Engine imminence (C3) | unchanged; owner decides whether DEST-004 gains the lower bound | `domain/context.py` | D7 |

## Consequences

### Positive

1. The full set of push texts is enumerable, so the privacy test is exhaustive rather than sampled.
2. Held and failed pushes are retried by construction; a double or concurrent dispatch cannot double-send.
3. Shadow mode previews production exactly, including timing, so the shadow period measures the real volume.
4. No new setting and no Home Assistant or recipient detail enters the app.

### Negative

1. `FindingSubject` touches every rule module, including the parser and change impact (Opus review).
2. A warning-heavy week (many tentative rides, CONF-008) produces many `fl-warning` items; chunking bounds the call
   count, not the item count. The shadow period decides whether warnings need a digest.
3. A persistent DATA-011 (for example, no school snapshot) sends one urgent "could not verify" per imminent window.
4. A failed urgent push waits up to one scan interval for its retry.
5. Switching to production sends every open issue once more.

### Neutral

1. `fl-report` carries a short weekly summary until the report document exists.
2. The service sends nothing until the scheduler PR calls `dispatch`.

## Alternatives Considered

- **Counts and statuses only (ADR 0001 wording)**: Safe, but "1 critical" cannot say which ride; parents would open the
  card for every push. Rejected.
- **Push `Finding.message` after a redaction filter**: Messages hold raw note text; a filter is a blacklist and fails
  open. Rejected.
- **Notify on `new` and `regressed` per run**: Loses pushes held for quiet hours or failed. Replaced by owing per
  episode (D4).
- **Shadow sends only the Sunday report to the operator**: Hides the pushes the shadow period must evaluate (C1).
  Rejected.
- **Ops alerts bypass quiet hours**: The operator cannot act at night; imminent cases already go to `fl-urgent`. Held
  instead (owner-pending).
- **In-memory de-duplication**: Lost on restart; not safe for concurrent dispatch. Rejected for the state table.
- **Derive ride times from `entity_keys` and the run's trips**: Parse findings have no trip and coverage findings have
  no ride; would guess. Rejected for `FindingSubject`.
- **Fix `is_imminent` in the engine now**: Changes DEST-004 severity, an engine rule change. Recorded as an owner
  follow-up instead.

## Implementation

One PR, branch `feat/notification-service`, in this order:

1. `FindingSubject`, `make_finding(subject=...)` and every emitter in the D7 table; `UNREGISTERED_PREFIX` moved to
   `domain/models.py`. All existing tests pass unchanged.
2. `RunOutcome.trigger` and `RunOutcome.ride_starts`, set by `AssessmentService.run` (completed: the accepted trips'
   starts; failed: the stored snapshot's starts when the history was read, else None).
3. Migration `0006_notification_state`, `db/schema.py` mirror, `db/notifications.py`, the retention purge.
4. `domain/notification_policy.py` and `domain/notification_text.py` (pure).
5. `services/notifications.py` (`NotificationService`, `DispatchReport`).
6. The doc corrections below.

### Follow-up edits

The implementation PR makes these; this ADR does not.

- `src/family_logistics/adapters/apprise/__init__.py`: replace "In shadow mode only the Sunday report goes to Morgan,
  and nothing goes to Riley" with D1 (every push to the shadow tag, routed by `services.notifications`).
- `src/family_logistics/core/config.py`: the `notification_mode` docstring (same stale sentence); the tag docstrings
  (`apprise_tag_report` "weekly run summary", `apprise_tag_warning` "non-urgent finding and could-not-verify pushes",
  `apprise_tag_urgent` "critical or unverifiable imminent ride; bypasses quiet hours"); keep `FIELD_STATUS` at `M3` for
  quiet hours and tags until the scheduler PR first calls `dispatch`.
- `src/family_logistics/services/notifications.py` and `services/scheduler.py` docstrings: point at this ADR; the
  scheduler docstring says the scheduler calls `dispatch` and `report_ops` rather than `ops_alert` directly.
- `docs/runbook.md`, "Notification modes": shadow row as D1, production row naming the six tags and quiet hours;
  "Ops alerting": ops alerts go through `NotificationService` (held in quiet hours, failed runs and failing channels),
  incomplete runs raise none.
- `docs/guides/configuration.md`: the tag rows as in the D3 table (`fl-report` weekly summary, `fl-warning`,
  `fl-urgent`), quiet hours "urgent pushes bypass".
- `docs/privacy/privacy-and-retention-spec.md`: section 4, a line under the table that pushes are rendered from
  templates (ADR 0008), never from finding text, and carry no place names in v1; section 5, the masking example as
  rendered (`Fri 30 Oct 15:15`); section 7, a row for `notification_state` (90 days, closed rows).
- `docs/ADRs/0001-deterministic-authority-and-llm-boundary.md`: amend "Notifications carry engine counts and statuses
  only" to "Notifications carry engine-decided findings, counts and statuses, rendered from templates (ADR 0008)".
- `docs/project/roadmap.md`: the M3 notification row points at ADR 0008 and lists the PR; add the C3 owner item
  (engine imminence lower bound and DEST-004) to the owner follow-ups.
- `docs/ADRs/README.md`: status of 0008 once approved.

## Test Plan

Names are indicative; each assertion is required. "Payload" means the JSON posted to the mock Apprise transport.

### Policy, `tests/unit/test_notification_policy.py`

- **T-pol-1** `test_is_imminent_bounds`: True at `due_at == now` and `now + window`; False at `now + window + 1s` and
  `now - 1s`
- **T-pol-2** `test_quiet_hours_wrap_midnight`: 22:00 to 06:00: 21:59 out, 22:00 in, 03:00 in, 05:59 in, 06:00 out
- **T-pol-3** `test_quiet_hours_same_day_window`: 13:00 to 15:00 behaves without wrap
- **T-pol-4** `test_quiet_hours_spring_forward`: 2027-03-14 `America/Los_Angeles`, boundary 02:30: no exception; in from
  03:00 local
- **T-pol-5** `test_quiet_hours_fall_back`: 2026-11-01: both instants of 01:30 local are in a 01:00 to 02:00 window;
  22:00 to 06:00 lasts 9 h
- **T-pol-6** `test_severity_kind_tag_table`: Parametrized over the D3 per-finding table, in both modes
- **T-pol-7** `test_warning_on_imminent_ride_is_notice`: Warning, imminent: `notice`, `fl-warning`
- **T-pol-8** `test_finding_without_due_at_is_run_wide`: Imminent iff a ride start is imminent
- **T-pol-9** `test_unknown_ride_set_counts_as_imminent`: `ride_starts=None`: failed run plans `urgent`
- **T-pol-10** `test_only_urgent_and_reminder_bypass_quiet_hours`: Every `PushKind`
- **T-pol-11** `test_cooldown_holds_regressed_notice_not_urgent`: Within 4 h: `COOLDOWN` for notice, `SEND` for urgent
- **T-pol-12** `test_reminder_interval_and_limit`: Due after 60 min, at most 2, stops when not imminent, critical only
- **T-pol-13** `test_severity_escalation_reowes_and_downgrade_does_not`: Warning then critical: `SEND`; critical then
  warning: `NOT_DUE`
- **T-pol-14** `test_run_wide_urgent_rearms_after_window`: `last_sent_at` older than the window and a ride imminent:
  `SEND`
- **T-pol-15** `test_failed_run_plans_no_finding_pushes_and_closes_nothing`: Only `run-failed` and `ops:run_failed`;
  `completed` False
- **T-pol-16** `test_status_and_report_by_trigger`: The D3 run-level table, every status and trigger
- **T-pol-17** `test_stale_state_is_skipped`: Row newer than the outcome: `STALE`

### Rendering, `tests/unit/test_notification_text.py`

- **T-text-1** `test_every_rule_has_a_template`: `set(RULE_TEMPLATES) == set(RULES)`; placeholders only from the allowed
  four
- **T-text-2** `test_no_free_text_reaches_a_payload`: Hypothesis over every rule id, severity, lifecycle, kind and mode:
  sentinels in `message`, `suggestion`, `entity_keys`, `pickup`, `Destination.raw`, `free_notes`, location names,
  addresses, calendar id and UID never appear in title or body
- **T-text-3** `test_external_child_rendered_as_initial`: `E.` present, the full first name absent
- **T-text-4** `test_unregistered_and_unknown_ids_are_generic`: `a passenger`, `the driver`; `FindingSubject` refuses
  `unregistered:`
- **T-text-5** `test_names_are_filtered`: Digits, punctuation and control characters removed; at most 30 characters
- **T-text-6** `test_non_clear_pushes_never_use_clear_wording`: Regex of D3 never matches outside a `clear` status or
  report
- **T-text-7** `test_shadow_header_names_production_tag`: `[shadow]` title prefix; first line names the intended tag
- **T-text-8** `test_lengths_within_client_limits`: Title at most 120, body at most 2000, chunk of 8 at maximum name
  lengths
- **T-text-9** `test_data_011_source_map_is_closed`: Known keys map; any other key renders `a data source`
- **T-text-10** `test_failed_run_text_has_no_error_text`: `AssessmentResult.error` sentinel absent

### Service, `tests/unit/test_notification_service.py`

These run on SQLite at head with `httpx.MockTransport` and a fixed clock.

- **T-svc-1** `test_failed_run_sends_could_not_verify_never_clear`: `fl-warning` (`fl-urgent` with an imminent ride); no
  `fl-status` or `fl-report`; wording rule
- **T-svc-2** `test_incomplete_daily_run_never_reported_clear`: Summary to `fl-warning`; no `fl-status`
- **T-svc-3** `test_imminent_critical_bypasses_quiet_hours`: 23:30: one `fl-urgent` payload
- **T-svc-4** `test_held_notice_sent_after_quiet_hours`: 23:30 nothing posted, no delivered row; 06:10 dispatch posts
  once
- **T-svc-5** `test_same_run_dispatched_twice_sends_once`: One payload; second report counts duplicates
- **T-svc-6** `test_concurrent_double_dispatch_is_idempotent`: Two threads on one outcome: one payload per key; other
  `in_flight`
- **T-svc-7** `test_failed_delivery_retried_next_dispatch`: 500 then 200: one delivered row, two attempts, one success
- **T-svc-8** `test_failing_channel_raises_ops_alert_once`: Three failures on `fl-warning`: one `fl-ops` payload; reset
  after success
- **T-svc-9** `test_ops_unreachable_is_logged_not_sent`: `fl-ops` failing: no recursion, one error event
- **T-svc-10** `test_shadow_mode_routes_everything_to_shadow_tag`: Every kind including ops: payload tags equal
  `{"fl-shadow"}`
- **T-svc-11** `test_tags_never_outside_the_six`: Production, all kinds: tags within the configured six; DB CHECK
  refuses others
- **T-svc-12** `test_resolved_closes_state_and_recurrence_notifies`: Resolve then regress after the cooldown: pushed
  again
- **T-svc-13** `test_ongoing_not_renotified`: Three runs: one payload
- **T-svc-14** `test_mode_switch_renotifies_open_issues`: Shadow delivered, then production: pushed once to the
  production tag
- **T-svc-15** `test_logs_carry_no_text_or_names`: Captured events hold no sentinel, title or body
- **T-svc-16** `test_crash_after_send_resends_after_claim_ttl`: Completion skipped: no resend before `CLAIM_TTL`, one
  resend after
- **T-svc-17** `test_dispatch_never_raises_on_delivery_failure`: Transport errors and non-200s: report, no exception
- **T-svc-18** `test_unconfigured_client_writes_nothing`: No rows; `unconfigured` True
- **T-svc-19** `test_audit_per_delivered_push`: With key: one row per delivered push, `object_ref` a subject key; none
  for held or failed
- **T-svc-20** `test_no_audit_without_key`: No row, no error
- **T-svc-21** `test_stale_outcome_cannot_reopen_newer_state`: Older outcome after newer: no change, no payload
- **T-svc-22** `test_notices_are_chunked`: 20 owed notices: 3 payloads (8, 8, 4), each item delivered once
- **T-svc-23** `test_report_ops_opens_and_closes_episode`: `active=True` sends once; `active=False` closes; reopen sends
  again

### Engine, migration and retention

- **T-eng-1** `test_every_finding_has_a_subject` (acceptance matrix): Every finding except DATA-011 has `due_at` or
  `day`
- **T-eng-2** `test_subject_does_not_change_identity`: `issue_id`, equality and severity unchanged with and without a
  subject
- **T-eng-3** `test_run_outcome_carries_trigger_and_ride_starts`: Completed, failed with history, failed without history
  (None)
- **T-eng-4** `test_engine_imminence_unchanged`: Pins today's DEST-004 behaviour for a started ride (C3 follow-up)
- **T-db-1** `test_migration_0006_upgrade_and_downgrade`: Tables, indexes and every named CHECK
- **T-db-2** `test_schema_mirror_matches_migration`: `db/schema.py` parity, as for the pilot-core tables
- **T-db-3** `test_purge_expired_notification_state`: Closed rows older than 90 days deleted, open kept, idempotent,
  naive `now` refused

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

**Last Updated**: 2026-10-26
**Reviewed By**: pending
**Next Review**: before `FL_NOTIFICATION_MODE=production`
