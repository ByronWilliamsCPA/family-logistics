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

!!! note "Not wired in yet"
    The two routes below are live, but nothing in the application builds the
    `ha_relay` provider yet (`build_route_provider` has no caller outside tests),
    so no request is queued and `GET /api/v1/route-requests` returns an empty
    list until the M3 wiring lands.

!!! warning "Unverified assumptions"
    `#ASSUME`: Home Assistant resolves a bare time-of-day `departure_time`
    (for example `08:00:00`) to the **next** occurrence of that time, not to
    "now", and the price it returns depends on that time at all. Neither has
    been verified.
    `#ASSUME`: Home Assistant's configured time zone equals `FL_TIMEZONE`,
    because `departure_time` is a bare local time that Home Assistant reads in
    its own zone. If they differ, results are silently filed under the wrong
    bucket or weekday.
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
- **Other families' homes need a zone.** A location with no zone that is another
  family's home is never sent: its address does not leave the app, the leg stays
  unknown (ROUTE-010) and any ride that needs it is incomplete until the home has
  a zone. A home is another family's when a contact from that family (for
  example Driver S) is registered with it as home, even if a household member
  lists the same home. A passenger's default pickup or drop-off alone does not
  count: Passenger E's home is protected only once a contact from that
  household is registered with it as home, or it has a zone. The household's own
  zone-less home and venues such as a school or a music studio are still sent as
  addresses. The rule is `family_logistics.domain.address_release`.
- `weekday` is `mon` to `sun`; `departure_time` is a local `HH:MM:SS`.
- A request is **due** when its weekday matches the weekday of the next local
  occurrence of `departure_time` at poll time **and** that occurrence is at least
  30 minutes away, so Home Assistant's latency and clock skew cannot carry the
  pricing call across the start time. A Friday `08:00:00` request is returned on
  Friday until 07:30 and then waits a week. Others stay queued. A request that
  has expired or been answered is not returned. A start time that does not exist
  or occurs twice on either of the next two local days (a daylight saving
  change) is not returned either.
- The app judges every endpoint again when the poll is served, against the
  registry as it is then: a request queued before a contact was registered for a
  home is left out from that poll on, and nothing is decrypted for it.
- A request whose location has neither a zone nor a readable address (an
  address that cannot be decrypted, or no `FL_DATA_KEY`), or whose endpoint is
  another family's zone-less home, is left out. This fails
  closed: the leg stays unknown. It still occupies the queue (500 requests) until
  it expires after 8 days, which is how it clears. The limit of 50 per poll
  counts only the requests that can be routed, so unroutable ones cannot starve
  the rest.

### POST /api/v1/route-results

```json
{"results": [{"request_id": "0123456789abcdef0123456789abcdef", "duration_seconds": 1260}]}
```

- `duration_seconds` must be an integer from 1 to 43200 (12 hours). Strings,
  floats, booleans, null, zero and negatives get a 422 and nothing is stored.
- 1 to 100 results per call; more, none, or a malformed body is a 422. The 422
  body lists each problem's location, type and message and never echoes what
  you posted.
- The body is read after the token is checked and may not exceed 64 KiB: more is
  a 413. A request without a valid token gets the 401 whatever its body.
- Each item is judged on its own and the response is 200:
  `accepted`, `unchanged` (the same duration posted again) or `rejected` with a
  reason: `unknown_request`, `expired`, `conflict` (a different duration for an
  answered request) or `not_due` (see below).
- `not_due` means the request's start time has come round since it was served,
  so Home Assistant would have priced the following day. Nothing is stored and
  the request stays pending; it is served again in its next due window. Post
  results soon after each poll: one that arrives after the start time is refused
  even if the right day was priced.
- Only `request_id` and `duration_seconds` are read. Any weekday or endpoint you
  add is ignored: the app already knows them from its own request row.
- 503 with `Retry-After` means the database was busy (another writer held the
  lock); nothing was stored. Post again later. A 503 without `Retry-After` is a
  store error that waiting will not cure; check the app's logs.

## Example automation

Placeholders only: replace `fl.example.invalid`, the config entry id and the
secret. Find the `google_travel_time` config entry id in Home Assistant (the
integration page, or Developer tools).

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
              config_entry_id: "0123456789ABCDEFGHIJKLMNOP"
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
- The response shape above was verified on Home Assistant Core 2026.10.x. Check
  it again after a Home Assistant upgrade; the app's strict validation makes a
  changed shape fail visibly.
- Editing a zone's geometry inside Home Assistant (same entity id) does not
  invalidate the cache: the app only sees the entity id. After such an edit,
  wait out the affected entries (28 days) or clear them. There is no admin
  command for this yet; change the location's zone id or verification state
  (which fires the invalidation trigger) or delete that location's rows from
  `route_cache` in the database.

## Deployment checklist

None of the checks below has been done yet; until they pass, nothing here is
verified.

1. Set `FL_HA_TOKEN`, `FL_AUDIT_KEY` and, if any location has no zone,
   `FL_DATA_KEY`. Confirm both routes return 401 without the token.
2. Give each location that is a routing endpoint a Home Assistant zone, so no
   address is sent. This is required for every home of another family (for
   example Driver S's or Passenger E's): without a zone the app withholds it.
   For Passenger E and Passenger R, register a contact from the household with
   the home as `home_location_id`, or give the home a zone, before enabling
   `ha_relay`.
3. Confirm Home Assistant's time zone equals `FL_TIMEZONE` (Home Assistant
   Settings, General, time zone). `departure_time` is a bare local time that Home
   Assistant reads in its own zone, so a mismatch silently files results under
   the wrong bucket or weekday.
4. **Check that the price depends on `departure_time` at all.** Run the same
   `google_travel_time` request (a leg that is slow in the morning) with
   `departure_time` `03:00:00` and again with `08:00:00`, and compare `duration`
   with each other and with the static (traffic-unaware) duration. If the
   duration equals the static duration at every time, the integration returns no
   time-dependent signal and the whole weekday-and-bucket model carries none: do
   not use the results, and record that in the deployment repository.
5. **Only if step 4 shows a difference, compare the same request in the evening
   and again before 08:00. Matching durations mean Home Assistant resolves a
   bare time-of-day to the next occurrence.** Matching durations alone, without
   step 4, would also come from a traffic-unaware price. Different durations mean
   it prices "now": do not use the results, and rework the request shape. Record
   the outcome in the deployment repository.
6. Keep notifications in shadow mode until steps 3 to 5 have passed.
7. Check one full cycle: a run that needs a leg is incomplete (ROUTE-010), the
   next poll lists it, the post is accepted, and the following run is clear.
8. Decide first route versus slowest route (open owner decision).
