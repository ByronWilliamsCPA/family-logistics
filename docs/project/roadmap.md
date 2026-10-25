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
finding lifecycle and `assess()`). Also in the tree as groundwork for later
milestones: the SQLite schema with Alembic migrations, an append-only audit log,
the pilot registry and seed loader, the Apprise client and `ops_alert()` helper
(not yet called by any scheduler; channel wiring and scheduling remain M3),
structured logging, and startup checks (`verify-db`, `healthcheck`). The
assessment orchestrator that joins the collector to the engine is in review
(#17).

## Path to MVP

MVP is the M4 exit gate: both parents use the calendar as the only ride
editor, with findings running in shadow mode. M5 (LLM narrative and
hardening) and M6 (meals) follow MVP.

Status as of 2026-10-25. Merged: pilot contracts and core schema (#4),
registry and seed loader (#5), review fixes (#13, #14), the engine (#6), the
collector (#7) and Renovate updates (#8, #10, #11). The assessment
orchestrator is open as #17.

### Step 1: Close out M1 and M2

| PR | State | What it completed |
|----|-------|-------------------|
| #7 `feat/m1-collector` | Merged | M1 ingestion half: read-only CalDAV collector, recurrence expansion, source persistence, `collect` CLI |
| #6 `feat/m2-engine` | Merged | M2 engine: location timeline, coverage, driver and feasibility rules, finding lifecycle, `assess()` |
| #8 Renovate base image digest | Merged | Dependency hygiene |
| #10 Renovate pre-commit hooks (minor) | Merged | Dependency hygiene |
| #11 Renovate pre-commit hooks (major) | Merged | Dependency hygiene. Major bumps of `pre-commit-hooks` (v6 drops two hooks) and `conventional-pre-commit` (v4 skips merge commits by default). |

Open items that block the M1 and M2 exit gates, none of which is a PR yet:

- **M1 gate (zero write calls to iCloud, fixtures pass):** the code is merged but
  has not run against live iCloud. M0 checks 1 and 2 must confirm the partition
  host, sync tokens, `writable=no` on View Only calendars, stable calendar IDs,
  an intact DESCRIPTION, the RECURRENCE-ID and TZID formats, and how
  cancellations arrive.
- **M2 gate (acceptance matrix passes):** the matrix runs except scenario 18
  (M5, LLM) and scenario 20 (M4, on-demand idempotency).
- **Owner review of the engine defaults:** merge gap, travel and stop buffers,
  pickup tolerance, unknown-capacity threshold, bus and school rules, and
  DEST-004 severity. Verify registry addresses before the shadow pilot.

### Step 2: PRs still to be written

| Milestone | PR | Replaces | Completes |
|-----------|----|----------|-----------|
| M2/M3 bridge | Assessment orchestrator (#17, in review) | `services/assessments.py` stub | Collect, build timeline, apply school overlays, run the rules, compute change impact and run status, persist findings with lifecycle. A failed or incomplete run is never reported as clear. |
| M3 | Routing provider and budget | `adapters/routing` base only | At least one route provider, caching, budget warning. Unknown travel time stays incomplete (ROUTE-010). |
| M3 | Notification service | `services/notifications.py` stub | Finding rendering, deduplication, shadow-mode default, quiet hours, imminent-ride windows, dedicated `fl-*` tags only. |
| M3 | Scheduler and ops alerts | `services/scheduler.py` stub | Weekly, daily and incremental jobs, on-demand coalescing, `ops_alert()` wired to failed or incomplete runs, collection failures, a degraded database and the routing budget. **Gate: live dry-run reports in shadow mode.** |
| M4 | Home Assistant adapter and snapshot ingest | `adapters/home_assistant` stub | School and location snapshot (clears the SCHOOL-009 warnings on every weekday), `POST /api/v1/ingest/ha-snapshot`. |
| M4 | Status and assessment API | `api/ha.py` (auth scaffold only) | `GET /status`, `POST /assessments`, `GET /assessments/{run_id}`, route request and result routes. Closes acceptance scenario 20 (on-demand idempotency). |
| M4 | Family validation card and registry UI | none | The card and registry editing in Home Assistant. **Gate: both parents use the calendar as the only ride editor.** |

### Step 3: Go live (outside this repository)

1. Deploy the attested GHCR image with the private deployment repository's
   registry seed, Apprise tags and Home Assistant configuration.
2. Run shadow mode for several weeks, compare findings with what the parents
   know, and tune rules and thresholds.
3. Before switching notifications live, run one backup and restore drill and
   one failure-injection pass over the "never clear on failure" path.
4. Switch notifications from shadow to live.

### After MVP

- **M5:** LLM narrative explanations, privacy controls, failure injection and
  backup and restore drills in full. Closes acceptance scenario 18.
- **M6:** meal rotation, cooking windows and shopping review.
