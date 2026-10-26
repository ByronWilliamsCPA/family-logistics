"""Notification text: templates, masked names and chunking (ADR 0008 D1 to D3).

Everything here is pure: no I/O, no clock reads, no logging, and nothing is
read from the environment. A ``PlannedPush`` carries the full finding, so the
content boundary is enforced here, at render time. A push is built only from

- the rule ID and severity (closed template and label maps),
- the finding's lifecycle and ``issue_id`` (16 hex characters),
- the structured ``Subject`` (a local time or day, registry IDs and kinds),
  rendered through ``NameDirectory``, and
- closed maps for the DATA-011 source, the run status and the counts.

``Finding.message``, ``Finding.suggestion``, ``Finding.entity_keys`` (except
the closed DATA-011 source map), addresses, UIDs, calendar IDs, free notes and
``AssessmentResult.error`` are never read.

Clear wording (``CLEAR_PHRASES``) lives only in the clear status or report
text. Every other push, including every push of a failed, unavailable or
incomplete run, avoids those words.

#CRITICAL: Security: children's data. Names come only from ``NameDirectory``
by ``(kind, id)``: family first names, an external child as an initial, and
generic words for anything unregistered or unknown. No free text is rendered.
#VERIFY: tests/unit/test_notification_text.py (T-text-2, T-text-3, T-text-5,
T-text-11 and the canary properties).
#CRITICAL: Timing: a failed, unavailable or incomplete run is never worded as
clear, and a status or report without a completed run status is worded as a
failed check.
#VERIFY: tests/unit/test_notification_text.py (TestNeverClear, T-text-6).
#ASSUME: External resources: the production tags in ``PlannedPush.intended_tag``
come from operator-controlled settings and never contain clear wording; the
shadow header prints them after dropping every character outside
``[A-Za-z0-9_.:-]``.
#VERIFY: tests/unit/test_notification_text.py (header sanitising); the tag
settings are validated in ``core.config``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from family_logistics.domain.lifecycle import Lifecycle
from family_logistics.domain.models import PassengerKind, RunStatus, Severity
from family_logistics.domain.notification_policy import (
    MAX_ITEMS_PER_PUSH,
    PlannedPush,
    PushKind,
    severity_rank,
)
from family_logistics.domain.notification_subjects import UNREGISTERED_PREFIX, Subject
from family_logistics.domain.rules import RULES

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from zoneinfo import ZoneInfo

    from family_logistics.domain.notification_policy import NotificationPolicy

TITLE_LIMIT: Final = 120
"""Longest title any push client is asked to show."""
BODY_LIMIT: Final = 2000
"""Longest body any push client is asked to show."""
NAME_LIMIT: Final = 20
"""A rendered first name is cut to this many characters."""
GENERIC_PASSENGER: Final = "a passenger"
GENERIC_DRIVER: Final = "the driver"
_MAX_NAMES_SHOWN: Final = 2
_FAMILY_ROLES: Final = frozenset({"child", "parent"})
_ISSUE_ID: Final = re.compile(r"[0-9a-f]{16}")
_TAG_CHARS: Final = re.compile(r"[^A-Za-z0-9_.:-]")
_TAG_LIMIT: Final = 40
_EPOCH: Final = datetime.min.replace(tzinfo=UTC)
_WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


@dataclass(frozen=True, slots=True)
class RuleTemplate:
    """The wording of one rule.

    Attributes:
        label: Title fragment, for example ``destination missing or
            unverified``. No placeholders.
        line: One body sentence; ``str.format`` with only ``{who}``,
            ``{when}``, ``{driver}`` and ``{source}``.
    """

    label: str
    line: str


RULE_TEMPLATES: Final[Mapping[str, RuleTemplate]] = MappingProxyType(
    {
        "PARSE-001": RuleTemplate(
            "transportation notes not readable",
            "A transportation event ({when}) has notes that could not be read.",
        ),
        "NAME-002": RuleTemplate(
            "unknown family name in notes",
            "A transportation event ({when}) names someone not in the registry.",
        ),
        "PASS-003": RuleTemplate(
            "passenger ride dropped",
            "{who}: a ride ({when}) was dropped, no replacement; driver was {driver}.",
        ),
        "DEST-004": RuleTemplate(
            "destination missing or unverified",
            "{who}: the destination for the ride ({when}) is missing or unverified.",
        ),
        "RIDE-005": RuleTemplate(
            "no ride for a location change",
            "{who}: a change of location ({when}) has no ride.",
        ),
        "DRIVER-006": RuleTemplate(
            "driver double-booked or unreachable",
            "{driver} may be double-booked or unreachable for {who} ({when}).",
        ),
        "TIME-007": RuleTemplate(
            "pickup or drop-off not feasible",
            "{who}: the pickup or drop-off ({when}) with {driver} is not feasible.",
        ),
        "CONF-008": RuleTemplate(
            "ride tentative or changed",
            "A ride ({when}) is tentative or changed since it was confirmed.",
        ),
        "SCHOOL-009": RuleTemplate(
            "school schedule uncertain",
            "{who}: the school schedule ({when}) is uncertain.",
        ),
        "ROUTE-010": RuleTemplate(
            "could not verify travel",
            "{who}: could not verify travel time or places for the ride ({when}).",
        ),
        "DATA-011": RuleTemplate(
            "data source stale or failed",
            "Data is stale or failed ({source}); rides could not be verified.",
        ),
        "ORDER-012": RuleTemplate(
            "stop order misses a deadline",
            "{who}: the explicit stop order for the ride ({when}) misses a deadline.",
        ),
        "EXT-013": RuleTemplate(
            "unknown external name",
            "A transportation event ({when}) names a child who needs matching.",
        ),
        "CAP-014": RuleTemplate(
            "vehicle capacity",
            "Vehicle capacity for the ride with {driver} ({when}) is exceeded or unknown.",
        ),
        "MODE-015": RuleTemplate(
            "bus, bike or walk plan unconfirmed",
            "{who}: the bus, bike or walk plan ({when}) is unconfirmed.",
        ),
    }
)
"""One template per rule ID in ``RULES`` (a test pins the key sets equal)."""

_FALLBACK_LABEL: Final = "needs attention"
_FALLBACK_TEXT: Final = "A ride check needs attention"
_SEVERITY_LABELS: Final = {
    Severity.CRITICAL: "CRITICAL",
    Severity.WARNING: "WARNING",
    Severity.INCOMPLETE: "COULD NOT VERIFY",
}
_UNKNOWN_SEVERITY_LABEL: Final = "COULD NOT VERIFY"
_LIFECYCLE_WORDS: Final = {
    Lifecycle.NEW: "new",
    Lifecycle.REGRESSED: "back again",
    Lifecycle.ONGOING: "still open",
}
_SOURCE_LABELS: Final = {
    "source:icloud": "calendar",
    "source:ha_snapshot": "school schedule",
    "source:none": "any data source",
}
_OTHER_SOURCE: Final = "a data source"
_REASON_LABELS: Final = {
    "DATA-011": "data sources",
    "ROUTE-010": "travel times or places",
    "CAP-014": "vehicle capacities",
}
_OTHER_REASONS: Final = "other checks"
_STATUS_LABELS: Final = {
    RunStatus.CLEAR: "Clear",
    RunStatus.ATTENTION_REQUIRED: "attention required",
    RunStatus.INCOMPLETE: "could not fully verify",
    RunStatus.FAILED: "check failed",
}
_CLEAR_BODY: Final = "No open findings; sources fresh; driver acceptance not verified."
_ACCEPTANCE: Final = "Driver acceptance not verified."
_FAILED_BODY: Final = "The ride check did not finish, so rides could not be verified."
_RUN_FAILED_BODY: Final = (
    "The ride check did not finish, so rides could not be verified. "
    "Treat rides as unchecked until a check completes."
)
_RUN_FAILED: Final = "run-failed"
_OPS_TITLES: Final = {
    "ops:run_failed": (
        "Ops: ride check run failed",
        "A ride check run failed. See the service log and the runbook.",
    ),
    "ops:run_incomplete": (
        "Ops: ride check could not fully verify",
        "The latest ride check could not fully verify. See the status card.",
    ),
}
_OPS_OTHER: Final = (
    "Ops: service needs attention",
    "A service check needs the operator's attention. See the service log.",
)


def _first_token(display_name: str) -> str | None:
    """Return the filtered first token of a display name, or None.

    Every character outside letters, apostrophe and hyphen is removed, the
    result is cut to ``NAME_LIMIT`` and must contain a letter.
    """
    tokens = display_name.split()
    if not tokens:
        return None
    cleaned = "".join(c for c in tokens[0] if c.isalpha() or c in "'-")[:NAME_LIMIT]
    return cleaned if any(c.isalpha() for c in cleaned) else None


def _initial(display_first_name: str) -> str | None:
    """Return the upper-cased first letter plus a period, or None."""
    for char in display_first_name:
        if char.isalpha():
            return f"{char.upper()[0]}."
    return None


@dataclass(frozen=True, slots=True)
class NameDirectory:
    """Masked names, looked up by ``(kind, id)`` (D2, F8).

    Attributes:
        family: ``person_id`` (role child or parent) to first name.
        drivers: ``person_id`` (any role) to first name.
        externals: ``passenger_id`` to an initial such as ``E.``.
    """

    family: Mapping[str, str]
    drivers: Mapping[str, str]
    externals: Mapping[str, str]

    @classmethod
    def from_rows(
        cls,
        people: Iterable[tuple[str, str, str]],
        passengers: Iterable[tuple[str, str]],
    ) -> NameDirectory:
        """Build the directory from the two narrow queries.

        Args:
            people: ``(person_id, display_name, role)`` rows of the ``person``
                table.
            passengers: ``(passenger_id, display_first_name)`` rows of the
                ``external_passenger`` table.

        Returns:
            The directory. A name with no usable letter is left out, so it
            renders as a generic word.
        """
        family: dict[str, str] = {}
        drivers: dict[str, str] = {}
        for person_id, display_name, role in people:
            first = _first_token(display_name)
            if first is None:
                continue
            drivers[person_id] = first
            if role in _FAMILY_ROLES:
                family[person_id] = first
        externals = {
            passenger_id: initial
            for passenger_id, name in passengers
            if (initial := _initial(name)) is not None
        }
        return cls(
            family=MappingProxyType(family),
            drivers=MappingProxyType(drivers),
            externals=MappingProxyType(externals),
        )

    @classmethod
    def empty(cls) -> NameDirectory:
        """Return a directory that renders every name generically.

        Returns:
            The empty directory (also the fallback when loading failed).
        """
        return cls.from_rows([], [])

    def passenger(self, passenger_id: str, kind: PassengerKind) -> str:
        """Render a passenger or monitored child.

        Args:
            passenger_id: A ``person_id`` (family) or ``passenger_id``
                (external).
            kind: Which table the ID belongs to.

        Returns:
            A first name, an initial such as ``E.``, or ``a passenger``.
        """
        if passenger_id.startswith(UNREGISTERED_PREFIX):
            return GENERIC_PASSENGER
        table = self.externals if kind is PassengerKind.EXTERNAL else self.family
        return table.get(passenger_id, GENERIC_PASSENGER)

    def driver(self, person_id: str | None) -> str:
        """Render a driver.

        Args:
            person_id: A ``person_id`` of any role, or None.

        Returns:
            A first name, or ``the driver``.
        """
        if person_id is None or person_id.startswith(UNREGISTERED_PREFIX):
            return GENERIC_DRIVER
        return self.drivers.get(person_id, GENERIC_DRIVER)


@dataclass(frozen=True, slots=True)
class RenderedPush:
    """One push ready for Apprise.

    Attributes:
        title: At most ``TITLE_LIMIT`` characters.
        body: At most ``BODY_LIMIT`` characters.
    """

    title: str
    body: str


@dataclass(frozen=True, slots=True)
class _Piece:
    """One push's contribution: its own title and its body lines."""

    title: str
    lines: tuple[str, ...]


def _to_zone(moment: datetime, zone: ZoneInfo) -> datetime:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(zone)


def _when(subject: Subject, zone: ZoneInfo) -> str | None:
    """Format the subject's time or day in the local zone, or None."""
    if subject.due_at is not None:
        local = _to_zone(subject.due_at, zone)
        return (
            f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]} "
            f"{local.hour:02d}:{local.minute:02d}"
        )
    if subject.day is not None:
        day = subject.day
        return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"
    return None


def _who(subject: Subject, names: NameDirectory) -> str:
    shown = [names.passenger(pid, kind) for pid, kind in subject.passengers]
    if not shown:
        return GENERIC_PASSENGER
    if len(shown) == 1:
        return shown[0]
    if len(shown) == _MAX_NAMES_SHOWN:
        return f"{shown[0]} and {shown[1]}"
    return f"{shown[0]}, {shown[1]} and {len(shown) - _MAX_NAMES_SHOWN} more"


def _source(entity_keys: Sequence[str]) -> str:
    """Map the DATA-011 source key through the closed map."""
    key = next((k for k in entity_keys if k.startswith("source:")), None)
    return _SOURCE_LABELS.get(key, _OTHER_SOURCE) if key is not None else _OTHER_SOURCE


def _safe_issue_id(issue_id: str | None) -> str | None:
    if issue_id is not None and _ISSUE_ID.fullmatch(issue_id):
        return issue_id
    return None


def _suffix(lifecycle: Lifecycle | None, issue_id: str | None) -> str:
    parts: list[str] = []
    if lifecycle is not None and lifecycle in _LIFECYCLE_WORDS:
        parts.append(_LIFECYCLE_WORDS[lifecycle])
    if issue_id is not None:
        parts.append(f"ref {issue_id}")
    return f" ({', '.join(parts)})" if parts else ""


def _fallback_text(rule_id: str | None, issue_id: str | None, when: str | None) -> str:
    """Build the fixed fallback sentence (a rule ID only if it is in ``RULES``)."""
    text = _FALLBACK_TEXT + (f", at {when}" if when is not None else "") + "."
    if rule_id in RULES and issue_id is not None:
        return f"{text} Rule {rule_id}, ref {issue_id}."
    if rule_id in RULES:
        return f"{text} Rule {rule_id}."
    if issue_id is not None:
        return f"{text} Ref {issue_id}."
    return text


def _finding_piece(
    pushed: PlannedPush, names: NameDirectory, policy: NotificationPolicy
) -> _Piece:
    """Render one finding push from its template (never from its free text)."""
    tracked = pushed.tracked
    found = tracked.finding if tracked is not None else None
    rule_id = found.rule_id if found is not None else pushed.rule_id
    severity = found.severity if found is not None else pushed.severity
    issue_id = _safe_issue_id(found.issue_id if found is not None else None)
    keys = found.entity_keys if found is not None else ()
    subject = pushed.subject if pushed.subject is not None else Subject()
    when = _when(subject, policy.zone)
    severity_label = (
        _SEVERITY_LABELS.get(severity, _UNKNOWN_SEVERITY_LABEL)
        if severity
        else _UNKNOWN_SEVERITY_LABEL
    )
    template = RULE_TEMPLATES.get(rule_id) if rule_id is not None else None
    if template is None:
        label = _FALLBACK_LABEL
        sentence = _fallback_text(rule_id, issue_id, when)
        tail = ""
    else:
        label = template.label
        sentence = template.line.format(
            who=_who(subject, names),
            when=when if when is not None else "time unknown",
            driver=names.driver(subject.driver_id),
            source=_source(keys),
        )
        tail = _suffix(tracked.lifecycle if tracked is not None else None, issue_id)
    prefix = "Urgent ride check" if pushed.kind is PushKind.URGENT else "Ride check"
    return _Piece(
        title=f"[{severity_label}] {prefix}: {label}",
        lines=(f"- [{severity_label}] {sentence}{tail}",),
    )


def _clamp(value: int) -> int:
    return max(0, int(value))


def _open_line(counts: Mapping[str, int] | None) -> str | None:
    if counts is None:
        return None
    critical = _clamp(counts.get(Severity.CRITICAL.value, 0))
    warning = _clamp(counts.get(Severity.WARNING.value, 0))
    return f"Open: {critical} critical, {warning} warning."


def _reasons_line(counts: Mapping[str, int] | None) -> str | None:
    """List the incomplete reasons as counts from the closed label map."""
    if not counts:
        return None
    shown = [
        (label, _clamp(counts.get(rule_id, 0)))
        for rule_id, label in _REASON_LABELS.items()
    ]
    other = sum(
        _clamp(value)
        for rule_id, value in counts.items()
        if rule_id in RULES and rule_id not in _REASON_LABELS
    )
    shown.append((_OTHER_REASONS, other))
    parts = [f"{label}: {count}" for label, count in shown if count > 0]
    return f"Reasons: {', '.join(parts)}." if parts else None


def _run_level_piece(pushed: PlannedPush, counts: Mapping[str, int] | None) -> _Piece:
    """Render a status or report push; anything but a known status is a failure."""
    status = pushed.run_status if pushed.run_status is not None else RunStatus.FAILED
    head = "Weekly ride report" if pushed.kind is PushKind.REPORT else "Ride check"
    title = f"{head}: {_STATUS_LABELS[status]}"
    if status is RunStatus.CLEAR:
        return _Piece(title, (_CLEAR_BODY,))
    if status is RunStatus.FAILED:
        return _Piece(title, (_FAILED_BODY,))
    open_line = _open_line(counts)
    if status is RunStatus.ATTENTION_REQUIRED:
        lead = open_line or "Open findings need attention."
        return _Piece(title, (f"{lead} {_ACCEPTANCE}",))
    lines = ["Ride check: could not fully verify."]
    reasons = _reasons_line(counts)
    if reasons is not None:
        lines.append(reasons)
    lines.append(f"{open_line} {_ACCEPTANCE}" if open_line else _ACCEPTANCE)
    return _Piece(title, tuple(lines))


def _run_failed_piece(pushed: PlannedPush) -> _Piece:
    if pushed.kind is PushKind.URGENT:
        title = "Urgent: ride check failed, rides could not be verified"
    else:
        title = "Ride check failed, rides could not be verified"
    return _Piece(title, (_RUN_FAILED_BODY,))


def _piece(
    pushed: PlannedPush,
    names: NameDirectory,
    policy: NotificationPolicy,
    counts: Mapping[str, int] | None,
) -> _Piece:
    if pushed.kind in {PushKind.STATUS, PushKind.REPORT}:
        return _run_level_piece(pushed, counts)
    if pushed.subject_key == _RUN_FAILED:
        return _run_failed_piece(pushed)
    if pushed.kind is PushKind.OPS:
        title, body = _OPS_TITLES.get(pushed.subject_key, _OPS_OTHER)
        return _Piece(title, (body,))
    return _finding_piece(pushed, names, policy)


def _shadow_header(first: PlannedPush) -> str:
    tag = _TAG_CHARS.sub("", first.intended_tag)[:_TAG_LIMIT]
    note = (
        "bypasses quiet hours"
        if first.kind is PushKind.URGENT
        else "held during quiet hours"
    )
    return f"SHADOW: production would send this to {tag} ({note})."


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3].rstrip()}..."


def _fit_body(header: Sequence[str], pieces: Sequence[_Piece]) -> str:
    """Join the lines, dropping whole trailing pieces if the body is too long."""
    kept = list(pieces)
    while True:
        dropped = len(pieces) - len(kept)
        lines = [*header, *(line for each in kept for line in each.lines)]
        if dropped:
            lines.append(f"and {dropped} more.")
        body = "\n".join(lines)
        if len(body) <= BODY_LIMIT or len(kept) <= 1:
            return _clip(body, BODY_LIMIT)
        kept.pop()


def render(
    pushes: Sequence[PlannedPush],
    *,
    names: NameDirectory,
    policy: NotificationPolicy,
    counts: Mapping[str, int] | None = None,
) -> RenderedPush:
    """Render one chunk as a single push (D2, D3, D10).

    The chunk is one push of any kind, or up to ``MAX_ITEMS_PER_PUSH`` notices
    from ``chunk_pushes``. Longer input is shortened by whole items.

    Args:
        pushes: The planned pushes of the chunk.
        names: Masked names for the subjects.
        policy: The policy (local zone and mode).
        counts: For a status or report: open counts keyed ``critical`` and
            ``warning``, and the incomplete findings per rule keyed by rule
            ID (reasons). Other keys are ignored.

    Returns:
        The rendered push. In shadow mode the title starts ``[shadow]`` and
        the first body line names the tag production would use.

    Raises:
        ValueError: ``pushes`` is empty.
    """
    if not pushes:
        msg = "render needs at least one planned push"
        raise ValueError(msg)
    pieces = [_piece(each, names, policy, counts) for each in pushes]
    title = (
        pieces[0].title
        if len(pieces) == 1
        else f"Ride check: {len(pieces)} items need attention"
    )
    header: list[str] = []
    if policy.mode == "shadow":
        title = f"[shadow] {title}"
        header.append(_shadow_header(pushes[0]))
    return RenderedPush(title=_clip(title, TITLE_LIMIT), body=_fit_body(header, pieces))


def _notice_order(pushed: PlannedPush) -> tuple[int, bool, datetime]:
    """Sort key: most severe first, then soonest, then no time last."""
    severity = pushed.tracked.finding.severity if pushed.tracked else pushed.severity
    rank = severity_rank(severity) if severity is not None else 0
    due = pushed.subject.due_at if pushed.subject is not None else None
    return (-rank, due is None, due if due is not None else _EPOCH)


def chunk_pushes(pushes: Iterable[PlannedPush]) -> tuple[tuple[PlannedPush, ...], ...]:
    """Group a plan's pushes into the Apprise calls of one dispatch (D10).

    Notices are grouped most severe and soonest first into chunks of at most
    ``MAX_ITEMS_PER_PUSH``. ``urgent``, ``status``, ``report`` and ``ops``
    are one push per chunk.

    Args:
        pushes: The pushes owed, in plan order.

    Returns:
        Urgent pushes first, then the notice chunks, then the other pushes in
        their given order.
    """
    urgent: list[tuple[PlannedPush, ...]] = []
    notices: list[PlannedPush] = []
    others: list[tuple[PlannedPush, ...]] = []
    for pushed in pushes:
        if pushed.kind is PushKind.NOTICE and pushed.subject_key != _RUN_FAILED:
            notices.append(pushed)
        elif pushed.kind is PushKind.URGENT:
            urgent.append((pushed,))
        else:
            others.append((pushed,))
    notices.sort(key=_notice_order)
    chunks = [
        tuple(notices[i : i + MAX_ITEMS_PER_PUSH])
        for i in range(0, len(notices), MAX_ITEMS_PER_PUSH)
    ]
    return (*urgent, *chunks, *others)


__all__ = [
    "BODY_LIMIT",
    "GENERIC_DRIVER",
    "GENERIC_PASSENGER",
    "NAME_LIMIT",
    "RULE_TEMPLATES",
    "TITLE_LIMIT",
    "NameDirectory",
    "RenderedPush",
    "RuleTemplate",
    "chunk_pushes",
    "render",
]
