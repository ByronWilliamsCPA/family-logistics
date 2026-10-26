---
title: "Home Assistant route relay"
schema_type: common
status: published
owner: core-maintainer
purpose: "Wiring a Home Assistant automation to price the legs Family Logistics asks for."
tags:
  - guide
  - routing
---

Family Logistics never calls Home Assistant (ADR 0007) and holds no Home
Assistant credential. With `FL_ROUTE_PROVIDER=ha_relay` it works the other way
round: it queues the legs it cannot price, Home Assistant polls for them, prices
each with `google_travel_time.get_travel_times` and posts the durations back.
The app caches each result for the same weekday and 15-minute bucket for up to
28 days. A leg with no valid cached time is unknown (ROUTE-010, run
incomplete); the app never guesses a duration. See
[ADR 0006](../ADRs/0006-route-provider-abstraction.md) for the decisions.

!!! warning "Unverified assumption"
    `#ASSUME`: Home Assistant resolves a bare time-of-day `departure_time`
    (for example `08:00:00`) to the **next** occurrence of that time, not to
    "now". This has not been verified.
    `#VERIFY`: run the deployment checklist below before trusting any result.

## Endpoints

Both are under `/api/v1`, require `Authorization: Bearer <FL_HA_TOKEN>` and are
answered with the same 401 for any authentication failure.

### GET /api/v1/route-requests

Returns the **due** requests, oldest first, at most 50:

```json
{
  "requests": [
    {
      "request_id": "0123456789abcdef0123456789abcdef",
      "origin": {"kind": "zone", "value": "zone.home_base"},
      "destination": {"kind": "zone", "value": "zone.school_a"},
      "weekday": "fri",
      "departure_time": "08:00:00"
    }
  ]
}
```

- `kind` is `zone` when the location has a Home Assistant zone (the entity id is
  sent, never the address) and `address` only for a location with no zone, in
  which case `value` is the decrypted address. Give a location a zone to keep its
  address off the relay.
- `weekday` is `mon` to `sun`; `departure_time` is a local `HH:MM:SS`.
- A request is **due** when its weekday matches the weekday of the next local
  occurrence of `departure_time` at poll time. Others stay queued. A request that
  has expired or been answered is not returned. A request whose location has
  neither a zone nor a readable address is left out.

### POST /api/v1/route-results

```json
{"results": [{"request_id": "0123456789abcdef0123456789abcdef", "duration_seconds": 1260}]}
```

- `duration_seconds` must be an integer from 1 to 43200 (12 hours). Strings,
  floats, booleans, null, zero and negatives get a 422 and nothing is stored.
- 1 to 100 results per call; more, none, or a malformed body is a 422.
- Each item is judged on its own and the response is 200:
  `accepted`, `unchanged` (the same duration posted again) or `rejected` with a
  reason: `unknown_request`, `expired` or `conflict` (a different duration for an
  answered request).
- Only `request_id` and `duration_seconds` are read. Any weekday or endpoint you
  add is ignored: the app already knows them from its own request row.
- 503 with `Retry-After` means the database was busy; nothing was stored. Post
  again later.

## Example automation

Placeholders only: replace `fl.example.invalid`, the entry id and the secret.
Find the `google_travel_time` config entry id in Home Assistant (the integration
page, or Developer tools).

```yaml
# configuration.yaml
rest_command:
  fl_get_route_requests:
    url: "http://fl.example.invalid:8000/api/v1/route-requests"
    method: GET
    headers:
      Authorization: !secret fl_bearer
  fl_post_route_result:
    url: "http://fl.example.invalid:8000/api/v1/route-results"
    method: POST
    headers:
      Authorization: !secret fl_bearer
      Content-Type: application/json
    payload: >-
      {"results": [{"request_id": "{{ request_id }}",
      "duration_seconds": {{ duration_seconds }}}]}
```

```yaml
# automations.yaml
- alias: Family Logistics route relay
  mode: single
  triggers:
    - trigger: time_pattern
      minutes: "/10"
  actions:
    - action: rest_command.fl_get_route_requests
      response_variable: poll
    - repeat:
        for_each: "{{ (poll.content.requests | default([])) }}"
        sequence:
          - action: google_travel_time.get_travel_times
            data:
              entry_id: "0123456789ABCDEFGHIJKLMNOP"
              origin: "{{ repeat.item.origin.value }}"
              destination: "{{ repeat.item.destination.value }}"
              departure_time: "{{ repeat.item.departure_time }}"
            response_variable: travel
          - if: "{{ travel.routes | default([]) | length > 0 }}"
            then:
              - action: rest_command.fl_post_route_result
                data:
                  request_id: "{{ repeat.item.request_id }}"
                  duration_seconds: "{{ travel.routes[0].duration | int(0) }}"
```

Notes:

- `origin` and `destination` take the zone entity id or the address text as the
  same field, so the automation passes `value` through and ignores `kind`.
- The automation posts the **first** route's `duration`. Whether to use the
  slowest returned route instead is an open owner decision.
- `int(0)` turns a missing duration into 0, which the app refuses with a 422 so
  the failure is visible in the Home Assistant trace instead of storing a guess.
- The response shape above was verified on Home Assistant Core 2026.10.2. Check
  it again after a Home Assistant upgrade; the app's strict validation makes a
  changed shape fail visibly.

## Deployment checklist

1. Set `FL_HA_TOKEN`, `FL_AUDIT_KEY` and, if any location has no zone,
   `FL_DATA_KEY`. Confirm both routes return 401 without the token.
2. Give each location that is a routing endpoint a Home Assistant zone, so no
   address is sent.
3. **Compare the same `google_travel_time` request in the evening and again
   before 08:00. Matching durations mean Home Assistant resolves a bare
   time-of-day to the next occurrence.** Different durations mean it prices
   "now": do not use the results, and rework the request shape. Record the
   outcome in the deployment repository.
4. Keep notifications in shadow mode until step 3 has passed.
5. Check one full cycle: a run that needs a leg is incomplete (ROUTE-010), the
   next poll lists it, the post is accepted, and the following run is clear.
6. Decide first route versus slowest route (open owner decision).
