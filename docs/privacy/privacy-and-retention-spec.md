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
| External passenger | passenger_id (`ext-<random>`), display_name, household_id or linked contact, default pickup location, default drop-off location, optional alternate destinations, aliases, commitments and their confirmation state, active, inactive_since (the recorded carpool end date) | R | Display name only (first name or nickname), no surname, birthdate or photo. |
| Household (external) | household_id, label, contacts (optional), privacy scope | R | Label is a nickname chosen by the parents (for example "Household E"). Contacts optional and only if a parent decides they are needed. |
| Driver (family or external contact) | contact_id, display_name, household_id, aliases, driver_eligible flag | C | Name only; phone or email only if notifications to them are ever added (not in v1). Children are never drivers by appearing in an event; eligibility is a registry flag. |
| Vehicle | vehicle_id, label, seat capacity (driver included), capacity_verified | C | No VIN or plate. |
| Location | location_id, display name (named location), HA zone ID, address, lat/lon, aliases, verification state | R for external homes, C otherwise | Real addresses are stored **only where routing needs them**, held protected (section 5); everywhere else the named location is shown. Coordinates rounded to 4 decimals (about 11 m) unless a pickup point needs more. |
| Source event | calendar_id, UID, recurrence_id, etag, raw hash, start/end, summary, location, description, last_seen | R (raw notes) | The raw text (`description` and `location`) is kept for 30 days after the event ends or is deleted, then cleared; the `summary`, the normalised fields and the raw hash persist (section 7). |
| Trip and trip passenger | trip_id, type, status, driver, origin, destinations, constraints, sequence | C (R where an external passenger or external address is involved) | Destinations stored as location IDs, not free-text addresses. |
| Finding | issue_id, rule_id, severity, affected IDs, first/last seen, resolved_at, evidence | C | Evidence references IDs, not raw notes text. |
| Analysis run | run_id, trigger, window, freshness, rules version, state, counts, output | I (output C) | Report HTML stored as an artifact with run-log retention. |
| Audit | timestamp, actor, object, prior hash, new hash, hash key version, action, correlation ID | I | Hashes only, never content. The hashes are HMAC-SHA256 of a canonical state under `FL_AUDIT_KEY` (kept in the secret store, never in the database), and each row records the key version. Holding the table alone does not let anyone confirm a guessed state; whoever also holds the key can, so rows stay pseudonymous, not anonymous. The table is append-only: the database rejects updates, overwrites and deletes of rows younger than 365 days. |
| HA snapshot | parsed school days (person, date, location, start, end, kind), fetched_at, received_at, content hash, day count | C | Event text, descriptions, calendar entity IDs and zones are checked or classified on arrival and never stored or logged. Only the configured school calendars and zones are accepted; no person-tracker or device-location entities are ever accepted. |
| Route cache | endpoints (location IDs), weekday and 15-minute bucket, duration, source, age | C | Keyed by location ID, weekday and bucket, not address. |

Not collected, ever: GPS or device location of children, phone numbers of
minors, school records, health data, photos, Riley's unrelated Google work
events, contact lists.

## 4. Where each kind of data may appear

Legend: **Y** allowed, **M** allowed masked, **P** pseudonym only, **N** never.

| Data | SQLite DB | App logs | Push notifications | Email report | HA card / HA API | LLM payload | Git (code, tests, docs) |
|------|-----------|----------|------|-------|---------|-----|-----|
| Family child first name | Y | Y (ID preferred) | Y | Y | Y | Y | N (fictional examples only) |
| External child first name | Y | N (ID only) | M (initial) | Y | M (initial) | P | P |
| External household address | Y (protected, section 5; only where routing needs it) | N | N | N (named location only) | N for a contact's home (only its `ha_zone_entity_id`; with no zone the address is withheld and the leg is incomplete, ROUTE-010); a home recorded only as a passenger's default pickup or drop-off is treated as a venue and may be sent (section 5) | N | N |
| Family home address | Y | N | N | N (shown as "Home") | N ("Home"), except the `ha_relay` route-request fallback for a location with no `ha_zone_entity_id` (section 5) | N ("Home") | N |
| Activity venue name | Y | Y | Y | Y | Y | Y | Y (public venues only) |
| Ride times and driver | Y | Y | Y | Y | Y | Y | Fixtures only, synthetic |
| Raw calendar notes | Y (until 30 days after the event ends or is deleted) | N | N | N | N | N | Sanitized fixtures only (M0 check 2) |
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

- External household addresses and home coordinates are stored as Fernet
  ciphertext (application-level encryption; Fernet is AES-128-CBC with an
  HMAC-SHA256 authentication tag, not AES-GCM) in the
  `address_ciphertext` and `latlon_ciphertext` columns of the `location` table,
  using a key supplied at runtime from the secret store (`FL_DATA_KEY`, from the
  deployment's secret store). The other columns on `location` (name,
  `ha_zone_entity_id`, `verification_state`) are not sensitive. Losing the key
  loses only the ciphertext fields; they are re-entered from the registry UI.
- The schema also guards these two columns. Each uses a column type that accepts
  only Fernet-shaped tokens, and a CHECK constraint on each requires a blob that
  begins with a Fernet token prefix. These checks guard against an accidental
  plaintext write; they do not prove a value is encrypted, because only the key
  can show that.
- Routing uses location IDs internally; plaintext addresses are decrypted only
  to build an outgoing route request (a Google Routes call, or a zone-less
  fallback endpoint in the `ha_relay` route-request payload below) and in the
  registry UI.
- Outside the app, only the routing path receives a place: Google Routes
  directly, or, through the `ha_relay` route-request endpoint (the
  token-authenticated HA-only route that lists the legs HA must estimate), Home
  Assistant, whose Waze action passes each endpoint on to Waze (a zone is
  resolved by Home Assistant to its coordinates). Only the two endpoints of a
  leg are sent, never names or ride purpose.
- `ha_relay` leg endpoints follow the owner decision of 2026-10-25 (ADR 0006),
  **other families' homes need a zone**, enforced by
  `family_logistics.domain.address_release.ha_relay_endpoint` and
  `is_other_family_home`:
  - a location with an `ha_zone_entity_id` is sent as that entity id, never as
    an address;
  - another family's home with no zone is withheld: its address never leaves
    the app, the leg's travel time is unknown and any ride that needs it is
    `incomplete` (ROUTE-010), never clear. Another family's home is the
    `home_location_id` of any contact (Driver S), even one shared with a
    household member or belonging to an inactive contact. Give the location a
    Home Assistant zone to have it estimated;
  - any other location with no zone (the household's own home, a venue such as
    a school or a music studio, or an external passenger's default pickup or
    drop-off) is sent as its decrypted address. **This means plaintext
    family-home and venue addresses can reach Home Assistant, and so can an
    external passenger's home when it is recorded only as that child's
    default.** To protect Passenger E's or Passenger R's home, register a
    contact from that household with the home as its `home_location_id`, or
    give the home a Home Assistant zone, before enabling `ha_relay`.
- Once an endpoint reaches Home Assistant it is outside this app's encryption
  and retention: Home Assistant may keep it in automation traces, the logbook,
  recorder history and its backups, and Waze receives it to compute the route.
  The deployment excludes the relay automation from the recorder or shortens
  its trace retention (runbook in the private deployment repository).
  `#VERIFY` Home Assistant's trace, logbook and recorder behaviour for the
  relay automation against the deployed Home Assistant version before enabling
  `ha_relay`.
- The `ha_relay` route cache is keyed by location id and time bucket (weekday
  and 15-minute time of day), never by address, so the cache holds no address
  text.
- Logs pass through a redaction filter that drops fields tagged `R` and
  replaces external names with passenger IDs. A unit test asserts no
  address-shaped string reaches a log record.
- Masking in push: "Ride for E. at 3:15 needs a driver" rather than full name
  or destination.

> **Deviation from the original proposal.** The original proposal kept
> addresses and coordinates in a separate `protected_location` table. The
> approved implementation keeps the ciphertext columns on `location` instead.
> Protection is the same: each field is encrypted with the runtime key.
> SQLite has no column or table privileges, so a separate table would add no
> enforceable access control. Every other column on `location` is non-sensitive,
> because a venue name is allowed everywhere (section 4).

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
| Raw VEVENT text (`description` and `location` on `source_event`) | 30 days after the event is deleted, or after it ends if it is never deleted (`COALESCE(deleted_at, end_utc)`), not 30 days after last seen; the `summary`, the normalised fields and `raw_hash` persist | the database function `purge_raw_source_text` nulls those two fields and never deletes rows; the daily job that calls it arrives with the scheduler (M3), and until then the text is kept |
| Home Assistant school snapshot (parsed school days on `school_day`) | replaced by every ingest; days that ended more than 30 days ago are deleted (proposed); the raw payload, event text and zone list are never stored | the database function `purge_expired_school_days`; the daily job that calls it arrives with the scheduler (M3) |
| Analysis run logs and report artifacts | 90 days | daily purge job |
| Audit hashes | at least 365 days | the database refuses to delete a row younger than 365 days; the purge job for older rows arrives with the scheduler (M3), and until then rows are kept |
| Confirmed passenger legs after event deletion | 90 days after the trip date | supports change-impact comparison |
| Resolved findings | 90 days after resolution | |
| Inactive external passenger and household | removed or anonymised 180 days after the carpool ends (measured from the last trip, or a recorded carpool end date), or on request; audit hashes keep their own retention | registry review prompt at 165 days; the removal job arrives with the scheduler (M3), and until then rows are kept |
| Route cache entries | 28 days after the last observation for that weekday and 15-minute bucket, a fixed value rather than an `FL_` setting because it is also a correctness rule (changing it amends ADR 0006); also dropped at once when the location's address, coordinates or zone change, or the location is merged into another or re-verified (ADR 0006) | the purge and invalidation mechanism arrives with the `ha_relay` cache in PR 2 of the routing series, as migration `0005`; that number assumes migration `0004` from the school snapshot storage PR (#27) merges first, and until then there is no cache |
| Route endpoints held by Home Assistant | outside this app's control (automation traces, logbook, recorder history, HA backups) | Home Assistant configuration in the deployment: the relay automation is excluded from the recorder or has short trace retention (section 5) |
| Backups | 14 days rolling (proposed) | backup sidecar |

The inactive-passenger period is 180 days, not 90, because a carpool pauses
every summer (about 90 days) and a 90-day rule would remove active families each
summer. The review prompt keeps the original 15-day lead, so it fires at 165
days. A seasonal pause is not an end: the registry marks a passenger inactive
only on a deliberate action, and the clock runs from the last trip or a recorded
carpool end date. The database enforces that `active` and `inactive_since` agree
(inactive means a date is recorded, active means none), and deleting a
passenger, person or location removes its alias rows.

## 8. Other families

**Notice.** Morgan or Riley tells each external family whose child is entered
in the registry that the household uses a private scheduling system holding the
child's first name, the agreed pickup and drop-off places, and ride times, and
that it is deleted 180 days after the carpool ends or sooner on request. No
data about them is shared outside the household's systems, with one exception:
to estimate travel time, a routing service (Waze, through the household's Home
Assistant, or Google Routes) receives the location of a pickup or drop-off
place, never a name or the ride's purpose. Before `ha_relay` is enabled, their
home is registered as the home of a contact from their household or given a
Home Assistant zone; it is then sent to Home Assistant only as a zone, and
without a zone their rides are reported as incomplete rather than estimated.
Other pickup and drop-off places (a school, a club) may be sent as addresses.

If an external family asks what is held, or asks for removal:

- **Export**: the registry UI can produce a per-household summary (names as
  stored, destinations, upcoming rides) for a parent to share by hand.
- **Delete**: a hard-delete action removes the household, its passengers, the
  household's protected address data (the location's ciphertext fields are
  cleared, or the location is removed), route cache entries and future legs;
  audit rows keep only hashes. Deleted data ages out of backups within 14 days.
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
