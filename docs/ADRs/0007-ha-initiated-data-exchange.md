---
schema_type: planning
title: "ADR-007: Home Assistant Initiates Every Exchange"
description: "Home Assistant pushes school, zone and route data to Family Logistics and polls its status; the app stores no Home Assistant credential."
tags:
  - architecture
  - decisions
  - adr
  - home_assistant
  - security
status: draft
owner: core-maintainer
component: Strategy
source: "Planning response items 4 and 5"
purpose: "Record the application side of the Home Assistant trust boundary."
---

> **Status**: `proposed`. The infrastructure side (placement, ingress, network
> controls) is recorded in the private deployment repository.

**Decision Date**: 2026-10-21
**Deciders**: Byron Williams
**Affected Components**: api (HA routes), services/collector (HA snapshot), adapters/routing (ha_relay)
**Implementation Target**: M2 (snapshot), M3 (route relay), M4 (status card)

## Context

Family Logistics needs school and district calendar events, bell-schedule
exceptions, zone definitions and Waze estimates that live in Home Assistant, and
Home Assistant needs the app's status for its dashboard. The planning team chose
a one-way trust direction: Home Assistant calls the app, and the app holds no
Home Assistant token. MQTT was dropped.

## Decision

The app exposes token-authenticated routes for Home Assistant only, and never
initiates a connection to Home Assistant:

| Route | Direction | Purpose |
|-------|-----------|---------|
| `GET /healthz` | Uptime Kuma, no auth | liveness |
| `GET /api/v1/status` | HA polls (`rest:`) | family-wide card: status, counts, last sync, last analysis, stale flags |
| `POST /api/v1/assessments` | HA button (`rest_command:`) | on-demand run; coalesced while one is running; returns `run_id` |
| `GET /api/v1/assessments/{run_id}` | HA polls | progress |
| `POST /api/v1/ingest/ha-snapshot` | HA automation pushes | school/district `calendar.get_events` output and zone list, with `fetched_at` |
| `GET /api/v1/route-requests` | HA automation polls | legs needing an estimate |
| `POST /api/v1/route-results` | HA automation pushes | Waze results |

- Authentication: every HA route except `/healthz` requires
  `Authorization: Bearer <FL_HA_TOKEN>`, compared in constant time; the app
  refuses to start if the token is shorter than 32 characters. The token lives
  only in the deployment secret store and HA `secrets.yaml`.
- Network: the deployment must restrict who can reach these routes (for
  example, TLS plus a reverse-proxy rule that admits only the Home Assistant
  host and pins paths and methods). The token is the authenticator; network
  restriction narrows reachability and never grants access on its own. The
  site-specific controls are recorded in the private deployment repository.
- Snapshot validation: payloads are schema-checked, size-limited and stored with
  source attribution. A missing or stale snapshot (proposed limit 26 hours)
  raises DATA-011 and makes school-dependent checks `incomplete`; it is never
  read as "no school".
- School schedules reuse Home Assistant's two existing layers, delivered in the
  snapshot: the school-specific bell schedules (including weekday variations)
  and the district Google calendars (closures, short days). The app keeps no
  duplicate bell-schedule configuration. Pickup is due at the end of the last
  scheduled activity at that location, not at the bell. Unrecorded sports are
  never inferred; sports are entered manually in the child's iCloud calendar.
- Payloads carry no secrets; zone coordinates are stored as location records
  under the privacy spec.

## Alternatives Considered

| Option | Assessment |
|--------|------------|
| App holds a scoped HA token and pulls | Simpler data flow, but a standing credential to the most trusted controller on the LAN. Rejected by the planning team. |
| MQTT | No shared broker exists; adds infrastructure. Removed. |
| Identity-provider service account instead of a source restriction | A valid alternative at the proxy layer; the choice is a deployment decision. |

## Consequences

- No HA credential can leak from Family Logistics.
- Freshness depends on HA automations running; the app surfaces staleness.
- HA configuration (automations, `rest_command`, secrets) becomes part of the
  deployment and is documented in the private deployment repository.

## References

- [ADR-006](0006-route-provider-abstraction.md)
