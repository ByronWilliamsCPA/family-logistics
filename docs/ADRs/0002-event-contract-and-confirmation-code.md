---
schema_type: planning
title: "ADR-002: iCloud Event Contract v1 and Confirmation-Code Revisions"
description: "Ride events carry a versioned key-value block in Apple Calendar notes, and a confirmation is valid only while its code matches the ride's material content."
tags:
  - architecture
  - decisions
  - adr
  - calendar
  - data_contract
status: draft
owner: core-maintainer
component: Strategy
source: "Family Logistics development handoff v1.0, section 4; planning response item 11"
purpose: "Define the human-editable ride format and a confirmation mechanism that tells a fresh confirmation apart from an unchanged status field, without the app writing to iCloud."
---

> **Status**: `proposed`. The confirmation-code mechanism was **conditionally
> approved by the planning team on 2026-10-21**, subject to the conditions in
> "Approval conditions" below; final enforcement waits on M0 check 4 and on sign-off of the defaults
> register, which is kept in the private deployment repository.

**Decision Date**: 2026-10-21
**Deciders**: Byron Williams
**Affected Components**: domain/parser, domain/validation (CONF-008), web UI, iOS Shortcuts
**Implementation Target**: M1 (parser), M2 (CONF-008), M4 (Shortcut)

## Context

Apple iCloud calendars are the authoritative, human-editable record of rides.
Parents edit on iPhone. The app must never write to iCloud. A ride carries
`STATUS: tentative | confirmed`, but a status field alone cannot show whether a
confirmation was given **before or after** the driver, passengers, pickup,
destinations or times last changed: editing the driver leaves `STATUS:
confirmed` in place. The handoff requires that material changes invalidate a
prior confirmation unless a human explicitly reconfirms in iCloud.

## Decision

### Contract v1

Calendar: `Family Transportation` only. Event start/end is the planned transport
window (`America/Los_Angeles`, stored as UTC). Title is human-readable and not
parsed (recommended `Ride | <driver> | <purpose>`). Apple Calendar notes hold:

```text
FL_VERSION: 1
TRIP_TYPE: pickup | drop off | transfer
STATUS: tentative | confirmed
DRIVER: <registered driver or contact alias>
FAMILY_PASSENGERS: <comma list of family children>
EXTERNAL_PASSENGERS: <comma list of first-name labels>
PICKUP: <location alias or address>
DESTINATIONS:
  <passenger>: HOME | DEFAULT | <location alias> | <label> @ <address>
STOP_ORDER: AUTO | <comma list of passengers or destinations>
CONFIRM: <6-character code>        # optional; see below
NOTES: <free text, preserved>
```

Parser rules follow handoff section 4.2: case-insensitive labels, Unicode and
line-ending normalisation, exact registry names for family children (fuzzy match
only to *suggest*), external names resolved to stable IDs, rejection of duplicate
keys, unsupported versions, unknown enums, duplicate passengers, mismatched
destination rows and impossible stop orders, and no silent ignoring of malformed
notes (PARSE-001, NAME-002, EXT-013, ORDER-012).

### Confirmation code (proposed)

```text
material = canonical JSON of {
  occurrence: UID + (RECURRENCE-ID, or the occurrence start for an unmodified instance),
  start, end, driver_id,
  passengers: sorted [(passenger_id, resolved destination key)],
  pickup location key, stop_order
}
code = upper(hex(sha256(material)))[0:6]
```

The occurrence identity is calendar-agnostic (no calendar ID), so moving the
event between calendars does not change the code, while a code copied to a
different occurrence never matches.

- `STATUS: confirmed` is honoured only when `CONFIRM:` equals the code of the
  ride as it now stands.
- Missing or stale code on a `confirmed` ride raises **CONF-008** (Warning:
  "changed since confirmation"), naming the fields that differ from the last
  confirmed revision the app recorded.
- In the normal flow parents never compute or type a code: the "Confirm Ride"
  iOS Shortcut computes it with Shortcuts' *Generate Hash* (SHA-256) and writes
  the `CONFIRM:` line into the event notes. The app also shows the expected code
  next to each ride in the report and registry UI for the documented manual
  fallback (copy and paste), used only if the Shortcut proves unreliable.
- The app stores each confirmed revision (fingerprint, fields, first seen) for
  change-impact comparison and audit, but never rewrites the calendar.
- Six hex characters (24 bits) prevent accidental reuse; it is not a security
  control.

### Approval conditions (planning team, 2026-10-21)

1. **Shortcut writes the line.** Parents do not compute or type codes in the
   normal flow; a manual fallback is documented. A ride without a valid code is
   CONF-008.
2. **Occurrence binding.** The fingerprint includes the occurrence identity and
   all material fields listed above.
3. **Recurring series.** A `CONFIRM:` on the series master confirms only
   unmodified occurrences whose material fields equal the master's. Any
   overridden occurrence (one with a `RECURRENCE-ID` override) needs its own
   `CONFIRM:`. A series edit never reconfirms modified future occurrences.
   For the series master the occurrence component is `UID + "SERIES"`, and the
   material fields are the master's own; the app then checks that each
   unmodified occurrence's material fields equal the master's.
   `#VERIFY` the exact master-code rule with M0 check 2 fixtures before M1 locks
   the algorithm.
4. **History is immutable.** Past occurrences keep the confirmation state they
   had when they happened (snapshot in the audit log); it is never recomputed
   after a later edit or a rule change.
5. **Recorded confirmation is not driver acceptance.** The model holds two
   fields: `recorded_confirmation` (a parent marked it, validated by code) and
   `driver_acceptance`, which v1 always sets to `unverified`. Reports say
   "confirmed in calendar", never "driver accepted".

## Alternatives Considered

| Option | Assessment |
|--------|------------|
| **Timestamp field** (`CONFIRMED_AT: 2026-10-21 19:05`) | Easy to type, but the app cannot prove the timestamp is later than the material edit: iCloud `LAST-MODIFIED` also changes when the timestamp itself is added, and polling hourly blurs ordering. Rejected. |
| **Separate `CONFIRMED_BY:` field** | Records who, not what. A driver change after confirmation leaves it valid. Could be added alongside the code later. |
| **App-side acknowledgement** (button in registry UI or HA) | Clean UX, but moves authority out of iCloud, contradicting the "calendar notes remain authoritative" decision. Kept as a possible later convenience that only *displays* the code. |
| **Status field only** (handoff baseline) | Cannot distinguish a fresh confirmation from an untouched field. Rejected. |
| **Confirmation code** | Chosen (proposed). Deterministic, needs no iCloud write, works offline on iPhone via Shortcuts. Cost: one extra notes line and a short learning step. |

## Consequences

- Positive: material changes are caught even when nobody touches `STATUS`.
- Negative: a human must re-enter a code after every material edit; without the
  Shortcut this is friction. M0 check 4 verifies the Shortcut path.
- Neutral: a future `FL_VERSION: 2` can add `TRANSPORT_MODE` without breaking v1.

## Assumptions

- `#ASSUME` iOS Shortcuts exposes SHA-256 hashing on both parents' devices.
  `#VERIFY` M0 check 4.
- `#ASSUME` iCloud preserves notes text byte-for-byte across devices.
  `#VERIFY` M0 checks 1 and 2 (round-trip of a sample event).

## References

- Site-specific requirements and the defaults register are kept in the private deployment repository.
- [tools/m0/check4_shortcuts_protocol.md](../../tools/m0/check4_shortcuts_protocol.md)
