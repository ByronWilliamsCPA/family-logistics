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
error body is `{"detail": "<fixed sentence>"}` and never echoes the request.

### `GET /api/v1/status`

The family-wide card, built from the latest `analysis_run` and the latest school
snapshot.

| Field | Meaning |
|-------|---------|
| `status` | `clear`, `attention_required`, `incomplete` or `failed` |
| `reasons` | why a run that was clear is not: `no_run`, `analysis_stale`, `school_snapshot_missing`, `school_snapshot_stale`; or `run_interrupted` (a run started and never finished) |
| `counts` | open findings per severity; `null` when no run has finished |
| `lifecycle` | open findings per lifecycle state (`new`, `ongoing`, `regressed`); `null` when no run has finished |
| `last_analysis_at`, `last_school_sync_at` | when the latest run finished; when the school snapshot was fetched |
| `analysis_stale`, `school_snapshot_stale` | the run, or the snapshot, is older than its limit (or missing) |
| `run_in_flight` | a run is queued or executing in this process |
| `generated_at` | when the card was built |

A failed or incomplete analysis is never reported as clear (design rule 4). A
card is `clear` only when the latest finished run was clear, it is no older than
`FL_SOURCE_MAX_STALENESS_MINUTES` (default 120) and the school snapshot is no
older than 26 hours. Otherwise a clear run is downgraded to `incomplete` with
the reasons above; `attention_required`, `incomplete` and `failed` are never
loosened. A store error is a 503 (`Status store unavailable`), never a card.

### `POST /api/v1/assessments`

Starts an on-demand run (trigger `on_demand`, with the latest school snapshot)
and answers `202` with `{"run_id": "...", "coalesced": false}`. The run does not
collect the calendars; it assesses what is stored, so a stale calendar shows up
as DATA-011 rather than as a clear result. It returns at once; poll the run.

One run is queued or executing at a time. A second request while one is queued
gets the same `run_id` with `coalesced: true`; so does a request that arrives
while a scheduled run is executing. If the scheduler is collecting (it holds the
pipeline lock but has not started a run), the request queues behind it. A
request during shutdown is a 503 (`Service is shutting down`).

### `GET /api/v1/assessments/{run_id}`

Reports one run: `state` is `queued` (waiting for the pipeline lock), `running`
or `finished`. `status`, `counts` and `finished_at` are set only once finished,
so a queued or running run never reads as clear. A run that raised, never got
the lock within 30 minutes, or died with its process is `finished` with status
`failed`. An unknown id is a 404 (`Run not found`); a value that is not a run
id (a lower-case UUID) is a 422 (`Invalid run id`); a store error is a 503.

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
