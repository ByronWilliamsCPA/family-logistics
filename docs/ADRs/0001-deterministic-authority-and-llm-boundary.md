---
schema_type: planning
title: "ADR-001: Deterministic Engine Is the Sole Validation Authority"
description: "Rule-based validation alone decides pass or fail; the LLM only explains findings, and a failed or incomplete run is never shown as clear."
tags:
  - architecture
  - decisions
  - adr
  - validation
  - llm
status: published
owner: core-maintainer
component: Strategy
source: "Family Logistics development handoff v1.0 (2026-10-21), sections 1, 7 and 9"
purpose: "Fix the authority boundary between deterministic validation and the local LLM, and the run-status semantics that keep incomplete analysis from looking safe."
---

> **Status**: `published` (accepted 2026-10-25)

**Decision Date**: 2026-10-21
**Deciders**: Byron Williams (owner), Riley (co-approver for family-facing behavior)
**Affected Components**: domain/validation, services/assessments, services/notifications, adapters/llama_cpp
**Implementation Target**: M2 (engine and statuses), M5 (narrative)

## Context

### Problem Statement

Family Logistics exists to find missing or conflicting rides before a child is
stranded, including children from other families. Two failure modes are worse
than useless:

- a **false green**: the dashboard says "clear" when the analysis could not run,
  could not reach iCloud, or could not route a trip;
- a **soft override**: a language model rewords, drops or downgrades a finding,
  so a real gap disappears from the report.

The deployment targets a self-hosted local model behind an OpenAI-compatible
endpoint. Such models may emit reasoning text and are not guaranteed to return
valid JSON.

### Requirements

- **Must have**: pass/fail is reproducible from inputs and rule versions alone.
- **Must have**: missing inputs produce `incomplete`, a runtime error produces
  `failed`; neither can ever render as `clear`.
- **Must have**: the full rule-based report is delivered when the LLM is down.
- **Should have**: plain-language explanation and suggested actions when the
  LLM is available.

## Decision

> **We will make the deterministic rule engine the only component that creates,
> resolves, or sets the severity of a finding, and treat the LLM as an optional
> narrator over a read-only copy of those findings.**

### Run status

Each analysis run ends in exactly one status, evaluated in this order:

| Order | Condition | Status |
|-------|-----------|--------|
| 1 | Unhandled runtime error, or the run did not finish | `failed` |
| 2 | Any required input missing or stale (iCloud sync, HA snapshot, route estimate for an in-window trip) | `incomplete` |
| 3 | Any unresolved Critical or Warning finding | `attention_required` |
| 4 | None of the above | `clear` |

`incomplete` wins over `attention_required` for the run status so a partially
checked week is never summarised as "only two warnings". Severity is stored per
finding and is independent of run status: an `incomplete` run still lists every
Critical it did find.

Findings carry deterministic, stable `issue_id`s (hash of rule ID plus the
affected entity keys) and the lifecycle `new`, `ongoing`, `resolved`,
`regressed`.

### LLM contract

- Input: compact structured facts and finding records, external minors as
  pseudonyms only (see the privacy spec), no addresses.
- Output: JSON with `executive_summary`, `prioritized_findings`,
  `suggested_actions`, `assumptions`, `uncertainties`, validated against a
  schema; token and wall-clock caps.
- The narrative is rendered in a separate, labelled section. Finding counts,
  severities and statuses in every report, push and HA card come from the
  engine, never from the narrative.
- LLM unavailable, timed out, or schema-invalid: the report ships with a
  `narrative_unavailable` marker. This is not a run failure.
- Notifications carry engine-decided findings, counts and statuses, rendered
  from templates ([ADR 0008](0008-notification-service.md)), through the dedicated
  `fl-*` Apprise tags (never bare severity tags); a `failed` or `incomplete`
  run is never announced as clear on any tag.
- `FL_LLM_ENABLED=false` by default. Enabling it is an M5 task, gated on the
  local inference server's model route being reachable from the app.

## Alternatives Considered

### Alternative 1: LLM triage of findings

Let the model merge, re-rank or suppress "noise". Rejected: it can silently
remove the one finding that matters, and its output is not reproducible.

### Alternative 2: Collapse `incomplete` into `attention_required`

Simpler UI. Rejected: "two warnings" and "could not check Thursday" call for
different actions, and the handoff requires the distinction explicitly.

## Consequences

### Positive

1. A green card means every in-window trip was checked against fresh data.
2. The family workflow keeps working through M1 to M4 with no LLM at all.
3. Malformed model output cannot alter a finding (M5 gate: failure injection).

### Negative

1. Rule coverage gaps are visible as `incomplete` more often, especially early.
2. The narrative can contradict the findings in wording; mitigated by
   rendering it separately and validating that referenced issue IDs exist.

## Implementation

- `domain/validation` returns `Finding` records; `services/assessments` computes
  run status with the table above; unit tests cover every row.
- `adapters/llama_cpp` receives an immutable DTO and returns a `Narrative | None`.
- Scenario tests: "iCloud unavailable", "LLM unavailable", "route service
  unavailable" in the acceptance scenario matrix
  (`tests/scenarios/test_acceptance_matrix.py`).

## References

- Site-specific requirements and the defaults register are kept in the private deployment repository.
- [privacy-and-retention-spec.md](../privacy/privacy-and-retention-spec.md)
