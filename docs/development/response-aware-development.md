---
title: "Response-Aware Development"
schema_type: common
status: published
owner: core-maintainer
purpose: "How Family Logistics tags assumptions that could cause production failures."
tags:
  - development
  - quality
---

Response-Aware Development (RAD) makes risky assumptions visible in the code
so reviewers and tools can verify them.

## Tag syntax

```python
# #CRITICAL: [category]: [assumption that could cause an outage, data loss or a stranded child]
# #VERIFY: [the defensive code or test that covers it]

# #ASSUME: [category]: [assumption that could cause a bug]
# #VERIFY: [the validation that covers it]

# #EDGE: [category]: [assumption about an uncommon scenario]
# #VERIFY: [optional improvement]
```

Every tag is followed by a `#VERIFY` line naming the check, test or guard
that covers it.

## Mandatory categories in this project

| Category | Examples |
|----------|----------|
| Security | CalDAV credentials, the Home Assistant token, trusted proxy headers, address encryption, children's data in logs or notifications |
| External resources | iCloud availability and rate limits, Home Assistant snapshots, Apprise delivery, routing providers, the inference server |
| Timing | Scheduler runs, recurrence expansion and time zones, quiet hours, the imminent-ride window, stale inputs |
| Data integrity | Notes-template parsing, occurrence identity, confirmation codes, passenger-leg keys |
| Concurrency | The single SQLite writer and WAL checkpoints |

## Example

```python
# #CRITICAL: External resources: a network failure must never look like an empty calendar.
# #VERIFY: the collector raises and the run status becomes `incomplete`; covered by tests.
```

Find all tags with:

```bash
grep -rnE '#(CRITICAL|ASSUME|EDGE)' src
```
