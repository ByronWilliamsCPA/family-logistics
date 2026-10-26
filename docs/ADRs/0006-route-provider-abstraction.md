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
> $8 warning was accepted by the planning team on 2026-10-21. The `ha_relay`
> endpoint (other families' homes need a zone), validity and invalidation
> decisions below were made by the owner on 2026-10-25.
> The front matter reads `draft` because the documentation front-matter
> validator accepts only `draft`, `in-review` and `published`; every ADR in
> this directory uses `draft` there and `proposed` in this line.

**Decision Date**: 2026-10-21 (amended 2026-10-25)
**Deciders**: Byron Williams
**Affected Components**: adapters/routing, domain/validation (TIME-007, ORDER-012, ROUTE-010)
**Implementation Target**: M3, delivered as three stacked PRs (see
[Delivery](#delivery))

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
| `ha_relay` | The app publishes needed legs at `GET /api/v1/route-requests`; an HA automation runs the Waze action and posts results to `POST /api/v1/route-results`. Each leg endpoint is a zone entity id, or, for a location with no zone, its address, except that another family's zone-less home is withheld (ROUTE-010). Results are cached by origin, destination, weekday and 15-minute time-of-day bucket with age and source, and stay valid for that weekday bucket for up to 28 days (see [Owner decisions](#owner-decisions-2026-10-25)). | Default in M3 |
| `google_routes` | Direct `computeRoutes` call with a key restricted to the Routes API and the deployment's egress IP. An in-app monthly spend counter is the hard stop: at $8 Morgan gets a warning (Apprise tag `fl-ops`); at `FL_ROUTES_MONTHLY_BUDGET_USD` (10) discretionary calls stop, non-routing validation continues, and any trip without a recent valid estimate is marked travel-feasibility `incomplete` (ROUTE-010). Google Cloud budget alerts at $8 and $10 are a second signal only (they do not stop spend). | M3, PR 3 (see [Delivery](#delivery)) |
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

### Owner decisions (2026-10-25)

1. **Endpoints: other families' homes need a zone.** Each leg endpoint the
   relay sends to Home Assistant is the location's `ha_zone_entity_id` when it
   has one. A location with no zone is sent as its decrypted address unless it
   is another family's home: the home of a contact (Driver S), even when a
   household member shares it, or the default pickup or drop-off of an external
   passenger (Passenger E, Passenger R). Another family's home without a zone
   never leaves the app: the endpoint is withheld, the leg is unknown
   (ROUTE-010) and any ride that needs it is `incomplete`, never clear. The
   household's own home and venues (a school, a music studio) may be sent as
   addresses. Only the two endpoints of a leg are sent, never names or ride
   purpose. The rule is the pure functions `is_other_family_home` and
   `ha_relay_endpoint` in `family_logistics.domain.address_release`, which the
   `ha_relay` provider must call for every endpoint.

   *Rationale.* Other families' home addresses are privacy class R, held on
   loan, and the notice given to those families promises their home address is
   never sent to Home Assistant as text. Sending it as plaintext to Home
   Assistant, and from there to Waze and into Home Assistant's traces and
   recorder, would break that promise for any home that merely lacks a zone,
   which is the normal state of a newly registered location. The household's
   own address is the operator's to share, and a venue's address is not
   private to any family, so requiring zones for them would only add setup
   work. Ambiguous cases fail closed: a home shared with a contact is treated
   as another family's. Withholding costs only an `incomplete` ride until the
   operator adds a zone, and an incomplete run is never reported as clear.
2. **Cache validity.** `ha_relay` results are cached by origin, destination,
   weekday and 15-minute time-of-day bucket (origin and destination are
   location ids, not addresses). A result observed in a bucket stays valid for
   that same weekday bucket for up to 28 days. A Waze result measures current
   traffic at the time it was taken, so it is valid only through this
   weekday-and-bucket rule: it must never answer a different weekday, a
   different bucket, or a leg whose bucket has no observation in the last 28
   days (that leg is unknown, ROUTE-010).
3. **Invalidation.** Editing a location's address, coordinates or
   `ha_zone_entity_id`, merging it into another location, or re-verifying it
   drops that location's route cache entries. A corrected pickup address must
   never be answered from the old address's travel time for up to 28 days: a
   too-short cached duration reads as feasible, which is the unsafe direction.
   Separately, an entry is purged 28 days after the last observation for its
   weekday bucket (privacy spec, section 7).

```text
#CRITICAL: Security: another family's home address must never reach Home
Assistant as plaintext; the family home and venue addresses may, and once there, Home Assistant may keep them in traces, the logbook,
recorder history and backups, and its Waze action sends every endpoint (a zone
as its coordinates) on to Waze.
#VERIFY: The ha_relay provider calls `ha_relay_endpoint` for every endpoint.
PR 2 adds a test that a zone-less other-family home never appears in a
route-request payload and yields ROUTE-010, and that no address appears in
logs, route cache rows, audit rows or error responses. Check Home Assistant's
trace and recorder behaviour for the relay automation against the deployed
version before enabling ha_relay (privacy spec, section 5).
#ASSUME: Timing: a cached Waze observation is a good enough estimate for the
same weekday and bucket for 28 days.
#VERIFY: PR 2 adds a test that a location with a zone is sent as its zone id
and never as an address, a test that an observation older than 28 days, or
from another weekday or bucket, is not served, and a test
(`test_location_edit_drops_its_route_cache_entries`) that editing, merging or
re-verifying a location drops that location's cached entries so the next
estimate is unknown (ROUTE-010) until Home Assistant posts a fresh result.
Revisit the 28-day window after the shadow dry-run if estimates drift.
```

### Delivery

Routing ships as three stacked PRs:

1. **PR 1:** `NullRouteProvider`, the `build_route_provider` factory and this
   decision record. Until PR 2, `ha_relay` yields the Null provider (every
   estimate is unknown, so any ride that needs an estimate is `incomplete`,
   ROUTE-010), `google_routes` raises a
   typed not-implemented error, and `static` requires an explicit table. The
   factory is not wired into any service yet.
2. **PR 2:** `ha_relay` with the result cache and request tables (migration
   `0005`), `GET /api/v1/route-requests` and `POST /api/v1/route-results`.
   Migration `0005` depends on migration `0004` from the school snapshot
   storage PR (#27); that stack merges before PR 2, so the number holds. The
   28-day window is a fixed value in PR 2, not an `FL_` setting, because it is
   a correctness rule; changing it amends this ADR.
3. **PR 3:** `google_routes` and the monthly budget counter.

## Alternatives Considered

| Option | Assessment |
|--------|------------|
| App calls HA's Waze action directly | Requires a standing HA token in the app; rejected by ADR-007. |
| Google Routes only | Paid from day one; Waze is free and already trusted by the family. Kept as the secondary provider (PR 3). |

## Consequences

- `ha_relay` results lag by one HA automation cycle; weekly and daily runs
  request legs ahead of time, and on-demand runs may finish `incomplete` until
  HA posts results.
- A cached result is a traffic observation for one weekday bucket, so legs not
  observed in the last 28 days stay unknown rather than borrowing another
  bucket's time.
- Address resolution must happen before routing; unverified locations raise
  DEST-004 or ROUTE-010.
- Under `ha_relay`, every leg to or from another family's home with no Home
  Assistant zone stays unknown, and any ride that needs it is `incomplete`
  (ROUTE-010), until the operator gives that location a zone. Family-home and
  venue addresses with no zone do reach Home Assistant and Waze.

## References

- <https://www.home-assistant.io/actions/waze_travel_time.get_travel_times/>
- <https://developers.google.com/maps/documentation/routes/opt-way>
- [ADR-007](0007-ha-initiated-data-exchange.md)
