---
schema_type: planning
title: "ADR-006: Route Provider Abstraction and Constrained Stop Ordering"
description: "Travel estimates come through a RouteProvider protocol with HA-relayed Waze, Google Routes under a budget cap, and a static provider; deadlines always beat speed."
tags:
  - architecture
  - decisions
  - adr
  - routing
status: draft
owner: core-maintainer
component: Strategy
source: "Family Logistics development handoff v1.0, sections 3.4 and 10; planning response item 15"
purpose: "Decouple feasibility checks from any single routing service and define the ordering rule for multi-stop rides."
---

> **Status**: `proposed`. The $10/month app-enforced Google Routes cap with an
> $8 warning was accepted by the planning team on 2026-10-21.

**Decision Date**: 2026-10-21
**Deciders**: Byron Williams
**Affected Components**: adapters/routing, domain/validation (TIME-007, ORDER-012, ROUTE-010)
**Implementation Target**: M3

## Context

The family wants the shortest driving time when stop order is flexible, but
never at the cost of a missed drop-off deadline. Waze is available through Home
Assistant's `waze_travel_time.get_travel_times` action, which gives pairwise
times only. Family Logistics holds no Home Assistant credential (ADR-007), so it cannot
call that action itself. Google Routes `computeRoutes`
offers `optimizeWaypointOrder` but optimises efficiency, not per-passenger
deadlines, and costs money.

## Decision

```python
class RouteProvider(Protocol):
    def estimate(
        self, origin: Place, destination: Place, departure_time: datetime, mode: Mode
    ) -> RouteEstimate | Unknown: ...
    def optimize(
        self,
        origin: Place,
        stops: Sequence[Place],
        end: Place | None,
        constraints: StopConstraints,
    ) -> StopPlan | Unknown: ...
```

Providers (`FL_ROUTE_PROVIDER`):

| Provider | How it works | Use |
|----------|--------------|-----|
| `ha_relay` | The app publishes needed legs at `GET /api/v1/route-requests`; an HA automation runs the Waze action and posts results to `POST /api/v1/route-results`. Results are cached by endpoints and 15-minute departure bucket with age and source. | Default in M3 |
| `google_routes` | Direct `computeRoutes` call with a key restricted to the Routes API and the deployment's egress IP. An in-app monthly spend counter is the hard stop: at $8 Morgan gets a warning (Apprise tag `fl-ops`); at `FL_ROUTES_MONTHLY_BUDGET_USD` (10) discretionary calls stop, non-routing validation continues, and any trip without a recent valid estimate is marked travel-feasibility `incomplete` (ROUTE-010). Google Cloud budget alerts at $8 and $10 are a second signal only (they do not stop spend). | Evaluate in M3 |
| `static` | Fixed matrix from fixtures. | Tests and simulations |

Ordering rule, in the domain layer and independent of provider:

1. A manual `STOP_ORDER` always wins; if it misses a deadline, raise ORDER-012.
2. Otherwise, for up to 6 stops, enumerate permutations, estimate each with
   pairwise times plus the configured per-stop load/unload buffer, reject any
   that miss a passenger deadline or pickup time, and choose the fastest valid
   one. Above 6 stops, use the provider's optimisation as a candidate and
   validate it the same way.
3. A missing estimate, unresolved address or provider error yields `Unknown`,
   which raises ROUTE-010 and makes the run `incomplete`. No ETA is ever
   fabricated.

Unofficial Waze scraping and undocumented APIs are prohibited.

## Alternatives Considered

| Option | Assessment |
|--------|------------|
| App calls HA's Waze action directly | Requires a standing HA token in the app; rejected by ADR-007. |
| Google Routes only | Paid from day one; Waze is free and already trusted by the family. Kept as an evaluated option. |

## Consequences

- `ha_relay` results lag by one HA automation cycle; weekly and daily runs
  request legs ahead of time, and on-demand runs may finish `incomplete` until
  HA posts results.
- Address resolution must happen before routing; unverified locations raise
  DEST-004 or ROUTE-010.

## References

- <https://www.home-assistant.io/actions/waze_travel_time.get_travel_times/>
- <https://developers.google.com/maps/documentation/routes/opt-way>
- [ADR-007](0007-ha-initiated-data-exchange.md)
