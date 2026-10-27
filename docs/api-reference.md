---
title: "API Reference"
schema_type: common
status: published
owner: core-maintainer
purpose: "API documentation for Family Logistics."
tags:
  - api
  - reference
---

Complete API documentation for Family Logistics.

## Status and assessment routes

Three routes on the Home Assistant router, all behind the `FL_HA_TOKEN` bearer
token (a missing or wrong token is a 401 with a fixed body, throttled like the
other Home Assistant routes). Answers carry counts, times and fixed strings
only: no child names, addresses, calendar identifiers or finding text. Every
error body is `{"detail": "<fixed sentence>"}` and never echoes the request; the
disabled-gate 503 of `POST /api/v1/assessments` also carries a machine-readable
`code` (below).

### `GET /api/v1/status`

The family-wide card, built from the latest `analysis_run` and the latest school
snapshot.

| Field | Meaning |
|-------|---------|
| `status` | `clear`, `attention_required`, `incomplete` or `failed` |
| `reasons` | why the card is not simply the latest run's verdict, set for any status: `no_run`, `analysis_stale`, `calendar_stale`, `school_snapshot_missing`, `school_snapshot_stale`; or `run_interrupted` (a run started and never finished) |
| `counts` | open findings per severity; `null` when no run has finished |
| `lifecycle` | open findings per lifecycle state (`new`, `ongoing`, `regressed`); `null` when no run has finished |
| `last_analysis_at`, `last_school_sync_at` | when the latest *finished* run finished (a run still executing, or one that died, does not move it); when the school snapshot was fetched |
| `analysis_stale`, `school_snapshot_stale` | the run, or the snapshot, is older than its limit (or missing) |
| `calendar_stale` | the oldest successful collection among the enabled calendar sources is older than `FL_SOURCE_MAX_STALENESS_MINUTES`, judged against now (no source, or one never collected, counts as stale) |
| `run_in_flight` | a run is queued or executing in this process, or the pipeline lock is held (a scheduled scan, collection or purge) |
| `generated_at` | when the card was built |

The card is defined as the last *stored* analysis. A failed on-demand run that
left no database row (it never got the pipeline lock, the route provider could
not be built, or it was abandoned at shutdown) does not change it: `GET
/api/v1/assessments/{run_id}` reports that run as `failed`, while `GET
/api/v1/status` keeps the previous verdict, possibly `clear`, until that analysis
goes stale (`FL_SOURCE_MAX_STALENESS_MINUTES`, default 120). This does not break
design rule 4, because the card never claims to describe a run that stored
nothing. After a button press, Home Assistant should poll the run id and treat a
`failed` there as authoritative for that press, not wait for the card to change.

A failed or incomplete analysis is never reported as clear (design rule 4). A
card is `clear` only when the latest finished run was clear, it is no older than
`FL_SOURCE_MAX_STALENESS_MINUTES` (default 120), the calendars were collected
within that same limit (an on-demand run assesses what is stored, so a clear run
over an old collection is not trusted) and the school snapshot is no older than
26 hours. Otherwise a clear run is downgraded to `incomplete` with
the reasons above; `attention_required`, `incomplete` and `failed` are never
loosened. A store error is a 503 (`Status store unavailable`), never a card.

### `POST /api/v1/assessments`

Starts an on-demand run (trigger `on_demand`, with the latest school snapshot)
and answers `202` with `{"run_id": "...", "coalesced": false}`. The run does not
collect the calendars; it assesses what is stored, so a stale calendar shows up
as DATA-011 rather than as a clear result. It returns at once; poll the run.

The route is gated by `FL_ON_DEMAND_ENABLED` (default `false`). While it is off
every request is a 503 with `{"detail": "On-demand runs are disabled", "code":
"on_demand_disabled"}` and nothing starts.

One run is queued or executing at a time. A second request while one is queued
gets the same `run_id` with `coalesced: true`; so does a request that arrives
while a scheduled run is executing. If the scheduler is collecting (it holds the
pipeline lock but has not started a run), the request queues behind it. A
request during shutdown is a 503 (`Service is shutting down`).

Known risk, not built: a button pressed repeatedly after each run finishes can
queue back to back and starve the scheduler's scans of the pipeline lock (a scan
that finds the lock held is skipped). There is no cooldown yet; one is future
work, and the runbook says what to watch.

### `GET /api/v1/assessments/{run_id}`

Reports one run: `state` is `queued` (waiting for the pipeline lock), `running`
or `finished`. `status`, `counts` and `finished_at` are set only once finished,
so a queued or running run never reads as clear. A run that raised, never got
the lock within 30 minutes, or died with its process is `finished` with status
`failed`. An unknown id is a 404 (`Run not found`); a value that is not a run
id (a lower-case UUID) is a 422 (`Invalid run id`); a store error is a 503.

The service remembers the last 32 runs that failed before writing a row, in
memory. A run id past that, or from before a restart, answers 404.

## Core Module

::: family_logistics.core.config
    options:
      show_root_heading: true
      members_order: source

## Utils Module

### Logging

::: family_logistics.utils.logging
    options:
      show_root_heading: true
      members_order: source

## CLI Module

::: family_logistics.cli
    options:
      show_root_heading: true
      members_order: source
