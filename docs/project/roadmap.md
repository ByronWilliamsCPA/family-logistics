---
title: "Roadmap"
schema_type: common
status: published
owner: core-maintainer
purpose: "Milestone roadmap for Family Logistics."
tags:
  - project
  - roadmap
---

Family Logistics is delivered in milestones. Each milestone has an exit gate;
transportation does not wait on later phases such as meal planning.

| Milestone | Scope | Exit gate |
|-----------|-------|-----------|
| M0 Discovery | Read-only CalDAV probe, sanitized fixtures, Home Assistant inventory, iOS Shortcuts protocol, egress, single sign-on, notification and routing checks ([M0 kit](https://github.com/ByronWilliamsCPA/family-logistics/blob/main/tools/m0/README.md)) | All eight checks recorded |
| M1 Ingestion and parser | CalDAV collector, normalized schema, registry, migrations, notes parser | Fixtures pass; zero write calls to iCloud |
| M2 Timeline and engine | School overlays, location continuity, external obligations, driver conflicts, issue lifecycle | Acceptance matrix passes |
| M3 Routing and notifications | Route providers, Apprise channels, deduplication, weekly and daily scheduling, ops alerts | Live dry-run reports in shadow mode |
| M4 Home Assistant UI | Status API, family-wide validation card, registry UI, on-demand runs | Both parents use the calendar as the only ride editor |
| M5 LLM and hardening | Narrative explanations, privacy controls, failure injection, backup and restore drills | Deterministic findings unaffected by malformed LLM output |
| M6 Meals (phase 2) | Meal rotation, cooking windows, shopping review | Estimated separately |

Done so far (M0): domain models, the Template v1 notes parser,
occurrence-bound confirmation codes, passenger-level change impact, run
status, capacity, constrained stop ordering, address encryption at rest and
the read-only CalDAV transport.
