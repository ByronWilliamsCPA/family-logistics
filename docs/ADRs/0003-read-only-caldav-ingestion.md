---
schema_type: planning
title: "ADR-003: Read-Only CalDAV Ingestion with Occurrence Identity"
description: "The collector reads iCloud over CalDAV through a method allow-list, keys occurrences by calendar, UID and RECURRENCE-ID, and never treats a failed sync as an empty calendar."
tags:
  - architecture
  - decisions
  - adr
  - caldav
  - security
status: draft
owner: core-maintainer
component: Strategy
source: "Family Logistics development handoff v1.0, sections 2, 4.2 and 6"
purpose: "Guarantee the app cannot modify iCloud calendars and that recurring rides are tracked per occurrence."
---

> **Status**: `proposed`

**Decision Date**: 2026-10-21
**Deciders**: Byron Williams
**Affected Components**: adapters/caldav, services/collector, domain/recurrence
**Implementation Target**: M1

## Context

An iCloud app-specific password separates credentials but does **not** make
CalDAV read-only: it authenticates as the full Apple ID, with whatever write
access that account holds to every calendar (and other DAV data). Rides recur
weekly, with per-occurrence exceptions ("Jordan absent one Tuesday"). A network
failure that is read as "no events" would erase every obligation in the
analysis and could show green.

## Decision

1. **Method allow-list in code.** The CalDAV HTTP client is wrapped so only
   `PROPFIND`, `REPORT`, `GET` and `OPTIONS` can be sent. Any other method
   raises `WriteAttemptBlocked` before any network I/O. Unit tests assert that
   `PUT`, `DELETE`, `MOVE`, `COPY`, `POST`, `PROPPATCH`, `MKCALENDAR`, `LOCK`
   and `UNLOCK` are refused. M1 gate: zero write calls to iCloud.
2. **Server-enforced read-only (accepted by the planning team, 2026-10-21).**
   The calendars are owned by Morgan's Apple ID and are already ingested into
   Home Assistant through its CalDAV integration. Create a dedicated Apple ID used
   only by Family Logistics. Each parent shares the needed calendars with it as
   **View Only**. The app password belongs to that account, so iCloud itself
   refuses writes and a leaked password exposes only shared, view-only
   calendars. `#VERIFY` in M0 check 1 (`--check-write-privilege` reads
   `current-user-privilege-set`; it never attempts a write). It is called
   server-enforced only after M0 confirms, through the dedicated account, that
   notes, recurrence rules, overridden occurrences and change identifiers
   (ETag, sync token) are all visible. If iCloud's
   view-only shares do not expose notes or recurrence data over CalDAV, fall
   back to a parent's Apple ID with the method allow-list alone and record that
   residual risk.
3. **Occurrence identity.** Source key = `(calendar_id, UID, RECURRENCE-ID or
   master)`. Recurrence expansion happens locally (RFC 5545), applying
   `EXDATE`, overridden instances and moved occurrences, so a moved occurrence
   updates one ride and never duplicates it. Time zones are resolved from
   `TZID`; storage is UTC.
4. **Synchronisation.** Use `sync-collection` (RFC 6578) with stored sync tokens
   and per-resource ETags where the server supports them; otherwise, or on
   token invalidation, perform a full-window `calendar-query` REPORT. Default
   window: 2 days back, 14 days ahead; weekly runs extend to the next
   Monday to Sunday.
5. **Failure is not emptiness.** A sync attempt that errors, times out or is
   throttled marks that calendar's freshness as stale and raises DATA-011;
   previously stored events stay in place and the run is `incomplete`. A
   calendar that returns zero events is accepted as empty only after a
   successful, complete response. Retries use exponential backoff and honour
   `Retry-After`.
6. **Raw retention.** Raw VEVENT snapshots are kept for 30 days (configurable)
   for diagnosis, then deleted; normalised records persist.

## Alternatives Considered

| Option | Assessment |
|--------|------------|
| HA `calendar.get_events` as the source | Lacks UID, RECURRENCE-ID and ETag; fine for a proof of concept, not for change tracking. Rejected for rides. |
| Rely on the app password alone | Credential separation only; no write protection. Rejected. |
| Public iCloud calendar links (webcal) | Read-only, but publishes the family schedule to anyone with the URL and may omit notes. Rejected. |

## Consequences

- Writes are impossible in code and, with the dedicated account, on the server.
- One more Apple ID to manage, and each calendar share must be accepted.
- Calendar IDs are discovered at runtime or configured through the secret
  store or stack env; none are committed to source.
- Stale data is surfaced as `incomplete` rather than hidden.

## References

- RFC 4791 (CalDAV), RFC 5545 (iCalendar), RFC 6578 (WebDAV sync)
- [tools/m0/README.md](../../tools/m0/README.md), check 1
- [ADR-001](0001-deterministic-authority-and-llm-boundary.md)
