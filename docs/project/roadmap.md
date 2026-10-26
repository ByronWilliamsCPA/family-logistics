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
transportation does not wait on later phases such as meal planning.

| Milestone | Scope | Exit gate |
|-----------|-------|-----------|
| M0 Discovery | Read-only CalDAV probe, sanitized fixtures, Home Assistant inventory, iOS Shortcuts protocol, egress, single sign-on, notification and routing checks ([M0 kit](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/tools/m0/README.md)) | All eight checks recorded |
| M1 Ingestion and parser | CalDAV collector, normalized schema, registry, migrations, notes parser | Fixtures pass; zero write calls to iCloud |
| M2 Timeline and engine | School overlays, location continuity, external obligations, driver conflicts, issue lifecycle | Acceptance matrix passes |
| M3 Routing and notifications | Route providers, Apprise channels, deduplication, weekly and daily scheduling, ops alerts | Live dry-run reports in shadow mode |
| M4 Home Assistant UI | Status API, family-wide validation card, registry UI, on-demand runs | Both parents use the calendar as the only ride editor |
| M5 LLM and hardening | Narrative explanations, privacy controls, failure injection, backup and restore drills | Deterministic findings unaffected by malformed LLM output |
| M6 Meals (phase 2) | Meal rotation, cooking windows, shopping review | Estimated separately |

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
stores one, but no production caller of `run` reads it yet, so a run without a
snapshot ends non-CLEAR (DATA-011). `AssessmentService.run` has no production
caller yet: no CLI command, API route or scheduler job invokes it.

Also in the tree as groundwork for later milestones: the SQLite schema with
Alembic migrations, an append-only audit log, the pilot registry and seed
loader, the Apprise client and `ops_alert()` helper (not yet called by any
scheduler; channel wiring and scheduling remain M3), structured logging, and
startup checks (`verify-db`, `healthcheck`).

## Path to MVP

MVP is the M4 exit gate: both parents use the calendar as the only ride
editor. Notifications are still in shadow mode at MVP; shadow mode is the M3
exit gate (live dry-run reports in shadow mode). Switching notifications to
live is a separate go-live step after MVP (Step 3). M5 (LLM narrative and
hardening) and M6 (meals) follow MVP, except for the two M5 items that must land
before go-live, listed in Step 3.

Status as of main at 57a6146 (2026-10-25). Merged: compliance alignment, ops
foundations and public-repo data guards (#2), pilot contracts and core schema
(#4), registry and seed loader (#5), the engine (#6), the collector (#7),
review fixes (#13, #14), the assessment orchestrator (#17), this roadmap (#18),
the fix that makes a missing school snapshot incomplete (#26) and dependency
updates (#8, #9, #10, #11, #16).

### Step 1: Merged M1 and M2 work, and open gate items

| PR | State | What it completed |
|----|-------|-------------------|
| #7 `feat/m1-collector` | Merged | M1 ingestion half: read-only CalDAV collector, recurrence expansion, source persistence, `collect` CLI |
| #6 `feat/m2-engine` | Merged | M2 engine: location timeline, coverage, driver and feasibility rules, finding lifecycle, `assess()` |
| #17 `feat(assessments)` | Merged | Assessment orchestrator: collected rows to parsed rides, `assess()` and a stored run; run and finding persistence; the previous run's rides persisted (migration `0003_trip_snapshot`) so PASS-003 and CONF-008 work across runs. A run that raises is stored as failed and never reads as clear. |
| #8, #9, #10, #11 Renovate | Merged | Dependency hygiene (base image digest, GitHub Actions, pre-commit hooks). #11 is a major bump of `pre-commit-hooks` (v6 drops two hooks) and `conventional-pre-commit` (v4 skips merge commits by default). |

Open items that block the M1 and M2 exit gates, or that must be closed before
the M3 shadow dry-run is trusted. None of these is a PR yet:

- **M1 gate (zero write calls to iCloud, fixtures pass):** the code is merged but
  has not run against live iCloud. M0 checks 1 and 2 must confirm the partition
  host, sync tokens, `writable=no` on View Only calendars, stable calendar IDs,
  an intact DESCRIPTION, the RECURRENCE-ID and TZID formats, and how
  cancellations arrive.
- **M0 gate (all eight checks recorded):** results are kept outside Git
  (`tools/m0/README.md`), so this repository cannot show which of the eight
  checks are recorded. Owner item: confirm all eight are recorded. Checks 3
  (Home Assistant inventory), 7 (Apprise routing) and 8 (routing provider) feed
  the M3 PRs below.
- **M2 gate (acceptance matrix passes):** the matrix runs except two skipped
  tests in `tests/scenarios/test_acceptance_matrix.py`: `test_llm_unavailable`
  (reason M5) and `test_on_demand_idempotent` (reason M4).
- **Owner review of the engine defaults:** merge gap, travel and stop buffers,
  pickup tolerance, unknown-capacity threshold, bus and school rules, and
  DEST-004 severity. Verify registry addresses before the shadow pilot.
- **Follow-ups #17 left open** (documented as RAD notes): snapshot retention
  against ADR 0004, INCOMPLETE run semantics, privacy posture of the stored ride
  text, run serialization, and the healing policy for a corrupt snapshot row.

### Step 2: Remaining PRs (M3 and M4)

| Milestone | PR | Replaces | Completes |
|-----------|----|----------|-----------|
| M3 (ADR 0007 target: M2) | Home Assistant adapter and snapshot ingest | `adapters/home_assistant` stub | School and location snapshot (clears the SCHOOL-009 warnings on every weekday), `POST /api/v1/ingest/ha-snapshot`. ADR 0007 targets M2, but the adapter is still a stub, so this is the first M3 PR: it must land before the shadow dry-run so school checks are not incomplete. In flight as three stacked PRs: storage, mapper, route. Merging them stores the snapshot but does not make any run read it. |
| M3 | Routing provider and budget | `adapters/routing` base only | `ha_relay`, the default provider in M3 (ADR 0006), with `GET /api/v1/route-requests` and `POST /api/v1/route-results` on `api/ha.py`; optionally `google_routes`; caching, budget warning. Unknown travel time stays incomplete (ROUTE-010). |
| M3 | Notification service | `services/notifications.py` stub | Finding rendering, deduplication, shadow-mode default, quiet hours, imminent-ride windows, dedicated `fl-*` tags only. |
| M3 | Scheduler and ops alerts | `services/scheduler.py` stub | Weekly, daily and incremental jobs, on-demand coalescing, `ops_alert()` wired to failed or incomplete runs, collection failures, a degraded database and the routing budget. Becomes the first production caller of `AssessmentService.run`, so it must also add a test for overlapping runs with the chosen serialization policy (see the `#CRITICAL` in `services/assessments.py`). **Gate: live dry-run reports in shadow mode.** |
| M4 | Status and assessment API | `api/ha.py` (auth scaffold only) | `GET /api/v1/status`, `POST /api/v1/assessments`, `GET /api/v1/assessments/{run_id}`. Un-skips `test_on_demand_idempotent`. |
| M4 | Family validation card and registry UI | none | The card and registry editing in Home Assistant. **Gate: both parents use the calendar as the only ride editor.** |

The scheduler (M3) and the on-demand API (M4) must call
`AssessmentService.run(trigger, school=SchoolSnapshotRepository.latest())`.
Until they do, runs report INCOMPLETE (DATA-011), even after the snapshot
ingest merges.

### Step 3: Go live (outside this repository)

1. Deploy the attested GHCR image with the private deployment repository's
   registry seed, Apprise tags and Home Assistant configuration.
2. Run shadow mode through at least the two-week shadow period (README), compare
   findings with what the parents know, and tune rules and thresholds.
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
- **M6:** meal rotation, cooking windows and shopping review.
