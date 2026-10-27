---
title: "Roadmap"
schema_type: common
status: published
owner: core-maintainer
purpose: "Milestone roadmap for Family Logistics."
tags:
  - project
  - roadmap
---

Family Logistics is delivered in milestones. Each milestone has an exit gate;
transportation does not wait on later phases such as meal planning. This page
is the single source of truth for the plan: where another document names a
milestone for a piece of work, this page records it.

| Milestone | Scope | Exit gate |
|-----------|-------|-----------|
| M0 Discovery | Read-only CalDAV probe, sanitized fixtures, Home Assistant inventory, iOS Shortcuts protocol, egress, single sign-on, notification and routing checks ([M0 kit](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/tools/m0/README.md)) | All eight checks recorded (see [M0 closure](#m0-closure)) |
| M1 Ingestion and parser | CalDAV collector, normalized schema, registry, migrations, notes parser | Fixtures pass; zero write calls to iCloud |
| M2 Timeline and engine | School overlay rules, location continuity, external obligations, driver conflicts, issue lifecycle | Acceptance matrix passes |
| M3 Routing, scheduler, notifications ([GitHub milestone](https://github.com/ByronWilliamsCPA/family-logistics/milestone/1)) | Home Assistant school snapshot ingest, route providers, Apprise channels, deduplication, weekly and daily scheduling, retention jobs, ops alerts | 7 consecutive days in shadow mode with zero false-clear runs, and every FAILED or INCOMPLETE run in that period explained in the runbook |
| M4 Home Assistant UI, MVP ([GitHub milestone](https://github.com/ByronWilliamsCPA/family-logistics/milestone/2)) | Status API, family-wide validation card, registry UI, on-demand runs | 2 consecutive weeks in which neither parent creates, changes or cancels a ride commitment anywhere but the shared calendar (registry edits of people, places and vehicles do not count) |
| M5 LLM and hardening | Narrative explanations, privacy controls, failure injection, backup and restore drills | Deterministic findings unaffected by malformed LLM output |
| M6 Meals (phase 2) | Meal rotation, cooking windows, shopping review | Estimated separately |

A **false-clear run** is a run reported CLEAR when a required source was stale
or failed, or when a parent's review of that day finds a ride problem the run should have raised. Design rule 4 forbids it; the
M3 gate requires that none occurs.

The Home Assistant school snapshot is M3 scope (decided 2026-10-25, closing
[#25](https://github.com/ByronWilliamsCPA/family-logistics/issues/25)). M2
delivered the school overlay rules; its acceptance matrix runs on synthetic
school days. The M3 snapshot ingest is merged (#27 to #29), but no run reads
a real snapshot until the M3 scheduler passes one in.

## Status

Status as of 2026-10-26: **between M2 and M3.** The
M1 ingestion half and the M2 engine, with the assessment orchestrator (#17) and
the missing-snapshot fix (#26), are merged, and M3 work has started: the Home
Assistant school snapshot ingest (#27 to #29) and parts 1 and 2 of 3 of the
routing work (#30, #32) are merged (see [M3 progress](#m3-progress)). None of
the M0, M1 or M2 gates is formally closed (see Step 1), and the M3 gate has not
started. `collect --once` and three token-protected Home Assistant routes under
`serve` are the only wired production paths: the snapshot ingest
(`POST /api/v1/ingest/ha-snapshot`) and the route relay pair
(`GET /api/v1/route-requests`, `POST /api/v1/route-results`). None of them runs
an assessment: `AssessmentService.run` has no production caller yet, so no CLI
command, API route or scheduler job runs one. No service calls the route
provider factory yet either, so nothing queues a route request and no run reads
the route cache.

Done so far: M0 discovery code (domain models, the Template v1 notes parser,
occurrence-bound confirmation codes, passenger-level change impact, run
status, capacity, constrained stop ordering, address encryption at rest and
the read-only CalDAV transport), the M1 ingestion half (read-only CalDAV
collector, recurrence expansion, source persistence and the `collect` CLI) and
the M2 engine (location timeline, coverage, driver and feasibility rules,
finding lifecycle and `assess()`). The assessment orchestrator
(`services/assessments.py`, `AssessmentService`) is also merged: it reads the
already-collected calendar rows, judges source freshness, runs
`domain.engine.assess()` and stores the run, its findings and the previous
run's rides. Collection stays in `services/collector.py`. Timeline, rules,
change impact and run status all run inside `assess()`. School overlays apply
only when a `SchoolSnapshot` is supplied. The Home Assistant ingest route
(M3, #27 to #29) now stores one, but no production caller of `run` reads it
yet, so a run without a snapshot still ends non-CLEAR (DATA-011, #26).

Also in the tree as groundwork for later milestones: the SQLite schema with
Alembic migrations, an append-only audit log, the pilot registry and seed
loader, the Apprise client and `ops_alert()` helper (not yet called by any
scheduler; channel wiring and scheduling remain M3), structured logging, and
startup checks (`verify-db`, `healthcheck`).

Merged: compliance alignment, ops foundations and public-repo data guards (#2),
pilot contracts and core schema (#4), registry and seed loader (#5), the engine
(#6), the collector (#7), review fixes (#13, #14), the assessment orchestrator
(#17), the Path to MVP section of this page (#18), the missing-snapshot fix
(#26), the Home Assistant school snapshot ingest (#27, #28, #29), the null
route provider and provider factory (#30), the `ha_relay` provider, route cache
and Home Assistant relay routes (#32) and dependency updates
(#8, #9, #10, #11, #15, #16, #31).

### M0 closure

The eight M0 checks are run by the owner. Detailed results stay in the private
deployment repository, because they may hold personal data; only pass or fail
is recorded here.

| # | Check | Status |
|---|-------|--------|
| 1 | Dedicated Apple ID, View Only CalDAV access | Awaiting owner attestation |
| 2 | Calendar inventory, recurrence and notes behavior, sanitized fixtures | Awaiting owner attestation |
| 3 | Home Assistant school schedules (including bell schedules) and location entities | Awaiting owner attestation |
| 4 | iOS Shortcut ride creation and `CONFIRM:` writing | Awaiting owner attestation |
| 5 | Docker host egress | Awaiting owner attestation |
| 6 | Single sign-on accounts and `family-logistics` group | Awaiting owner attestation |
| 7 | Apprise routing and recipients | Awaiting owner attestation |
| 8 | Routing provider credentials and budget enforcement | Awaiting owner attestation |

## Path to MVP

MVP is the M4 exit gate: 2 consecutive weeks in which neither parent creates,
changes or cancels a ride commitment anywhere but the shared calendar.
Notifications are still in shadow mode at MVP; the M3 exit gate is 7
consecutive days in shadow mode with zero false-clear runs. Switching
notifications to live is a separate go-live step after MVP (Step 3). M5 (LLM
narrative and hardening) and M6 (meals) follow MVP, except for the two M5 items
that must land before go-live, listed in Step 3.

### Step 1: Merged M1 and M2 work, and open gate items

| PR | State | What it completed |
|----|-------|-------------------|
| #7 `feat/m1-collector` | Merged | M1 ingestion half: read-only CalDAV collector, recurrence expansion, source persistence, `collect` CLI |
| #6 `feat/m2-engine` | Merged | M2 engine: location timeline, coverage, driver and feasibility rules, finding lifecycle, `assess()` |
| #17 `feat(assessments)` | Merged | Assessment orchestrator: collected rows to parsed rides, `assess()` and a stored run; run and finding persistence; the previous run's rides persisted (migration `0003_trip_snapshot`) so PASS-003 and CONF-008 work across runs. A run that raises is stored as failed and never reads as clear. |
| #18 `docs(roadmap)` | Merged | The Path to MVP section of this page |
| #26 `fix(assessments)` | Merged | A run without a school snapshot is INCOMPLETE (DATA-011) instead of silently skipping school checks. |
| #8, #9, #10, #11, #15, #16, #31 Renovate | Merged | Dependency hygiene (base image digest, GitHub Actions, pre-commit hooks, validate-pyproject, the lockfile). #11 is a major bump of `pre-commit-hooks` (v6 drops two hooks) and `conventional-pre-commit` (v4 skips merge commits by default). |

Open items that block the M0, M1 and M2 exit gates:

- **M0 gate (all eight checks recorded):** results are kept outside Git
  (`tools/m0/README.md`); the [M0 closure](#m0-closure) table records each
  check's pass or fail once the owner attests it. Checks 3 (Home Assistant
  inventory), 7 (Apprise routing) and 8 (routing provider) feed the M3 work
  below.
- **M1 gate (zero write calls to iCloud, fixtures pass):** the code is merged but
  has not run against live iCloud. M0 checks 1 and 2 must confirm the partition
  host, sync tokens, `writable=no` on View Only calendars, stable calendar IDs,
  an intact DESCRIPTION, the RECURRENCE-ID and TZID formats, and how
  cancellations arrive.
- **M1 carry-over, `FL_DATA_KEY` re-key command:** the [runbook](../runbook.md)
  (`FL_DATA_KEY` rotation) records it as not yet delivered, so the key must not
  be rotated yet. The registry is merged and stores Fernet-encrypted addresses
  and coordinates; the command, which must re-encrypt and verify both, is not.
- **M2 gate (acceptance matrix passes):** the matrix runs except two skipped
  tests in `tests/scenarios/test_acceptance_matrix.py`: `test_llm_unavailable`
  (reason M5) and `test_on_demand_idempotent` (reason M4).
- **Owner review of the engine defaults:** merge gap, travel and stop buffers,
  pickup tolerance, unknown-capacity threshold, bus and school rules, and
  DEST-004 severity. Verify registry addresses before the shadow pilot.

### M3 progress

Merged M3 work, and the scheduler PR in review. Until a deployment sets
`FL_SCHEDULER_ENABLED=true`, none of it is read by a run, because nothing else
calls `AssessmentService.run`.

| PR | State | What it completed |
|----|-------|-------------------|
| #27 `feat(db)` | Merged | Home Assistant school snapshot storage, ingest 1 of 3: `SchoolSnapshot` moved to `domain/events.py`, migration `0004_school_snapshot` (`school_snapshot` freshness row and parsed `school_day` rows; no raw payload, event text or zone column), `SchoolSnapshotRepository` with an atomic, audited replace, and the `purge_expired_school_days` retention function (no caller until the scheduler). |
| #28 `feat(ha)` | Merged | Ingest 2 of 3: the `FL_HA_SCHOOLS` setting, the strict snapshot payload schema (`adapters/home_assistant/schemas.py`) and the deterministic mapping from school calendar events to school days (`adapters/home_assistant/school_days.py`). |
| #29 `feat(api)` | Merged | Ingest 3 of 3: `POST /api/v1/ingest/ha-snapshot` on the token-protected Home Assistant router, wired in `create_app` (`services/school_snapshot.py`). The snapshot is stored, but no run reads it yet, so runs stay INCOMPLETE (DATA-011). Operator guide: [Home Assistant school snapshot](../guides/ha-school-snapshot.md). |
| #30 `feat(routing)` | Merged | Routing 1 of 3 ([ADR 0006](../ADRs/0006-route-provider-abstraction.md)): `NullRouteProvider` and the `build_route_provider` factory. Until #32, the default `ha_relay` setting mapped to the Null provider, so every estimate was unknown and any ride that needed one was incomplete (ROUTE-010). |
| #32 `feat(routing)` | Merged | Routing 2 of 3: the `ha_relay` provider; the `route_cache` and `route_request` tables (migration `0005`); `GET /api/v1/route-requests` and `POST /api/v1/route-results` (defined in `api/route_relay.py`, registered on the token-protected Home Assistant router), which send zone ids and withhold another family's zone-less home through the address-release gate (privacy spec section 5); the `purge_expired_route_cache` and `purge_expired_route_requests` retention functions (no caller until the scheduler); operator guide: [Home Assistant route relay](../guides/ha-route-relay.md). The default `ha_relay` setting yields the real provider when the factory is given the database engine, but no service calls the factory yet, so routing has no effect on assessments. Its next-occurrence assumption about Home Assistant's `departure_time` was observed on HA Core 2026.10.x on 2026-10-26 but is not a documented guarantee, so it is re-checked after each Home Assistant upgrade (the 08:00 and 15:00 pair; ADR 0006), and shadow mode stays until the time zone, first-cycle and other-families checklist items are done. |
| Scheduler `feat(services)` | In review | The scheduler (`services/scheduler.py`) behind `FL_SCHEDULER_ENABLED` (off by default): scan, daily and weekly pipelines (collect, `AssessmentService.run` with the latest school snapshot, `NotificationService.dispatch`, or `dispatch_unavailable` when a run raises, a daily, weekly or retry tick waits out the 30-minute lock wait or a daily or weekly fire is missed), the nightly retention purges (raw source text, school days, route cache, route requests, notification state), the one extra urgent dispatch after `urgent_failed`, one pipeline at a time per process, the scan-interval and quiet-hours cron checks, and the route provider built through the factory. Runbook: [Scheduler](../runbook.md#scheduler). Not built here: `google_routes`, ops alerts for collection failures, a degraded database and the routing budget, the audit and external-household purges, on-demand coalescing (M4), and the shadow soak itself. |

### M3 blockers

The #17 review left five issues, all assigned to the
[M3 milestone](https://github.com/ByronWilliamsCPA/family-logistics/milestone/1).
Each must close before the scheduler becomes a production caller of
`AssessmentService.run` and before the M3 shadow period counts toward the gate.

| Issue | Blocker | Note |
|-------|---------|------|
| [#19](https://github.com/ByronWilliamsCPA/family-logistics/issues/19) | Retain confirmed legs across runs | The trip snapshot holds only the previous run, so PASS-003 fires once and then resolves. The [privacy spec](../privacy/privacy-and-retention-spec.md) (section 7) bounds confirmed legs at 90 days after the trip date. |
| [#20](https://github.com/ByronWilliamsCPA/family-logistics/issues/20) | What the trip snapshot and finding text may store | The snapshot payload and finding messages can hold ride text in plain form; the privacy spec does not list the trip snapshot yet. |
| [#21](https://github.com/ByronWilliamsCPA/family-logistics/issues/21) | An INCOMPLETE run must not resolve findings or replace the snapshot | The guard must key on input trust (every required source usable and no DATA-011 finding), not on the run status label: `compute_run_status` returns ATTENTION_REQUIRED before it checks source freshness when a critical finding exists (`domain/run_status.py:85-89`), so a run on stale input can carry a non-INCOMPLETE label. |
| [#22](https://github.com/ByronWilliamsCPA/family-logistics/issues/22) | Run serialization | One write lock must span history load through run completion; today `begin_run`, the history load and `complete_run` are separate transactions, so overlapping runs can overwrite each other. The scheduler PR adds an overlapping-run test. |
| [#23](https://github.com/ByronWilliamsCPA/family-logistics/issues/23) | Corrupt snapshot healing | A corrupt or unknown-version snapshot row makes every run FAILED; define the healing policy and a runbook recovery step. Likely settled together with #19. |

### Step 2: Remaining work (M3 and M4)

| Milestone | PR | Replaces | Completes |
|-----------|----|----------|-----------|
| M3 | Routing provider and budget (part 3 of 3, and wiring; parts 1 and 2 merged in #30 and #32, [ADR 0006](../ADRs/0006-route-provider-abstraction.md)) | `adapters/routing` base, `NullRouteProvider` and the `build_route_provider` factory (#30); the `ha_relay` provider, route cache and relay routes (#32) | Part 3: optional `google_routes` with the monthly budget counter and warning. Wiring: the scheduler builds the provider with `build_route_provider(settings, engine=engine)`, so the default `ha_relay` setting yields the real provider once the scheduler is enabled; the M4 on-demand API must do the same. Unknown travel time stays incomplete (ROUTE-010) throughout. |
| M3 | Notification service (six PRs, [ADR 0008](../ADRs/0008-notification-service.md)) | `services/notifications.py` stub | Policy, rendering, state storage and the `NotificationService` (`dispatch` and `dispatch_unavailable`) with shadow routing of every push to `fl-shadow`, de-duplication per subject, quiet hours with one urgent push per episode, imminent-ride windows, and dedicated `fl-*` tags only, including the weekly report on `fl-report` (`FL_APPRISE_TAG_REPORT` in [configuration](../guides/configuration.md)). The scheduler (built, opt-in) calls it. |
| M3 | Scheduler and ops alerts | `services/scheduler.py` stub | **Built (in review, opt-in via `FL_SCHEDULER_ENABLED`):** weekly, daily and incremental jobs, `NotificationService.dispatch` after each run and `dispatch_unavailable` when a run raises (incomplete runs alert once per episode, ADR 0008 D3), the route provider built through the factory, and the retention purges that exist. **Not built:** on-demand coalescing (the M4 API takes the scheduler's `pipeline_lock`), `ops_alert()` for collection failures, a degraded database and the routing budget. Becomes the first production caller of `AssessmentService.run`, so the [M3 blockers](#m3-blockers) must close first, and it must pass the latest school snapshot into every run (see below). Includes the retention jobs below. **Gate: 7 consecutive days in shadow mode with zero false-clear runs, every FAILED or INCOMPLETE run explained in the runbook.** |
| M4 | Status and assessment API | `api/ha.py` (token-protected router carrying the snapshot ingest and, from `api/route_relay.py`, the two route relay routes) | `GET /api/v1/status`, `POST /api/v1/assessments`, `GET /api/v1/assessments/{run_id}`. Un-skips `test_on_demand_idempotent`. |
| M4 | Family validation card and registry UI | none | The card and registry editing, including the export and hard-delete actions and the single sign-on access control listed below. **Gate: 2 consecutive weeks in which neither parent creates, changes or cancels a ride commitment anywhere but the shared calendar (registry edits of people, places and vehicles do not count).** |
| M4 | "Confirm Ride" iOS Shortcut | none | The Shortcut that writes the `CONFIRM:` code ([ADR 0002](../ADRs/0002-event-contract-and-confirmation-code.md), Implementation Target M4). |

Further M3 scope, each an explicit item:

- **Collector success after apply:** `services/collector.py:438-441` records a
  calendar's success before its events are applied. Record success only after
  the events are stored, so a failed apply cannot leave a source looking fresh.
- **Bounded processing of untrusted calendar input:** a cap on recurrence
  occurrences per event and per run, where reaching the cap yields an
  INCOMPLETE finding rather than silent truncation; a linear-time replacement
  for the parser's explicit-identifier pattern; and a size cap on CalDAV responses.
- **Retention jobs on the scheduler:** the privacy spec (section 7) defers
  these purges to the M3 scheduler: raw VEVENT text after 30 days
  (`purge_raw_source_text`, spec line 180), audit rows older than 365 days
  (spec line 183) and inactive external passengers and households after 180
  days (spec line 186). The school-day purge that #27 added
  (`purge_expired_school_days`, 30 days, proposed, spec line 181) and the two
  route purges that #32 added (`purge_expired_route_cache`, 28 days after the
  last observation, spec line 187; `purge_expired_route_requests`, 8 days after
  queueing, spec line 188) are deferred the same way and need the same daily
  job. The scheduler's nightly purge runs the five that exist (raw source
  text, school days, route cache, route requests, notification state); the audit
  and inactive external passenger and household purges have no function yet.
- **Log redaction:** enforce the privacy spec section 4 "App logs" column
  (external children by ID only, no addresses) in the logging pipeline, not
  only by convention at each call site.
- **Ops alerting:** `ops_alert()` wired as listed in the scheduler row (not yet). Until
  then the [runbook](../runbook.md) ("Ops alerting") relies on the external
  monitor of `/healthz`, and a healthy process does not mean the last
  assessment succeeded.

Further M4 scope, from the [privacy spec](../privacy/privacy-and-retention-spec.md):

- **Registry export and hard-delete** (section 8): a per-household summary for
  a parent to share, and a hard-delete of a household, its passengers, its
  protected address data, route cache entries and future legs. Both audited and
  admin-only. The spec places them in the registry UI, which is M4.
- **Registry UI access control** (section 9): behind the deployment's single
  sign-on proxy, limited to the `family-logistics` group. The matching settings
  (`FL_PROXY_TRUSTED_CIDRS`, `FL_UI_ALLOWED_USERS`) are marked M4 in
  `core/config.py`.

The scheduler (M3) and the on-demand API (M4) must call
`AssessmentService.run(trigger, school=repository.latest())`, where `repository` is a
`SchoolSnapshotRepository`.
Until they do, runs report INCOMPLETE (DATA-011), even though the snapshot
ingest has merged (#27 to #29).

### Step 3: Go live (outside this repository)

1. Deploy the attested GHCR image with the private deployment repository's
   registry seed, Apprise tags and Home Assistant configuration.
2. Run shadow mode at least until the M3 gate is met (7 consecutive days with
   zero false-clear runs) and through MVP; compare findings with what the
   parents know, and tune rules and thresholds.
3. Before notifications switch to live, two M5 items must have landed: one
   backup and restore drill, and one failure-injection pass over the "never
   clear on failure" path. The rest of M5 (LLM narrative explanations and the
   remaining hardening) does not gate go-live. M5 also owns privacy controls:
   owner decision needed on which of them must land before notifications about
   children go live.
4. Switch notifications from shadow to live (`FL_NOTIFICATION_MODE=production`)
   only with the joint approval of both parents after the shadow period,
   recorded in the private deployment repository (runbook, "Notification
   modes"). The household must also have approved the defaults (README).

### Step 4: After go-live

- **M5:** LLM narrative explanations, privacy controls, failure injection and
  backup and restore drills in full. Un-skips `test_llm_unavailable`.
- **Operations:** a restore test at least monthly
  ([ADR 0005](../ADRs/0005-sqlite-wal-single-writer.md); runbook, "Backup and
  restore").
- **M6:** meal rotation, cooking windows and shopping review.

## Unscheduled

Items that a document names but no source places in a milestone. Each needs an
owner decision before it is scheduled.

- **Purges for run logs and resolved findings:** the privacy spec (section 7,
  lines 182 and 185) sets 90-day retention for analysis run logs and report
  artifacts and for resolved findings, but names no milestone. They would
  naturally run with the M3 retention jobs.
- **Interim deletion of external-family data (owner decision):** the privacy
  spec (section 8, lines 201-224) promises each external family that its
  child's data is deleted on request, and places the export and hard-delete
  actions in the registry UI, which is M4. The M3 shadow period already holds
  external-family data. Decide whether an interim manual deletion procedure
  (runbook step, audited) is needed in M3.
- **PR #17 lint suppressions**
  ([#24](https://github.com/ByronWilliamsCPA/family-logistics/issues/24)): fix
  or formally accept the suppressions that lack a tracking reference. Blocks
  no milestone.
- **Registry surface:** the roadmap places registry editing in Home Assistant
  (M4), while the privacy spec (section 9) and ADR 0002 describe a registry UI
  or web UI behind the single sign-on proxy. Whether these are one surface or
  two is not decided.
