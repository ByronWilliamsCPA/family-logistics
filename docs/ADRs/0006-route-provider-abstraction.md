---
schema_type: planning
title: "ADR-006: Route Provider Abstraction and Constrained Stop Ordering"
description: "Travel estimates come through a RouteProvider protocol with HA-relayed Google travel times, Google Routes under a budget cap, and a static provider; deadlines always beat speed."
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

> **Status**: `proposed`; the `ha_relay` provider is implemented (PR 2 of 3) but
> stays in shadow mode until the unverified assumption below is checked on the
> deployed Home Assistant. The $10/month app-enforced Google Routes cap with an
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
never at the cost of a missed drop-off deadline. Travel times are available
through Home Assistant's `google_travel_time.get_travel_times` action, which
gives pairwise times only. Family Logistics holds no Home Assistant credential (ADR-007), so it cannot
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
| `ha_relay` | The app publishes needed legs at `GET /api/v1/route-requests`; an HA automation runs the `google_travel_time.get_travel_times` action and posts results to `POST /api/v1/route-results`. Each leg endpoint is a zone entity id, or, for a location with no zone, its address, except that another family's zone-less home is withheld (ROUTE-010). Results are cached by origin, destination, weekday and 15-minute time-of-day bucket with age and source, and stay valid for that weekday bucket for up to 28 days (see [Owner decisions](#owner-decisions-2026-10-25)). | Default in M3 |
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

Unofficial scraping and undocumented routing APIs are prohibited.

### Owner decisions (2026-10-25)

1. **Endpoints: other families' homes need a zone.** Each leg endpoint the
   relay sends to Home Assistant is the location's `ha_zone_entity_id` when it
   has one. A location with no zone is sent as its decrypted address unless it
   is another family's home: the `home_location_id` of a contact (Driver S),
   even when a household member shares it or the contact is inactive. An
   external passenger's default pickup or drop-off is treated as a venue, so
   Passenger E's or Passenger R's home is protected only when a contact from
   that household is registered with it as home. Another family's home
   without a zone never leaves the app: the endpoint is withheld, the leg is
   unknown (ROUTE-010) and any ride that needs it is `incomplete`, never
   clear. The
   household's own home and venues (a school, a music studio) may be sent as
   addresses. Only the two endpoints of a leg are sent, never names or ride
   purpose. The rule is the pure functions `is_other_family_home` and
   `ha_relay_endpoint` in `family_logistics.domain.address_release`, which the
   `ha_relay` provider must call for every endpoint.

   *Rationale.* Other families' home addresses are privacy class R, held on
   loan, and the notice given to those families promises their home address is
   never sent to Home Assistant as text. Sending it as plaintext to Home
   Assistant, and from there to Google and into Home Assistant's traces and
   recorder, would break that promise for any home that merely lacks a zone,
   which is the normal state of a newly registered location. The household's
   own address is the operator's to share, and a venue's address is not
   private to any family, so requiring zones for them would only add setup
   work. Ambiguous cases fail closed: a home shared with a contact is treated
   as another family's. A passenger's defaults are not used, because a default
   is as often a school or club as a home, and treating every default as a home
   would withhold zone-less venues; registering the household's contact is the
   explicit signal that a location is someone's home. Withholding costs only an `incomplete` ride until the
   operator adds a zone, and an incomplete run is never reported as clear.
2. **Cache validity.** `ha_relay` results are cached by origin, destination,
   weekday and 15-minute time-of-day bucket (origin and destination are
   location ids, not addresses). A result observed in a bucket stays valid for
   that same weekday bucket for up to 28 days. A travel-time result measures
   current traffic at the time it was taken, so it is valid only through this
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
#CRITICAL: Security: another family's home address (a contact's home) must
never reach Home Assistant as plaintext; the family home, venue and passenger
default addresses may, and once there, Home Assistant may keep them in
traces, the logbook, recorder history and backups, and its Google travel time
action sends every endpoint (a zone as its coordinates) on to Google.
#VERIFY: The ha_relay provider calls `ha_relay_endpoint` for every endpoint.
PR 2 adds a test that a zone-less other-family home never appears in a
route-request payload and yields ROUTE-010, and that no address appears in
logs, route cache rows, audit rows or error responses. Check Home Assistant's
trace and recorder behaviour for the relay automation against the deployed
version before enabling ha_relay (privacy spec, section 5).
#ASSUME: Timing: a cached travel-time observation is a good enough estimate for
the same weekday and bucket for 28 days.
#VERIFY: PR 2 adds, in `tests/unit/test_route_relay_api.py`, a test that a
location with a zone is sent as its zone id and never as an address
(`test_a_zoned_location_is_sent_as_its_zone_id_and_never_as_an_address`); in
`tests/unit/test_ha_relay_provider.py`, a test that an observation older than 28
days, or from another weekday or bucket, is not served
(`test_an_observation_older_than_28_days_is_not_served` and
`test_another_bucket_or_weekday_is_not_borrowed`); and in
`tests/unit/test_route_relay_invalidation.py`
`test_location_edit_drops_its_route_cache_entries`, which shows that editing,
merging or re-verifying a location drops that location's cached entries and
pending requests so the next estimate is unknown (ROUTE-010) until Home
Assistant posts a fresh result. Revisit the 28-day window after the shadow
dry-run if estimates drift.
```

### How `ha_relay` works (PR 2)

`HaRelayRouteProvider.estimate` is synchronous and reads the cache only. A
valid hit is returned with source `ha_relay`. A miss returns `None` (the engine
turns that into ROUTE-010 and an `INCOMPLETE` run; a duration is never guessed)
and queues one pending request for the leg. A second miss for the same origin,
destination, weekday and bucket does not queue a duplicate (a partial unique
index enforces it), and the queue holds at most `MAX_PENDING_REQUESTS` (500)
live requests. `estimate` never raises into the engine: a busy or failing store
is logged as a structured warning without addresses and returns `None`.
`optimize` calls `choose_stop_order` with the cache-backed estimator.

The app, not the sender, decides what a result means. The request row stores
`request_id`, the origin and destination location ids, the weekday, the bucket,
`created_at`, `expires_at` and a status. `POST /api/v1/route-results` answers a
request by `request_id`; the cache key is taken from the app's own row, and any
weekday, endpoint or other field the sender adds is ignored. `observed_at` is
the server's receipt time.

Invalidation is enforced by the database, not by caller discipline: the cache
and request tables reference `location` with `ON DELETE CASCADE`, and a trigger
(`trg_location_route_relay_invalidate`, migration `0005`) deletes a location's
cache entries and pending requests, as origin or destination, whenever its id,
zone, address, coordinates or verification state change. Every mutation path
therefore drops them: a registry upsert, a merge, a re-verification, a seed
re-apply or a raw SQL update. A registry upsert that does not change the
plaintext reuses the stored ciphertext, so re-applying an unchanged seed does
not flush the cache.

### Unverified assumption and deployment checklist

```text
#ASSUME: External resources: Home Assistant resolves a bare time-of-day
`departure_time` (for example `08:00:00`, no date) to the NEXT occurrence of
that time, not to "now". This is NOT verified.
#VERIFY: Before enabling notifications, the owner compares the same request in
the evening and again before 08:00 the next morning. Matching durations mean
next-occurrence. Different durations mean "now" is being priced and the cached
weekday bucket would be mislabelled; do not leave shadow mode, and rework the
request shape.
```

Deployment checklist item: **compare the same `google_travel_time` request in
the evening and again before 08:00; matching durations mean next-occurrence.**
Until that is done, the relay's results are not treated as verified and
`FL_NOTIFICATION_MODE` stays `shadow`. The same item is in the
[route relay guide](../guides/ha-route-relay.md).

Because of this assumption `GET /api/v1/route-requests` returns only **due**
requests. A request is due when its weekday equals the weekday of the next local
occurrence of its bucket's start time at poll time (so a Friday 08:00 request is
due on Friday before 08:00 and not on Friday at 09:00, when the next 08:00 is
Saturday's). The rest stay queued until their day. Under the assumption, the
weekday and bucket on a result are then true. The poll never serves an expired
or answered request.

```text
#EDGE: Timing: around a daylight saving change, a bucket start time may not
exist (spring forward) or occur twice (fall back) on the due day.
#VERIFY: `due_weekday` computes the next occurrence as an instant, skips a
nonexistent local time to the next day and takes the first of an ambiguous one;
tests/unit/test_route_relay_repo.py::TestDueRequests covers midnight and both
DST edges.
```

### Result validation

`POST /api/v1/route-results` validates strictly at the boundary and returns 422
for anything else, so a changed Home Assistant shape fails visibly:

- `duration_seconds` must be a JSON integer from 1 to
  `MAX_ROUTE_DURATION_SECONDS` (12 hours). Strings, floats (even `600.0`),
  booleans, null, zero and negatives are refused.
- `results` holds 1 to `MAX_RESULTS_PER_POST` (100) items and `request_id` is a
  string of 1 to 64 characters. These Pydantic `max_length` limits are the body
  cap.
- An unknown, expired or already answered `request_id` is rejected **for that
  item** with a reason (`unknown_request`, `expired`, `conflict`); the rest of
  the batch is still applied and the response is 200, never a 5xx. Re-posting
  the same duration for an answered request is an idempotent no-op
  (`unchanged`); a different duration is a `conflict` for that item.
- A held database write lock returns 503 with `Retry-After` and stores nothing;
  a missing audit key returns 503 and stores nothing.
- Each POST appends one audit row holding counts only (accepted, rejected,
  unchanged), in the action text and as a keyed hash of the counts. No address,
  zone, request id or per-leg duration is audited, logged or put in an error.

### Version note

The Home Assistant action and response shape described here were verified on
Home Assistant Core 2026.10.2. A different Core version may change the
`google_travel_time.get_travel_times` response; the strict validation above is
what makes such a change visible. Re-verify after a Home Assistant upgrade.

### Design notes from PR 2

- The first route in the response is used; whether to take the slowest route
  instead is an open owner decision.
- A request whose endpoint cannot be resolved (a location with neither a zone
  nor an address, or an address that cannot be decrypted) is left out of the poll
  and logged by request id only. It stays pending until it expires, and the leg
  stays unknown.
- Pending requests expire after 8 days (`REQUEST_TTL`), long enough for a weekly
  bucket to come round once. Expired requests are purged with
  `purge_expired_route_requests`, and cache entries 28 days after their last
  observation with `purge_expired_route_cache`; no job calls them yet (the
  scheduler arrives later in M3).

### Delivery

Routing ships as three stacked PRs:

1. **PR 1:** `NullRouteProvider`, the `build_route_provider` factory and this
   decision record. Before PR 2, `ha_relay` yielded the Null provider (every
   estimate unknown, so any ride that needed an estimate was `incomplete`,
   ROUTE-010). `google_routes` raises a typed not-implemented error and
   `static` requires an explicit table. The factory is not wired into any
   service yet. `NullRouteProvider` remains as a fail-closed building block.
2. **PR 2 (this change):** `ha_relay` with the result cache and request tables
   (migration `0005`), `GET /api/v1/route-requests` and
   `POST /api/v1/route-results`. `build_route_provider` now returns the real
   `HaRelayRouteProvider` for `ha_relay` and takes the database engine; without
   one it raises `RouteProviderUnavailableError` (error code
   `ROUTE_PROVIDER_ENGINE_REQUIRED`) rather than falling back. Migration
   `0005` depends on migration `0004` from the school snapshot storage PR
   (#27), which has merged. The 28-day window is a fixed value, not an `FL_`
   setting, because it is a correctness rule; changing it amends this ADR.
3. **PR 3:** `google_routes` and the monthly budget counter.

## Alternatives Considered

| Option | Assessment |
|--------|------------|
| App calls HA's travel-time action directly | Requires a standing HA token in the app; rejected by ADR-007. |
| Google Routes only | Paid from day one; the relay keeps the Google key in Home Assistant, not in this app, and adds no app-side spend. Kept as the secondary provider (PR 3). |

## Consequences

- `ha_relay` results lag by one HA automation cycle; weekly and daily runs
  request legs ahead of time, and on-demand runs may finish `incomplete` until
  HA posts results.
- A cached result is a traffic observation for one weekday bucket, so legs not
  observed in the last 28 days stay unknown rather than borrowing another
  bucket's time.
- Until the next-occurrence assumption is verified on the deployed Home
  Assistant, the relay's estimates must not drive notifications.
- Address resolution must happen before routing; unverified locations raise
  DEST-004 or ROUTE-010.
- Under `ha_relay`, every leg to or from another family's home with no Home
  Assistant zone stays unknown, and any ride that needs it is `incomplete`
  (ROUTE-010), until the operator gives that location a zone. Family-home and
  venue addresses with no zone do reach Home Assistant and Google.

## References

- <https://www.home-assistant.io/integrations/google_travel_time/>
- <https://developers.google.com/maps/documentation/routes/opt-way>
- [ADR-007](0007-ha-initiated-data-exchange.md)
- [Home Assistant route relay guide](../guides/ha-route-relay.md)
