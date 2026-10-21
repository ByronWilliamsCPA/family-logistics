# M0 Check 4: iOS Shortcuts Ride Creation and Confirmation-Code Writing

**Purpose.** The planning team conditionally approved the confirmation code
(ADR-0002) on the condition that **parents never compute or type codes in the
normal flow**: an iOS Shortcut must write the `CONFIRM:` line into the event
notes. This protocol proves, on both parents' iPhones, that Shortcuts can create
a ride in the shared Family Transportation calendar with the v1 notes block,
compute the SHA-256 code, and write it back, and it rehearses the manual
fallback.

## Prerequisites

- Morgan's and Riley's iPhones, each with access to the Family Transportation
  calendar (edit rights).
- A **test** event day at least a week ahead, clearly titled `TEST Ride | ...`,
  deleted after the test.
- The draft Shortcuts `New Family Ride` and `Confirm Ride` (built during M0 from
  the steps below; exported `.shortcut` files are kept out of Git if they embed
  names of external children).
- The reference code for the test event, computed by the app's helper
  (`family-logistics confirm-code` once M1 lands) or by hand from the canonical
  JSON in ADR-0002, to compare against.

## Part A: capture versions

| Device | Owner | iOS version | Shortcuts version | Calendar account |
|--------|-------|-------------|-------------------|------------------|
| iPhone 1 | Morgan | | | |
| iPhone 2 | Riley | | | |

## Part B: ride creation (`New Family Ride`)

| # | Step | Expected | iPhone 1 | iPhone 2 |
|---|------|----------|----------|----------|
| B1 | Run Shortcut; choose trip type, driver, children, external passengers (pseudonym test names), pickup, destinations, status, stop order from menus | Menus show registry lists; no raw enum editing | | |
| B2 | Shortcut builds the notes block | Notes exactly match the v1 template (FL_VERSION first, one destination row per passenger) | | |
| B3 | Shortcut uses **Add New Event** targeting the shared Family Transportation calendar | Event saved in the shared calendar, not a personal one | | |
| B4 | Is a prefilled native Calendar editor available for review before saving? (Add New Event "Show Compose Sheet" or equivalent) | Record yes or no; if no, Shortcut shows a confirmation summary before saving | | |
| B5 | Open the event on the *other* iPhone and on a Mac | Notes identical, line breaks intact | | |

## Part C: confirmation-code writing (`Confirm Ride`)

| # | Step | Expected | iPhone 1 | iPhone 2 |
|---|------|----------|----------|----------|
| C1 | Is **Generate Hash** with SHA-256 available? | Yes | | |
| C2 | Shortcut finds the selected event (Find Calendar Events, then choose) and reads its start, end and notes | Reads all fields | | |
| C3 | Shortcut builds the canonical material text (ADR-0002) and hashes it; takes the first 6 hex characters uppercased | Code equals the reference code | | |
| C4 | Shortcut **writes** the `CONFIRM:` line into the notes (Edit Event, Notes) replacing any existing `CONFIRM:` line, without retyping anything | Notes now contain exactly one `CONFIRM:` line; rest unchanged | | |
| C5 | Change the driver on the event, run `Confirm Ride` again | New code written; old code gone | | |
| C6 | Recurring test series: confirm the series master | Code written on the master; check how iOS asks "this event / future events" and record the choice used | | |
| C7 | Edit one occurrence (move it 30 min), confirm only that occurrence | iOS creates an override; its notes carry its own code; master unchanged | | |
| C8 | Can the Shortcut obtain the occurrence identity it needs (start date of the occurrence)? | Yes; record which variable | | |

## Part D: manual fallback

| # | Step | Expected | iPhone 1 | iPhone 2 |
|---|------|----------|----------|----------|
| D1 | Copy the expected code from the registry UI or report (once available; for M0 use the reference code) and paste `CONFIRM: <code>` into notes | Works in under a minute | | |
| D2 | Note any autocorrect or smart punctuation that changes the text | None, or documented workaround | | |

## Pass criteria

- C1 to C5 pass on both iPhones: the Shortcut writes a correct code with no
  manual typing.
- C6 to C8 behave as described or the deviations are documented for the series
  rule in ADR-0002.
- D1 works as a fallback.

If C3 or C4 fails on either device, record it; the planning condition then falls
back to the manual flow and ADR-0002 is revisited before M1 locks the
algorithm.

## Cleanup

Delete every `TEST Ride` event and series. Record results in section 4 of
[RESULTS-TEMPLATE.md](RESULTS-TEMPLATE.md).
