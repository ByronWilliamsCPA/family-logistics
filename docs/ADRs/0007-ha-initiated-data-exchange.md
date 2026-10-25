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

## Snapshot Contract

Implemented for `POST /api/v1/ingest/ha-snapshot`; the full guide, with an
example payload, is [Home Assistant school snapshot](../guides/ha-school-snapshot.md).

- **Request**: `{"fetched_at", "calendars", "zones"}`. `calendars` maps each
  `calendar.*` entity ID to its raw `calendar.get_events` result
  (`{"events": [{"start", "end", "summary", "description"?, "location"?}]}`);
  `zones` lists `zone.*` entity IDs. Every object refuses unknown keys; lists
  and strings are bounded (16 calendars, 1000 events each, 64 zones, 500
  character summaries); the body is limited to 1 MiB, checked before parsing
  and after authentication.
- **Mapping**: `FL_HA_SCHOOLS` maps bell and district calendars to a registry
  school location and its students. The payload must hold exactly the
  configured calendars. Summaries are classified deterministically by one
  keyword table (no school, early release, late start, normal); an
  unrecognized summary is `unknown`, never `normal`. Bell times come only from
  timed bell-calendar events within one local day (`FL_TIMEZONE`).
- **Time**: `fetched_at` must carry a UTC offset and be no more than 5 minutes
  ahead of the server; the stored freshness is `min(fetched_at, received_at)`.
  A snapshot not newer than the stored one is refused (409) unless it is an
  identical re-post, which is acknowledged as unchanged.
- **Zones**: each zone must be the `ha_zone_entity_id` of a registry location,
  else 422. Zones are checked only and never stored; the payload carries no
  coordinates. (This narrows the earlier note on zone coordinates: location
  records stay the only home of coordinates, entered through the registry.)
- **Storage**: one row per person and day (date, location, start, end, kind)
  plus the freshness, a content hash and a day count, replaced atomically with
  one audit row of counts and freshness. Event text is never stored or logged.
- **Errors**: 422 bodies carry an error code or redacted locations only;
  configuration problems (no school, no audit key, a configured ID missing
  from the registry) answer 503, and a write lock not granted in time answers
  503 with `Retry-After`. The registry and zone checks run under the write
  lock before the comparison with the stored snapshot, so a reference error
  wins over a 409. A snapshot in which no configured calendar has an event in
  the window (the signature of a Home Assistant entity outage) is refused with
  422 `EMPTY_SNAPSHOT`, and the stored one is kept to go stale. Nothing is
  stored on any error.

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
