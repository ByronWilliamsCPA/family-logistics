---
schema_type: planning
title: "ADR-004: Passenger-Level Commitments and Change-Impact Analysis"
description: "Each passenger's ride leg is an independent obligation, so a change to one passenger can never silently remove another passenger's ride."
tags:
  - architecture
  - decisions
  - adr
  - domain_model
status: draft
owner: core-maintainer
component: Strategy
source: "Family Logistics development handoff v1.0, sections 3.3 and 16; planning response (Friday regression)"
purpose: "Make external children's rides survive edits to family children's plans, and alert when a confirmed commitment disappears."
---

> **Status**: `proposed`

**Decision Date**: 2026-10-21
**Deciders**: Byron Williams
**Affected Components**: domain/models, domain/dependency_graph, domain/validation (PASS-003)
**Implementation Target**: M1 (model, diff), M2 (rule and alerting)

## Context

The planning team's "Friday rule": Sam and External Passenger E share a
Friday ride. Sam stays late and a teacher drives them home. Someone edits or
deletes the Friday event. External Passenger E's ride is still owed. If the
system treats the event as the unit of obligation, removing the event (or
removing Sam from it) can erase External Passenger E's commitment without
any alert.

## Decision

> **We will model the obligation as the passenger leg, not the event.**

- Commitment key: `(source calendar, UID, RECURRENCE-ID, passenger_id)`.
- Each analysis computes the set of passenger legs from the current calendar
  state and compares it with the previous **confirmed** legs (retained in the
  database even after the source event disappears).
- Change classes per leg: `unchanged`, `modified` (driver, origin, destination
  or time), `moved` (same passenger and purpose, new event key: a replacement),
  `removed`.
- A removed leg with a replacement (same passenger, overlapping obligation
  window, new event) is `moved`, not lost.
- A **confirmed external passenger leg removed with no replacement** raises
  **PASS-003 Critical** to both parents, with the prior commitment as evidence.
  A removed family passenger leg raises RIDE-005 if the location timeline still
  needs a transition; otherwise it is informational.
- Removing one passenger from a shared event affects only that passenger's leg.
  Other legs on the same event stay `unchanged`.
- The Friday scenario is a permanent automated regression test
  (`tests/scenarios`), and must never be skipped or marked expected-fail.

## Alternatives Considered

| Option | Assessment |
|--------|------------|
| Event-level diff | Simple, but cannot tell "Sam removed" from "ride removed". Rejected. |
| Trust the current calendar only | No memory of prior commitments, so deletions are invisible. Rejected. |

## Consequences

- External children remain obligations independent of family plan changes.
- The database must retain confirmed legs past event deletion (bounded by the
  retention policy) and match replacements, adding modest complexity.

## References

- [Acceptance scenario matrix](../../tests/scenarios/test_acceptance_matrix.py)
- [ADR-002](0002-event-contract-and-confirmation-code.md)
