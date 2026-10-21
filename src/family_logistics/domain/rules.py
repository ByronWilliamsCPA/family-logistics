"""Validation rule catalog (handoff section 7).

Each rule has a default severity. A rule may be emitted with a different
severity only where its docstring in the emitting module says so (for example
PASS-003 is Warning for a tentative commitment and Critical for a confirmed one).
"""

from __future__ import annotations

from dataclasses import dataclass

from family_logistics.domain.models import Finding, Severity


@dataclass(frozen=True, slots=True)
class Rule:
    """A catalog entry.

    Attributes:
        rule_id: Stable identifier, e.g. ``PARSE-001``.
        severity: Default severity.
        title: Short description of the trigger.
    """

    rule_id: str
    severity: Severity
    title: str


RULES: dict[str, Rule] = {
    r.rule_id: r
    for r in (
        Rule("PARSE-001", Severity.CRITICAL, "Malformed transportation notes"),
        Rule("NAME-002", Severity.CRITICAL, "Unknown or misspelled family name"),
        Rule(
            "PASS-003",
            Severity.CRITICAL,
            "External passenger commitment disappeared without replacement",
        ),
        Rule("DEST-004", Severity.CRITICAL, "Destination missing or unverified"),
        Rule("RIDE-005", Severity.CRITICAL, "Location change lacks transportation"),
        Rule("DRIVER-006", Severity.CRITICAL, "Driver double-booked or unreachable"),
        Rule("TIME-007", Severity.CRITICAL, "Pickup or drop-off infeasible"),
        Rule("CONF-008", Severity.WARNING, "Tentative or changed since confirmation"),
        Rule("SCHOOL-009", Severity.WARNING, "School baseline uncertain"),
        Rule("ROUTE-010", Severity.INCOMPLETE, "Routing unavailable or unresolved"),
        Rule("DATA-011", Severity.INCOMPLETE, "Source stale or failed"),
        Rule("ORDER-012", Severity.CRITICAL, "Explicit stop order misses a deadline"),
        Rule("EXT-013", Severity.WARNING, "Unknown external name needs matching"),
        Rule("CAP-014", Severity.WARNING, "Vehicle capacity exceeded"),
        Rule("MODE-015", Severity.WARNING, "Bus/bike/walk rule or exception unclear"),
    )
}


def make_finding(  # noqa: PLR0913 - two are keyword-only options
    rule_id: str,
    message: str,
    entity_keys: tuple[str, ...],
    *,
    suggestion: str | None = None,
    severity: Severity | None = None,
) -> Finding:
    """Build a finding using the catalog's default severity unless overridden.

    Args:
        rule_id: Catalog rule identifier.
        message: Explanation without addresses.
        entity_keys: Affected entity identifiers (drive the stable issue ID).
        suggestion: Optional suggested correction.
        severity: Override of the default severity.

    Returns:
        The finding.

    Raises:
        KeyError: If the rule is not in the catalog.
    """
    rule = RULES.get(rule_id)
    if rule is None:
        msg = f"Unknown rule {rule_id!r}"
        raise KeyError(msg)
    return Finding(
        rule_id=rule_id,
        severity=severity or rule.severity,
        message=message,
        entity_keys=entity_keys,
        suggestion=suggestion,
    )
