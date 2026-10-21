# M0 Validation Kit

Eight discovery checks that must pass, or have their deviations recorded,
before M1 (ingestion and parser) starts. They answer the open technical
questions from the development handoff and the planning review (2026-10-21).
Results go in a copy of [RESULTS-TEMPLATE.md](RESULTS-TEMPLATE.md) kept outside
Git until it has been reviewed for personal data.

| # | Check | Type | Who runs it | Where |
|---|-------|------|-------------|-------|
| 1 | Dedicated Apple ID, View Only CalDAV access | script `check1_caldav_readonly_probe.py` | Morgan | owner's laptop on the home network |
| 2 | Calendar inventory, recurrence and notes behavior, sanitized fixtures | script `check2_export_sanitized_fixtures.py` | Morgan | owner's laptop |
| 3 | HA school schedules (incl. bell schedules) and location entities | script + [protocol](check3_ha_calendar_inventory.md) | Morgan | owner's laptop |
| 4 | iOS Shortcut ride creation and `CONFIRM:` writing | [manual protocol](check4_shortcuts_protocol.md) | Morgan and Riley | both iPhones |
| 5 | Docker host egress | script `check5_egress_probe.sh` | Morgan | the Docker host |
| 6 | Authentik accounts and `family-logistics` group | [manual protocol](check6_authentik_access.md) | Morgan | Authentik admin |
| 7 | Apprise routing and recipients | [manual protocol](check7_apprise_routing.md) | Morgan (Riley confirms receipt) | Apprise configuration |
| 8 | Routing provider credentials and budget enforcement | [manual protocol](check8_routing_provider.md) | Morgan | Google Cloud, HA |

## Secret handling (applies to every check)

- Credentials are supplied **only** through environment variables or a hidden
  interactive prompt. No script accepts a password or token as a command-line
  argument, and none prints one.
- Run from the owner's own machine on the home network (check 5 runs on
  the Docker host). Do not run these scripts in a CI job, a shared terminal
  recording, or a remote coding session.
- Use `read -rs VAR; export VAR` to set a secret, and `unset VAR` afterwards.
- Never commit credentials, calendar URLs or IDs, event UIDs, addresses,
  coordinates, external children's real names, phone numbers or e-mail
  addresses (privacy spec section 12).
- If a credential may have been exposed (pasted, logged, screen-shared):
  revoke the Apple app-specific password at appleid.apple.com, delete the HA
  token, or regenerate the Google key, then create a new one.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) installed; each Python script declares its
  own dependencies (PEP 723), so `uv run --script <file>` needs no project
  environment.
- Every script supports `--help`.

## Check 1: dedicated Apple ID, View Only CalDAV

**Purpose.** Prove that the dedicated Family Logistics Apple ID sees what the
app needs and cannot write (planning decision: dedicated Apple ID with View Only
shares; calendars are owned by Morgan's Apple ID).

**Setup.**

1. Create the dedicated Apple ID (for example `family-logistics@<household domain>`)
   with two-factor authentication.
2. From Morgan's Apple ID, share each needed calendar (Family Transportation and
   each child's calendar) with the dedicated account, **View Only**
   (Calendar app, calendar info, Add Person, then turn off "Allow Editing").
3. Accept each invitation while signed in as the dedicated account
   (iCloud.com Calendar).
4. Generate an app-specific password for the dedicated account at
   appleid.apple.com, Sign-In and Security, App-Specific Passwords. Name it
   `family-logistics`.

**Run.**

```bash
uv run --script tools/m0/check1_caldav_readonly_probe.py --self-test   # offline, proves writes are blocked
export FL_CALDAV_USERNAME='<dedicated Apple ID>'
read -rs FL_CALDAV_APP_PASSWORD; export FL_CALDAV_APP_PASSWORD
uv run --script tools/m0/check1_caldav_readonly_probe.py --check-write-privilege
unset FL_CALDAV_APP_PASSWORD
```

**Pass.** `RESULT: PASS`, every shared calendar listed, `server-side posture:
read-only` on all of them, at least one event shows `with DESCRIPTION`, ETags
present on all events, and sync-collection or ctag available. Read-only is
called **server-enforced** only if all of this holds through the dedicated
account.

**Fail handling.** If shared View Only calendars hide notes or recurrence data,
record it; ADR-0003 then falls back to the code-level allow-list with a
documented residual risk.

## Check 2: calendar inventory, recurrence and notes behavior, fixtures

**Purpose.** Confirm how iCloud stores recurring rides, overridden occurrences,
moved occurrences and notes, and produce sanitized fixtures for M1 tests.

**Setup.** On a test day at least a week ahead, create in the Family
Transportation calendar:

1. A shared ride with two family children and one external passenger, using the
   v1 notes template.
2. A weekly recurring ride, then edit one occurrence (remove one passenger) and
   move another occurrence by 30 minutes.
3. A ride with a misspelled family name (`Jordn`).

Create a mapping file **outside the repository**, for example
`~/fl-m0/mapping.json`: `{"<real external first name>": "External Passenger E"}`.

**Run.**

```bash
read -rs FL_CALDAV_APP_PASSWORD; export FL_CALDAV_APP_PASSWORD
uv run --script tools/m0/check2_export_sanitized_fixtures.py --caldav \
  --username '<dedicated Apple ID>' --calendar 'Family Transportation' \
  --summary 'TEST Ride' --mapping ~/fl-m0/mapping.json \
  --shift-days 364 --out ~/fl-m0/fixtures
unset FL_CALDAV_APP_PASSWORD
```

Alternatively export the events from Calendar on a Mac and use
`--from-ics <files>`. The script refuses to write into a git work tree unless
`--allow-repo-path` is given, and refuses any output that still contains an
e-mail address, phone number, street address or mapped real name.

**Pass.** One fixture per test scenario; the summary line shows
`recurrence_id=True` for overridden occurrences and `rrule=True` for the
series; notes keys survive intact. Review each file by eye, then copy the
reviewed fixtures to `tests/fixtures/caldav/` in a separate commit.

## Check 3: HA school schedules and locations

See [check3_ha_calendar_inventory.md](check3_ha_calendar_inventory.md). Uses a
short-lived HA token deleted right after the run.

## Check 4: iOS Shortcut creation and confirmation-code writing

See [check4_shortcuts_protocol.md](check4_shortcuts_protocol.md). Both iPhones.

## Check 5: Docker host egress

**Purpose.** Prove the Docker host, and a container on the service's network, can
reach iCloud CalDAV, Google Routes and Apprise, and confirm the local inference server is
not yet reachable (its model route is an M5 prerequisite).

**Run on the Docker host.**

```bash
export APPRISE_URL=<apprise status URL>        # from the private deployment repository
export INFERENCE_URL=<inference base URL>      # optional
bash tools/m0/check5_egress_probe.sh                             # default bridge network
bash tools/m0/check5_egress_probe.sh --network <stack network>   # once the stack network exists
```

**Pass.** `RESULT: PASS`; iCloud and Apprise reachable from both host and
container; Google Routes reachable if that provider will be evaluated; the
inference server, if probed, reported "not reachable yet, as expected" (if it is reachable, record it: the
model route may have gone live).

## Check 6: Authentik

See [check6_authentik_access.md](check6_authentik_access.md).

## Check 7: Apprise routing

See [check7_apprise_routing.md](check7_apprise_routing.md). Family Logistics
sends only to six dedicated `fl-*` tags, never to shared severity tags, and
needs an e-mail route that reaches both parents.

## Check 8: routing provider

See [check8_routing_provider.md](check8_routing_provider.md).

## Exit codes (scripts)

| Code | Meaning |
|------|---------|
| 0 | Pass |
| 1 | Completed, but a required capability is missing or output was refused |
| 2 | Usage, authentication or connectivity error |
