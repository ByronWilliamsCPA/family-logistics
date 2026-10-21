---
title: "Overview"
schema_type: common
status: published
owner: core-maintainer
purpose: "Overview of what Family Logistics checks and the rules it never breaks."
tags:
  - guide
  - overview
---

Family Logistics answers one question for every upcoming day: does every child
have a feasible way to get from where they are to where they need to be?

## What it checks

- **Ride commitments** written as a versioned notes template on events in a
  dedicated iCloud transportation calendar (driver, passengers, pickup, a
  destination per passenger, status and a confirmation code).
- **Location continuity**: consecutive activities at the same place need no
  ride; every change of location needs a ride, a recorded transport mode (bus,
  bike, walk) or an explicit exemption, and the last activity of the day needs
  a way home.
- **Passenger-level obligations**: each passenger's leg is tracked on its own,
  so changing one child's plan never removes another child's ride. A confirmed
  ride for a child from another family that disappears without replacement is
  a critical finding.
- **Drivers and timing**: double-booked drivers, unauthorized drivers, seat
  capacity and travel time against arrival deadlines.

## Rules it never breaks

- iCloud is the authoritative record; the application never writes to it.
- The deterministic engine decides pass or fail. A language model may explain
  findings but cannot create, dismiss or downgrade them.
- A failed or incomplete analysis is never reported as clear.

## Run statuses

| Status | Meaning |
|--------|---------|
| `clear` | All inputs fresh and no open warning or critical findings |
| `attention_required` | At least one open warning or critical finding |
| `incomplete` | Required inputs missing or stale; never shown as clear |
| `failed` | The run itself errored |

See the [architecture decisions](../ADRs/README.md) for the reasoning behind
each rule.
