---
title: "Testing"
schema_type: common
status: published
owner: core-maintainer
purpose: "Testing guide for Family Logistics."
tags:
  - development
  - testing
---

This guide covers the testing strategy and patterns for Family Logistics.

## Running Tests

### Full Test Suite

```bash
# Run all tests with coverage
uv run pytest -v --cov=src --cov-report=term-missing

# Run with nox across Python versions
nox -s test
```

### A subset of the tests

`pyproject.toml` runs every `pytest` with `--cov-fail-under=80` (the `addopts` setting), so any subset fails on the
coverage gate even when every test passes. Add `--no-cov` when you run only part of the suite:

```bash
uv run pytest tests/unit --no-cov
uv run pytest tests/integration --no-cov
uv run pytest tests/scenarios -q -rs --no-cov
uv run pytest tests/unit/test_readonly_transport.py --no-cov
```

Select by directory, not by marker: only a few modules carry `@pytest.mark.unit` or `@pytest.mark.integration`, so
`-m unit` selects almost nothing. The markers are registered (`--strict-markers`), so an unregistered one is an error.

## Test Structure

```text
tests/
├── conftest.py              # Shared fixtures
├── unit/                    # 83 test modules, one per domain, adapter, service or API area
├── integration/             # test_ci_security_guards.py, test_fips_workflow_gate.py
├── scenarios/               # test_acceptance_matrix.py, test_friday_external_passenger.py,
│                            # test_notification_subjects_matrix.py
├── fixtures/synthetic/      # the only tracked .ics files (synthetic events)
├── fl_*.py                  # shared builders for the fictional household and the service fixtures
└── caldav_fakes.py          # CalDAV test doubles
```

Test data is the fictional household only (Morgan, Riley, Alex, Sam, Jordan, Casey, Passenger E, Passenger R,
Driver S); see `tests/fl_factories.py`.

## Coverage Requirements

The only enforced gate is **80% overall coverage** (`--cov-fail-under=80` in `pyproject.toml`). There is no separate
per-tier target.

## Acceptance matrix

`tests/scenarios/test_acceptance_matrix.py` holds the acceptance scenarios. The numbered list they come from (the
development handoff, section 16) is not in this repository, so **the test file is the canonical record**. The numbers
below are the `# scenario N` comments in that file; scenarios 2 and 3 are not recorded there and need the owner's
handoff list. Rows without a number are not numbered in the file and are left unnumbered here.

| Scenario | Test |
|----------|------|
| 1 | `test_shared_ride_projects_to_each_passenger` |
| 4 | `test_consecutive_same_location_is_one_stay` |
| 5 | `test_school_sports_extends_stay` |
| 6 | `test_school_specific_bell_overlay` |
| 7 | `test_no_school_day` |
| 8 | `test_bike_baseline_exception` |
| 9 | `test_away_meet_team_bus` |
| 10 | `test_driver_unavailable_flags_every_passenger` |
| 11 | `test_friday_rule_through_the_engine`, plus `tests/scenarios/test_friday_external_passenger.py` |
| 12 | `test_destination_override_resolved_per_passenger` |
| (unnumbered) one passenger absent from one occurrence | `test_one_passenger_absent_from_one_occurrence` |
| (unnumbered) typo is critical with a suggestion | `test_typo_is_critical_with_suggestion` |
| (unnumbered) outbound for two passengers, return for one | `test_sam_and_passenger_r_outbound_only_sam_returns` |
| (unnumbered) two children with the same first name | `test_two_registered_children_with_same_first_name` |
| (unnumbered) driver changed on a confirmed trip | `test_driver_changed_on_confirmed_trip_requires_reconfirmation` |
| (unnumbered) iCloud unavailable | `test_icloud_unavailable_is_incomplete_not_green` |
| (unnumbered) route service unavailable | `test_route_service_unavailable_is_incomplete` |
| (unnumbered) deadline beats a shorter route | `test_deadline_beats_shorter_route` |
| (unnumbered) external passenger removed | `test_external_passenger_removed_unintentionally` |
| (unnumbered) moved occurrence | `test_event_moved_to_new_time_single_occurrence_no_duplicate` |
| (unnumbered) on-demand run is idempotent | `test_on_demand_idempotent` |
| (unnumbered) LLM unavailable | `test_llm_unavailable`, skipped until M5 (`M5: LLM unavailable -> deterministic report without narrative`) |

`tests/scenarios/test_friday_external_passenger.py` is a permanent regression test for design rule 3 (a change to one
passenger never removes another's ride). Never delete or weaken it; change it only with the owner's approval.

## Writing Tests

### Test Markers

```python
import pytest


@pytest.mark.unit
def test_unit_example():
    """Unit test example."""
    pass


@pytest.mark.integration
def test_integration_example():
    """Integration test example."""
    pass


@pytest.mark.slow
def test_slow_example():
    """Slow test - excluded from fast runs."""
    pass
```

## Continuous Integration

Tests run automatically on:

- Pull request creation
- Push to main/develop branches
- Scheduled nightly builds
