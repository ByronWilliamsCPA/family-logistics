---
schema_type: common
title: "Privacy and Data-Retention Specification"
description: "How Family Logistics collects, protects, shares, retains and deletes data about minors and external families."
tags:
  - privacy
  - security
  - retention
  - compliance
status: draft
owner: core-maintainer
purpose: "Project-specific data handling rules for children's schedules, locations and other families' data, pending a repo-wide classification policy."
---

> **Status**: draft for owner approval (Morgan and Riley). Values marked
> *proposed* are configurable; the site's defaults register is kept in the
> private deployment repository.

## 1. Why this spec exists

Family Logistics stores where children will be, when, and who drives them,
including children from other families who never agreed to the system. Absent
a broader data-classification policy at the deploying site, this document is
the binding rule set for this application.

Principles: collect the minimum, keep it for the shortest useful time, never
send more than a channel needs, and treat other families' data as on loan.

## 2. Classification

| Class | Meaning | Examples |
|-------|---------|----------|
| **R (Restricted)** | Could locate or identify a specific minor, or another household | External household addresses, external child names linked to schedules, raw VEVENT notes, location coordinates of homes |
| **C (Confidential)** | Family-internal schedule and identity data | Family children's activities and rides, family home location, driver assignments, audit evidence |
| **I (Internal)** | Operational data without personal detail | Rule IDs, counts, run status, sync freshness, route cost counters |
| **S (Secret)** | Credentials | Apple app password, `FL_HA_TOKEN`, Google key, LLM key |

## 3. Data inventory

| Entity | Fields | Class | Minimisation rule |
|--------|--------|-------|-------------------|
| Person (family) | person_id, display_name, role, calendar_id, home_location_id, aliases, active, monitoring_enabled | C | First names only. Casey stored with `monitoring_enabled=false`. |
| External passenger | passenger_id (`ext-<random>`), display_name, household_id or linked contact, default pickup location, default drop-off location, optional alternate destinations, aliases, commitments and their confirmation state, active, carpool_end_date | R | Display name only (first name or nickname), no surname, birthdate or photo. |
| Household (external) | household_id, label, contacts (optional), privacy scope | R | Label is a nickname chosen by the parents (for example "Household E"). Contacts optional and only if a parent decides they are needed. |
| Driver (family or external contact) | contact_id, display_name, household_id, aliases, driver_eligible flag | C | Name only; phone or email only if notifications to them are ever added (not in v1). Children are never drivers by appearing in an event; eligibility is a registry flag. |
| Vehicle | vehicle_id, label, seat capacity (driver included), capacity_verified | C | No VIN or plate. |
| Location | location_id, display name (named location), HA zone ID, address, lat/lon, aliases, verification state | R for external homes, C otherwise | Real addresses are stored **only where routing needs them**, held protected (section 5); everywhere else the named location is shown. Coordinates rounded to 4 decimals (about 11 m) unless a pickup point needs more. |
| Source event | calendar_id, UID, recurrence_id, etag, raw hash, start/end, location, description, last_seen | R (raw notes) | Raw text kept only as a snapshot with 30-day retention; normalised fields persist. |
| Trip and trip passenger | trip_id, type, status, driver, origin, destinations, constraints, sequence | C (R where an external passenger or external address is involved) | Destinations stored as location IDs, not free-text addresses. |
| Finding | issue_id, rule_id, severity, affected IDs, first/last seen, resolved_at, evidence | C | Evidence references IDs, not raw notes text. |
| Analysis run | run_id, trigger, window, freshness, rules version, state, counts, output | I (output C) | Report HTML stored as an artifact with run-log retention. |
| Audit | timestamp, actor, object, prior hash, new hash, hash key version, action, correlation ID | I | Hashes only, never content. The hashes are HMAC-SHA256 of a canonical state under `FL_AUDIT_KEY` (kept in the secret store, never in the database), and each row records the key version. Holding the table alone does not let anyone confirm a guessed state; whoever also holds the key can, so rows stay pseudonymous, not anonymous. The table is append-only: the database rejects updates, overwrites and deletes of rows younger than 365 days. |
| HA snapshot | school/district events, zones, fetched_at | C | Only the configured school calendars and zones; no person-tracker or device-location entities are ever accepted. |
| Route cache | endpoints (location IDs), bucket, duration, source, age | C | Keyed by location ID, not address. |

Not collected, ever: GPS or device location of children, phone numbers of
minors, school records, health data, photos, Riley's unrelated Google work
events, contact lists.

## 4. Where each kind of data may appear

Legend: **Y** allowed, **M** allowed masked, **P** pseudonym only, **N** never.

| Data | SQLite DB | App logs | Push notifications | Email report | HA card / HA API | LLM payload | Git (code, tests, docs) |
|------|-----------|----------|------|-------|---------|-----|-----|
| Family child first name | Y | Y (ID preferred) | Y | Y | Y | Y | N (fictional examples only) |
| External child first name | Y | N (ID only) | M (initial) | Y | M (initial) | P | P |
| External household address | Y (protected, section 5; only where routing needs it) | N | N | N (named location only) | N | N | N |
| Family home address | Y | N | N | N (shown as "Home") | N ("Home") | N ("Home") | N |
| Activity venue name | Y | Y | Y | Y | Y | Y | Y (public venues only) |
| Ride times and driver | Y | Y | Y | Y | Y | Y | Fixtures only, synthetic |
| Raw calendar notes | Y (30 days) | N | N | N | N | N | Sanitized fixtures only (M0 check 2) |
| Calendar IDs, UIDs | Y | Hashed | N | N | N | N | N |
| Secrets | N (env only) | N | N | N | N | N | N |

### Notification channels

Family Logistics sends only to its six dedicated Apprise tags (`fl-report`,
`fl-status`, `fl-warning`, `fl-urgent`, `fl-ops`, `fl-shadow`), each routed in
the notification service's configuration to the parents' private channels.
Recipient routing lives in that configuration, never in the app. The app
**never** sends to shared or generic severity tags, because those may reach
channels (for example operations alerts or public topics) that must never
carry children's schedules. Signal and SMS are future options only.

## 5. Address protection

- External household addresses and home coordinates are stored in a separate
  `protected_location` table with application-level encryption (Fernet/AES-GCM)
  using a key supplied at runtime from the secret store (`FL_DATA_KEY`, from the
  deployment's secret store). Losing the key loses only those fields; they are re-entered from
  the registry UI.
- Routing uses location IDs internally; plaintext addresses are decrypted only
  in the routing adapter call and the registry UI.
- The routing provider is the only external party that receives an address:
  Google Routes directly, or Home Assistant's Waze action through the
  `ha_relay` route-request endpoint (which returns origin and destination for
  the legs HA must estimate, over the token-authenticated HA-only route). Only
  the two endpoints of a leg are sent, never names or ride purpose.
- Logs pass through a redaction filter that drops fields tagged `R` and
  replaces external names with passenger IDs. A unit test asserts no
  address-shaped string reaches a log record.
- Masking in push: "Ride for E. at 3:15 needs a driver" rather than full name
  or destination.

## 6. LLM pseudonymisation

Before any call to the local inference server:

1. External children become stable per-run pseudonyms (`Passenger E1`), family
   children keep first names.
2. Addresses are removed; locations become categories ("school", "music studio",
   "Household E home").
3. Only the analysis window's findings and schedule summary are sent; no raw
   notes.
4. The response is checked for pseudonyms only; it is never fed back as data.
5. LLM request and response bodies are not logged; only token counts, latency
   and outcome.

## 7. Retention (proposed, configurable)

| Data | Retention | Mechanism |
|------|-----------|-----------|
| Raw VEVENT snapshots | 30 days | daily purge job |
| Analysis run logs and report artifacts | 90 days | daily purge job |
| Audit hashes | at least 365 days | the database refuses to delete a row younger than 365 days; the purge job for older rows arrives with the scheduler (M3), and until then rows are kept |
| Confirmed passenger legs after event deletion | 90 days after the trip date | supports change-impact comparison |
| Resolved findings | 90 days after resolution | |
| Inactive external passenger and household | removed or anonymised 90 days after the carpool ends (last trip, or a recorded carpool end date), or on request; audit hashes keep their own retention | registry review prompt at 75 days |
| Backups | 14 days rolling (proposed) | backup sidecar |

## 8. Other families

**Notice.** Morgan or Riley tells each external family whose child is entered
in the registry that the household uses a private scheduling system holding the
child's first name, the agreed pickup and drop-off places, and ride times, and
that it is deleted 90 days after the carpool ends or sooner on request. No
data about them is shared outside the household's systems.

If an external family asks what is held, or asks for removal:

- **Export**: the registry UI can produce a per-household summary (names as
  stored, destinations, upcoming rides) for a parent to share by hand.
- **Delete**: a hard-delete action removes the household, its passengers,
  protected locations, route cache entries and future legs; audit rows keep
  only hashes. Deleted data ages out of backups within 14 days.
- Both actions are audited and require an admin role.

## 9. Access control

- Registry UI and reports: behind the deployment's single sign-on proxy, limited
  to an identity-provider group `family-logistics` (Morgan and Riley, both
  editors). Admin actions (registry edits,
  delete/export) require the same group plus a re-auth prompt if supported.
  `#VERIFY` both parents have identity-provider accounts (M0 check 6).
- HA routes: bearer token, reachable from the Home Assistant host only (ADR-007).
- Database file: readable only by UID 65532 on the Docker host; backups go to a
  restricted-permission location configured by the deployment.

## 10. Backups

Backups contain class R data. They stay on local storage inside the home
network, under a short rotation (proposed 14 days). Off-site copies, if ever added, must be encrypted
client-side. Restore tests run against a scratch path and are deleted after
verification.

## 11. Incidents

Treat as an incident: secret exposure, a log or push containing an external
address, unauthorised registry access, or a backup leaving the house.

1. Rotate the affected secret (Apple app password, `FL_HA_TOKEN`, `FL_DATA_KEY`, `FL_AUDIT_KEY`)
   and revoke the old one.
2. Purge the exposed artifact where possible (log, notification history).
3. If another family's data was exposed, Morgan or Riley tells that family.
4. Record the incident and fix in the repo's security log without the exposed
   data itself.

## 12. Git hygiene

This repository is **public**. Allowed in Git: the fictional example family
(Morgan, Riley, Alex, Sam, Jordan, Casey), pseudonymous external passengers,
generic venue names and synthetic data.

**Never in Git**:

- real names of any family member, adult or child, or of other families;

- addresses or coordinates of any home, school pickup point or private place;
- real calendar IDs, calendar URLs or event UIDs;
- credentials of any kind;
- external children's real names (pseudonyms only: `External Passenger E`, `ext-e`);
- real transportation histories (actual past rides, real exports not sanitised);
- contact information (phone numbers, email addresses) of anyone.

Rules:

- Tests and fixtures use synthetic data.
- Real calendar exports, even after M0 check 2 sanitisation, stay local or in
  the private deployment repository; they are never committed here.
- Secret scanning (gitleaks at pre-commit, and over the full history in the
  Private Data Guard CI workflow) is mandatory.
