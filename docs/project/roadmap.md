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

Done so far (M0): domain models, the Template v1 notes parser,
occurrence-bound confirmation codes, passenger-level change impact, run
status, capacity, constrained stop ordering, address encryption at rest and
the read-only CalDAV transport. Also in the tree as groundwork for later
milestones: the SQLite schema with Alembic migrations, an append-only audit log,
the Apprise client and `ops_alert()` helper (not yet called by any scheduler;
channel wiring and scheduling remain M3), structured logging, and startup
checks (`verify-db`, `healthcheck`).

## Path to MVP

MVP is the M4 exit gate: both parents use the calendar as the only ride
editor, with findings running in shadow mode. M5 (LLM narrative and
hardening) and M6 (meals) follow MVP.

Status as of 2026-10-25. Merged: pilot contracts and core schema (#4), registry
and seed loader (#5), review fixes (#13, #14). Open PRs that complete the
remaining M1 and M2 work are listed first, then the PRs still to be written.

### Step 1: Merge the open PRs

| Order | PR | Completes | Remaining before its milestone gate closes |
|-------|----|-----------|--------------------------------------------|
| 1 | #7 `feat/m1-collector`: read-only CalDAV collector, recurrence expansion, `collect` CLI | M1 ingestion half (collector, recurrence, source persistence) | Merge-conflict reconciliation with #5 (`config.py`, `.env.example`, guides, README). Live iCloud validation (M0 checks 1 and 2): partition host, ctag and sync tokens, `writable=no` for View Only calendars, stable calendar IDs, DESCRIPTION intact, RECURRENCE-ID and TZID formats, cancellations. |
| 2 | #6 `feat/m2-engine`: location timeline and deterministic rule engine | M2 engine (timeline, coverage, driver, feasibility, lifecycle, `assess()`) | Merge-conflict reconciliation with #5 (`config.py` `FIELD_STATUS`, `.env.example`, configuration guide). Owner review of the default thresholds (merge gap, buffers, capacity, bus and school rules, DEST-004). Acceptance scenarios 18 (M5) and 20 (M4) stay skipped. |
| 3 | #8, #10, #11 Renovate updates | Dependency hygiene only | #11 is a major pre-commit bump. Run `pre-commit run --all-files` before merging. |

Exit gates closed by Step 1: **M1** (fixtures pass, zero write calls to iCloud)
once #7 merges, and **M2** (acceptance matrix passes) once #6 merges, with
scenarios 18 and 20 deferred to the milestones that own them.

### Step 2: PRs still to be written

| Milestone | PR | Replaces | Completes |
|-----------|----|----------|-----------|
| M2/M3 bridge | Assessment orchestrator | `services/assessments.py` stub | Collect, build timeline, apply school overlays, run the rules, compute change impact and run status, persist findings with lifecycle. A failed or incomplete run is never reported as clear. |
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
