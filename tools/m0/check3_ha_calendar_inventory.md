# M0 Check 3: Home Assistant School Schedules, Calendars and Locations

**Purpose.** The app reuses Home Assistant's two school layers (school-specific
bell schedules with weekday variations, and the district Google calendars for
closures and short days) through the HA snapshot push, and keeps no duplicate
bell-schedule configuration. Before the snapshot payload can be designed we must
know exactly how Home Assistant represents each layer, which integration backs
each calendar, which zones exist, and whether Waze is available.

## Prerequisites

- Admin access to Home Assistant, and its base URL (`HA_URL`).
- A laptop on the home network with `uv` installed.

## Steps

1. In Home Assistant, open **Profile, Security, Long-lived access tokens** and
   create a token named `fl-m0-discovery-<date>`. Copy it; do not save it to a file.
2. Run the inventory (the token is read from `HA_TOKEN` or a hidden prompt):

   ```bash
   uv run --script tools/m0/check3_ha_calendar_inventory.py --json
   # if the bell schedules are named after the schools:
   uv run --script tools/m0/check3_ha_calendar_inventory.py --pattern 'bell|school|<school short names>'
   ```

3. **Delete the token immediately** in the same Security page. Family
   Logistics never holds a Home Assistant credential (ADR-0007).
4. Answer the questions below by inspection in the HA UI where the script
   cannot tell (for example template sensor definitions).

## Questions to answer

| # | Question | Where to look |
|---|----------|---------------|
| 1 | Which integration backs `calendar.family_calendar`, `calendar.school_district`, `calendar.school_year` (CalDAV, Google, local, iCal URL)? | script output `platform=` / `entry=` |
| 2 | How is each school's **bell schedule** represented: `schedule.*` helper, local calendar with recurring events, template sensor, input_datetime set, or something else? One per school? | script "Bell-schedule candidates"; Settings, Devices and services, Helpers |
| 3 | How are **weekday variations** (early release, late start) encoded: separate schedule blocks, separate calendar events, or attributes? | open each candidate entity |
| 4 | Do district calendars mark closures and short days consistently (summary text, all-day events)? | `--show-school-summaries` |
| 5 | Which zones exist for schools, studios, fields and homes, and do they have coordinates and sensible radii? | script "Zones" (no coordinates printed) |
| 6 | Is `waze_travel_time` installed, and is the action available? | script "Waze travel time" |
| 7 | Which iCloud calendars are already ingested by HA's CalDAV integration (names only)? | `calendar.*` list |

## Pass criteria

- Every expected calendar entity exists and its source is known.
- Each school's bell schedule, including weekday variations, has an identified
  representation that the HA snapshot automation can read.
- School, activity and home zones are listed (coordinates present).
- Waze presence is known (absence is not a failure of M0, but it blocks the
  `ha_relay` provider until installed).

## Record

Fill section 3 of [RESULTS-TEMPLATE.md](RESULTS-TEMPLATE.md). Record entity IDs,
integration types and representation choices. **Do not** record coordinates,
addresses, or summaries from the family calendar.
