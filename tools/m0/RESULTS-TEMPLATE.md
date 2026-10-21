# M0 Results

> Copy this file outside the repository, fill it in, and review it before
> committing any part of it. Record **yes/no answers, counts, entity IDs and
> configuration choices only**. Never record passwords, tokens, keys, calendar
> URLs or IDs, event UIDs, addresses, coordinates, e-mail addresses, phone
> numbers, notification topic names, real family names or external children's
> real names. Completed results never go into this public repository.

| Field | Value |
|-------|-------|
| Date(s) run | |
| Run by | |
| Kit version (git commit) | |

## 1. Dedicated Apple ID, View Only CalDAV

| Item | Result |
|------|--------|
| Dedicated Apple ID created, 2FA on | |
| Calendars shared View Only and accepted (count, display names) | |
| `--self-test` blocked all write methods | |
| Calendars visible to the probe (count) | |
| Server-side posture read-only on all calendars | |
| DESCRIPTION visible (events with notes / total) | |
| RRULE masters visible | |
| RECURRENCE-ID overrides visible | |
| ETags on all events | |
| sync-collection supported / initial token returned | |
| TZIDs seen | |
| Verdict: server-enforced read-only? | |

## 2. Calendar inventory, recurrence and notes

| Item | Result |
|------|--------|
| Calendar inventory (display names and owner role only) | |
| Notes block survived iPhone, Mac and iCloud round trip unchanged | |
| Removing one passenger from one occurrence produced an override (RECURRENCE-ID) | |
| Moving one occurrence produced an override, not a new event | |
| Misspelled-name event exported | |
| Fixtures written (count) and reviewed by | |
| Residual-scan refusals and how resolved | |

## 3. Home Assistant school schedules and locations

| Item | Result |
|------|--------|
| `calendar.family_calendar` integration | |
| `calendar.school_district` integration | |
| `calendar.school_year` integration | |
| Bell schedule representation per school (entity IDs, helper type) | |
| Weekday variation encoding | |
| District closure / short-day encoding | |
| Zones present (names only) and coordinates present | |
| Waze action available | |
| Short-lived token deleted (time) | |

## 4. iOS Shortcuts

Attach the completed tables from [check4_shortcuts_protocol.md](check4_shortcuts_protocol.md).

| Item | iPhone 1 | iPhone 2 |
|------|----------|----------|
| Ride creation in shared calendar | | |
| Prefilled native editor available | | |
| SHA-256 Generate Hash available | | |
| Code matches reference | | |
| Shortcut writes `CONFIRM:` without typing | | |
| Series and override behavior as expected | | |
| Manual fallback works | | |

## 5. Docker host egress

| Target | Host | Container (network) |
|--------|------|---------------------|
| iCloud CalDAV | | |
| Google Routes | | |
| Apprise | | |
| Local inference server (expected unreachable) | | |

## 6. Authentik

| Item | Result |
|------|--------|
| Morgan account active, MFA | |
| Riley account active, MFA (existing / created) | |
| Group `family-logistics` exists, members Morgan and Riley | |
| Managed by OpenTofu or UI | |

## 7. Apprise routing

| Tag | Channel types | Recipients (roles) | Test received by |
|-----|---------------|--------------------|------------------|
| fl-report | | | |
| fl-status | | | |
| fl-warning | | | |
| fl-urgent | | | |
| fl-ops | | | |
| fl-shadow | | | |

| Item | Result |
|------|--------|
| No FL tag reaches SMS, Signal or a public topic | |
| Both-parents e-mail route added for `fl-report` | |
| App config uses only `FL_APPRISE_TAG_*` (no bare severity tags) | |
| HTML e-mail renders for both parents | |

## 8. Routing provider

| Item | Result |
|------|--------|
| Routes API enabled, key restricted to Routes API | |
| Key restricted to Docker host egress IP; refused elsewhere | |
| Budget alerts at $8 and $10 to Morgan | |
| Smoke test duration returned | |
| Pricing SKU for the app-side counter | |
| Waze relay prerequisites met | |

## Open issues found during M0

| # | Issue | Affects | Owner |
|---|-------|---------|-------|
| | | | |
