"""Notification text: templates, names, masking, chunking and run wording (ADR 0008).

The renderer is the content boundary: a ``PlannedPush`` carries the full
finding, so every assertion here is on the rendered title and body.
"""

from __future__ import annotations

import inspect
import re
import string
from contextlib import nullcontext
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from string import Formatter
from unittest.mock import patch

import pytest
from fl_factories import FRIDAY_START, REGISTRY, notes, trip
from fl_service import make_settings
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import family_logistics.domain.notification_text as text_module
from family_logistics.domain.engine import AssessmentResult, failed_result
from family_logistics.domain.lifecycle import Lifecycle, TrackedFinding
from family_logistics.domain.models import (
    Finding,
    PassengerKind,
    RunStatus,
    Severity,
)
from family_logistics.domain.notification_policy import (
    CLEAR_PHRASES,
    MAX_ITEMS_PER_PUSH,
    NotificationPolicy,
    Plan,
    PlannedPush,
    PushKind,
    plan,
    plan_unavailable,
)
from family_logistics.domain.notification_subjects import RideIndex, Subject
from family_logistics.domain.notification_text import (
    BODY_LIMIT,
    RULE_TEMPLATES,
    TITLE_LIMIT,
    NameDirectory,
    RenderedPush,
    RuleTemplate,
    chunk_pushes,
    render,
    status_counts,
)
from family_logistics.domain.rules import RULES, make_finding

pytestmark = pytest.mark.unit

CLEAR_REGEX = re.compile(
    r"\b(all[ -]clear|clear|no (open )?(issues|findings|problems)|all good"
    r"|all rides (are )?verified)\b",
    re.IGNORECASE,
)
"""The ADR D3 clear-wording regex."""
CLEAR_WORDS = [re.compile(rf"\b{re.escape(p)}\b", re.IGNORECASE) for p in CLEAR_PHRASES]
PLACEHOLDERS = {"who", "when", "driver", "source"}
ADDRESS = "742 Example Terrace"
"""An address-like string that must never be rendered."""

FRIDAY_LOCAL = "Fri 23 Oct 15:30"
"""``FRIDAY_START`` in America/Los_Angeles."""
ISSUE = "0123456789abcdef"
NOW = FRIDAY_START - timedelta(hours=1)


def policy(mode="shadow", **overrides) -> NotificationPolicy:
    return NotificationPolicy.from_settings(
        make_settings(notification_mode=mode, **overrides)
    )


def household() -> NameDirectory:
    """The fictional household as the service would load it."""
    return NameDirectory.from_rows(
        [(p.person_id, p.display_name, p.role.value) for p in REGISTRY.people],
        [(e.passenger_id, e.display_first_name) for e in REGISTRY.external_passengers],
    )


NAMES = household()
SAM = ("sam", PassengerKind.FAMILY)
ALEX = ("alex", PassengerKind.FAMILY)
EXT_E = ("ext-e", PassengerKind.EXTERNAL)


def subject(
    passengers=(SAM,),
    driver_id="riley",
    due_at=FRIDAY_START,
    due_end=FRIDAY_START + timedelta(minutes=45),
    day=None,
) -> Subject:
    return Subject(
        due_at=due_at,
        due_end=due_end,
        day=day,
        passengers=tuple(passengers),
        driver_id=driver_id,
    )


def finding(
    rule="DEST-004",
    keys=("cal|uid|-", "passenger:sam", "destination"),
    severity=None,
    message="free text that must never matter",
    suggestion="a suggestion that must never matter",
) -> Finding:
    return make_finding(rule, message, keys, suggestion=suggestion, severity=severity)


def push(
    found: Finding | None = None,
    *,
    kind=PushKind.URGENT,
    lifecycle=Lifecycle.NEW,
    subj: Subject | None = None,
    tag=None,
) -> PlannedPush:
    found = found or finding()
    tags = {
        PushKind.URGENT: "fl-urgent",
        PushKind.NOTICE: "fl-warning",
        PushKind.STATUS: "fl-status",
        PushKind.REPORT: "fl-report",
        PushKind.OPS: "fl-ops",
    }
    return PlannedPush(
        subject_key=f"issue:{found.issue_id}",
        kind=kind,
        intended_tag=tags[kind] if tag is None else tag,
        episode_start=lifecycle is not Lifecycle.ONGOING,
        rule_id=found.rule_id,
        severity=found.severity,
        tracked=TrackedFinding(found, lifecycle),
        subject=subj or subject(),
    )


def run_push(kind, status, key="run:run-2") -> PlannedPush:
    tags = {PushKind.STATUS: "fl-status", PushKind.REPORT: "fl-report"}
    return PlannedPush(
        subject_key=key, kind=kind, intended_tag=tags[kind], run_status=status
    )


def ops_push(key) -> PlannedPush:
    return PlannedPush(subject_key=key, kind=PushKind.OPS, intended_tag="fl-ops")


def render_one(pushed: PlannedPush, mode="shadow", **kwargs) -> RenderedPush:
    return render(
        [pushed], names=kwargs.pop("names", NAMES), policy=policy(mode), **kwargs
    )


def all_text(rendered: RenderedPush) -> str:
    return f"{rendered.title}\n{rendered.body}"


# ---------------------------------------------------------------------------
# T-text-1: one template per rule, fixed placeholders
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_every_rule_has_a_template(self):
        """T-text-1."""
        assert set(RULE_TEMPLATES) == set(RULES)

    def test_templates_use_only_the_four_placeholders(self):
        for rule_id, template in RULE_TEMPLATES.items():
            assert isinstance(template, RuleTemplate)
            used = {
                name
                for _, name, _, _ in Formatter().parse(template.line)
                if name is not None
            }
            assert used <= PLACEHOLDERS, rule_id
            assert [
                n for _, n, _, _ in Formatter().parse(template.label) if n is not None
            ] == [], rule_id

    @pytest.mark.parametrize("rule_id", sorted(RULES))
    def test_each_catalog_rule_renders_from_its_template(self, rule_id):
        found = finding(rule_id, keys=("cal|uid|-", "source:icloud"))
        rendered = render_one(push(found, subj=subject((SAM, EXT_E))))
        template = RULE_TEMPLATES[rule_id]
        assert template.label in rendered.title
        assert "A ride check needs attention" not in rendered.body
        assert f"ref {found.issue_id}" in rendered.body

    @pytest.mark.parametrize("rule_id", sorted(RULES))
    def test_a_rule_without_a_template_hits_the_fallback(self, rule_id, monkeypatch):
        monkeypatch.setattr(text_module, "RULE_TEMPLATES", {})
        found = finding(rule_id, severity=Severity.CRITICAL)
        rendered = render_one(push(found))
        assert rendered.title == (
            "[shadow] [CRITICAL] Urgent ride check: needs attention"
        )
        item = (
            f"- [CRITICAL] A ride check needs attention, at {FRIDAY_LOCAL}. "
            f"Rule {rule_id}, ref {found.issue_id}."
        )
        assert rendered.body.splitlines() == [
            "SHADOW: production would send this to fl-urgent (bypasses quiet hours).",
            item,
        ]

    def test_the_fallback_prints_a_rule_id_only_if_it_is_in_the_catalog(self):
        bogus = Finding("ZZZ-999 CANARYRULE", Severity.WARNING, "m", ("k",))
        rendered = render_one(push(bogus, kind=PushKind.NOTICE), mode="production")
        item = (
            f"- [WARNING] A ride check needs attention, at {FRIDAY_LOCAL}. "
            f"Ref {bogus.issue_id}."
        )
        assert rendered.body.splitlines() == [item]
        assert "ZZZ" not in all_text(rendered)
        assert "CANARYRULE" not in all_text(rendered)

    def test_the_fallback_omits_the_time_when_the_subject_has_none(self):
        bogus = Finding("ZZZ-999", Severity.WARNING, "m", ("k",))
        rendered = render_one(
            push(bogus, kind=PushKind.NOTICE, subj=Subject()), mode="production"
        )
        assert rendered.body == (
            f"- [WARNING] A ride check needs attention. Ref {bogus.issue_id}."
        )

    def test_a_finding_push_without_its_finding_still_renders(self):
        bare = PlannedPush(
            subject_key="issue:x", kind=PushKind.NOTICE, intended_tag="fl-warning"
        )
        rendered = render_one(bare, mode="production")
        assert rendered.body == ("- [COULD NOT VERIFY] A ride check needs attention.")


# ---------------------------------------------------------------------------
# Item wording
# ---------------------------------------------------------------------------


class TestItemWording:
    def test_an_urgent_finding_exactly(self):
        found = finding()
        rendered = render_one(push(found))
        item = (
            f"- [CRITICAL] Sam: the destination for the ride ({FRIDAY_LOCAL}) "
            f"is missing or unverified. (new, ref {found.issue_id})"
        )
        assert rendered.title == (
            "[shadow] [CRITICAL] Urgent ride check: destination missing or unverified"
        )
        assert rendered.body.splitlines() == [
            "SHADOW: production would send this to fl-urgent (bypasses quiet hours).",
            item,
        ]

    def test_production_mode_has_no_shadow_text(self):
        rendered = render_one(push(), mode="production")
        assert not rendered.title.startswith("[shadow]")
        assert "SHADOW" not in rendered.body
        assert rendered.title.startswith("[CRITICAL] Urgent ride check:")

    @pytest.mark.parametrize(
        ("severity", "label"),
        [
            (Severity.CRITICAL, "[CRITICAL]"),
            (Severity.WARNING, "[WARNING]"),
            (Severity.INCOMPLETE, "[COULD NOT VERIFY]"),
        ],
    )
    def test_severity_labels(self, severity, label):
        found = finding("DEST-004", severity=severity)
        rendered = render_one(push(found, kind=PushKind.NOTICE))
        assert rendered.title == (
            f"[shadow] {label} Ride check: destination missing or unverified"
        )
        assert f"- {label} Sam: the destination" in rendered.body

    @pytest.mark.parametrize(
        ("lifecycle", "word"),
        [
            (Lifecycle.NEW, "new"),
            (Lifecycle.REGRESSED, "back again"),
            (Lifecycle.ONGOING, "still open"),
        ],
    )
    def test_lifecycle_words(self, lifecycle, word):
        found = finding()
        rendered = render_one(push(found, lifecycle=lifecycle))
        assert f"({word}, ref {found.issue_id})" in rendered.body

    def test_a_notice_title_and_a_tag_note(self):
        rendered = render_one(push(finding("CONF-008"), kind=PushKind.NOTICE))
        assert rendered.title == (
            "[shadow] [WARNING] Ride check: ride tentative or changed"
        )
        assert rendered.body.splitlines()[0] == (
            "SHADOW: production would send this to fl-warning "
            "(held during quiet hours)."
        )

    def test_the_time_is_local_and_a_day_has_no_time(self):
        chicago = render(
            [push()],
            names=NAMES,
            policy=policy("production", timezone="America/Chicago"),
        )
        assert "(Fri 23 Oct 17:30)" in chicago.body
        day = subject(due_at=None, due_end=None, day=date(2026, 10, 5))
        rendered = render_one(push(finding("SCHOOL-009"), subj=day))
        assert "(Mon 5 Oct)" in rendered.body

    def test_an_unknown_time_says_so(self):
        rendered = render_one(push(subj=Subject()))
        assert "(time unknown)" in rendered.body

    def test_the_driver_is_named_where_the_template_asks(self):
        rendered = render_one(push(finding("TIME-007"), subj=subject((SAM,), "morgan")))
        assert "with Morgan" in rendered.body

    def test_many_passengers_are_summarised(self):
        crowd = subject((SAM, ALEX, EXT_E, ("jordan", PassengerKind.FAMILY)))
        rendered = render_one(push(finding("TIME-007"), subj=crowd))
        assert "Sam, Alex and 2 more" in rendered.body
        assert "Jordan" not in rendered.body

    def test_two_passengers_are_joined_with_and(self):
        rendered = render_one(push(subj=subject((SAM, ALEX))))
        assert "Sam and Alex:" in rendered.body

    def test_no_known_passenger_renders_a_generic_word(self):
        rendered = render_one(push(subj=subject(())))
        assert "a passenger:" in rendered.body


# ---------------------------------------------------------------------------
# T-text-3, 4, 5, 11: names
# ---------------------------------------------------------------------------


class TestNames:
    def test_external_child_rendered_as_initial(self):
        """T-text-3: each external renders its own initial, in subject order."""
        directory = NameDirectory.from_rows([], [("ext-e", "E"), ("ext-r", "R")])
        both = subject((("ext-r", PassengerKind.EXTERNAL), EXT_E))
        rendered = render_one(push(subj=both), names=directory)
        assert "- [CRITICAL] R. and E.: the destination" in rendered.body
        assert directory.passenger("ext-e", PassengerKind.EXTERNAL) == "E."
        assert directory.passenger("ext-r", PassengerKind.EXTERNAL) == "R."

    def test_a_swap_of_two_externals_is_visible_in_the_text(self):
        directory = NameDirectory.from_rows([], [("ext-e", "E"), ("ext-r", "R")])
        first = render_one(
            push(subj=subject((EXT_E,))), names=directory, mode="production"
        )
        second = render_one(
            push(subj=subject((("ext-r", PassengerKind.EXTERNAL),))),
            names=directory,
            mode="production",
        )
        assert "E.: the destination" in first.body
        assert "R.: the destination" in second.body
        assert first.body != second.body

    def test_driver_and_passenger_names_do_not_swap(self):
        directory = NameDirectory.from_rows(
            [
                ("morgan", "Morgan Example", "parent"),
                ("riley", "Riley Example", "parent"),
            ],
            [],
        )
        riley = ("riley", PassengerKind.FAMILY)
        rendered = render_one(
            push(finding("TIME-007"), subj=subject((riley,), "morgan")),
            names=directory,
            mode="production",
        )
        assert rendered.body.startswith(
            "- [CRITICAL] Riley: the pickup or drop-off (Fri 23 Oct 15:30) "
            "with Morgan is not feasible."
        )
        assert "Example" not in rendered.body

    def test_the_household_external_pseudonym_renders_its_initial(self):
        rendered = render_one(push(subj=subject((EXT_E,))))
        assert "P.:" in rendered.body
        assert "Passenger" not in all_text(rendered)

    def test_two_externals_with_one_initial_both_render_it(self):
        both = subject(
            (("ext-j1", PassengerKind.EXTERNAL), ("ext-j2", PassengerKind.EXTERNAL))
        )
        rendered = render_one(push(subj=both))
        assert "P. and P.:" in rendered.body

    def test_unregistered_and_unknown_ids_are_generic(self):
        """T-text-4."""
        assert (
            NAMES.passenger("unregistered:Quinn", PassengerKind.FAMILY) == "a passenger"
        )
        assert (
            NAMES.passenger("unregistered:Quinn", PassengerKind.EXTERNAL)
            == "a passenger"
        )
        assert NAMES.passenger("nobody", PassengerKind.FAMILY) == "a passenger"
        assert NAMES.passenger("nobody", PassengerKind.EXTERNAL) == "a passenger"
        assert NAMES.driver("nobody") == "the driver"
        assert NAMES.driver("unregistered:Quinn") == "the driver"
        assert NAMES.driver(None) == "the driver"
        assert NAMES.driver("riley") == "Riley"

    def test_an_unregistered_id_is_generic_even_if_a_row_carries_it(self):
        directory = NameDirectory.from_rows(
            [("unregistered:alex", "Alex", "child")],
            [("unregistered:e", "Passenger E")],
        )
        assert (
            directory.passenger("unregistered:alex", PassengerKind.FAMILY)
            == "a passenger"
        )
        assert (
            directory.passenger("unregistered:e", PassengerKind.EXTERNAL)
            == "a passenger"
        )
        assert directory.driver("unregistered:alex") == "the driver"

    def test_an_empty_directory_renders_generic_words(self):
        empty = NameDirectory.empty()
        rendered = render_one(
            push(finding("TIME-007"), subj=subject((SAM, EXT_E))), names=empty
        )
        assert "a passenger and a passenger:" in rendered.body
        assert "with the driver" in rendered.body
        for name in ("Sam", "Riley", "P."):
            assert name not in rendered.body

    def test_family_name_is_first_token_only(self):
        """T-text-5."""
        directory = NameDirectory.from_rows([("alex", "Alex Example", "child")], [])
        assert directory.passenger("alex", PassengerKind.FAMILY) == "Alex"
        rendered = render_one(push(subj=subject((ALEX,))), names=directory)
        assert "Example" not in all_text(rendered)
        assert "Alex" in rendered.body

    def test_first_token_is_filtered_and_cut(self):
        directory = NameDirectory.from_rows(
            [
                ("a", "O'Neil-Smith2 Example", "parent"),
                ("b", "Abcdefghijklmnopqrstuvwxyz Example", "child"),
                ("c", "123 Example", "child"),
                ("d", "   Padded   Example", "child"),
                ("e", "(Morgan) Example", "parent"),
                ("f", "", "child"),
                ("g", "-' Example", "child"),
            ],
            [],
        )
        assert directory.passenger("a", PassengerKind.FAMILY) == "O'Neil-Smith"
        assert directory.passenger("b", PassengerKind.FAMILY) == "Abcdefghijklmnopqrst"
        assert directory.passenger("c", PassengerKind.FAMILY) == "a passenger"
        assert directory.passenger("d", PassengerKind.FAMILY) == "Padded"
        assert directory.passenger("e", PassengerKind.FAMILY) == "Morgan"
        assert directory.passenger("f", PassengerKind.FAMILY) == "a passenger"
        assert directory.passenger("g", PassengerKind.FAMILY) == "a passenger"

    def test_family_names_come_from_children_and_parents_only(self):
        assert NAMES.passenger("morgan", PassengerKind.FAMILY) == "Morgan"
        assert NAMES.passenger("alex", PassengerKind.FAMILY) == "Alex"
        assert NAMES.passenger("ext-driver-s", PassengerKind.FAMILY) == "a passenger"
        assert NAMES.driver("ext-driver-s") == "Driver"

    def test_external_initial_skips_non_letters_and_uppercases(self):
        directory = NameDirectory.from_rows(
            [], [("a", "  7 zed"), ("b", "1234"), ("c", ""), ("d", "ßeta")]
        )
        assert directory.passenger("a", PassengerKind.EXTERNAL) == "Z."
        assert directory.passenger("b", PassengerKind.EXTERNAL) == "a passenger"
        assert directory.passenger("c", PassengerKind.EXTERNAL) == "a passenger"
        assert directory.passenger("d", PassengerKind.EXTERNAL) == "S."

    def test_names_looked_up_by_kind(self):
        """T-text-11."""
        directory = NameDirectory.from_rows(
            [("alex", "Alex", "child")], [("alex", "Passenger E")]
        )
        assert directory.passenger("alex", PassengerKind.EXTERNAL) == "P."
        assert directory.passenger("alex", PassengerKind.FAMILY) == "Alex"
        rendered = render_one(
            push(subj=subject((("alex", PassengerKind.EXTERNAL),))), names=directory
        )
        assert "Alex" not in all_text(rendered)

    def test_a_family_id_that_is_only_an_external_is_generic(self):
        directory = NameDirectory.from_rows([], [("kid", "Passenger E")])
        assert directory.passenger("kid", PassengerKind.FAMILY) == "a passenger"

    def test_a_contact_is_a_driver_but_never_a_family_passenger(self):
        directory = NameDirectory.from_rows([("c", "Driver S", "contact")], [])
        assert directory.driver("c") == "Driver"
        assert directory.passenger("c", PassengerKind.FAMILY) == "a passenger"

    def test_the_directory_is_immutable(self):
        with pytest.raises(TypeError):
            NAMES.family["alex"] = "Other"  # type: ignore[index]


# ---------------------------------------------------------------------------
# T-text-9: DATA-011 closed source map
# ---------------------------------------------------------------------------


class TestSourceMap:
    @pytest.mark.parametrize(
        ("keys", "word"),
        [
            (("source:icloud",), "calendar"),
            (("source:ha_snapshot",), "school schedule"),
            (("source:none",), "any data source"),
            (("source:cal-CANARYid",), "a data source"),
            (("source:icloudx",), "a data source"),
            (("icloud",), "a data source"),
            ((), "a data source"),
        ],
    )
    def test_data_011_source_map_is_closed(self, keys, word):
        """T-text-9."""
        found = finding("DATA-011", keys=keys)
        rendered = render_one(
            push(found, kind=PushKind.NOTICE, subj=Subject()), mode="production"
        )
        assert f"({word});" in rendered.body
        assert "CANARY" not in all_text(rendered)
        assert "cal-" not in all_text(rendered)

    def test_the_first_source_key_decides(self):
        found = finding("DATA-011", keys=("x", "source:ha_snapshot"))
        rendered = render_one(push(found, kind=PushKind.NOTICE, subj=Subject()))
        assert "(school schedule)" in rendered.body


# ---------------------------------------------------------------------------
# T-text-6, 7: clear wording and the shadow header
# ---------------------------------------------------------------------------


def is_clear_run(rendered: RenderedPush) -> bool:
    return bool(CLEAR_REGEX.search(all_text(rendered)))


ACCEPTANCE = "Driver acceptance not verified."
REASONS_LINE = (
    "Reasons: data sources: 1, travel times or places: 3, "
    "vehicle capacities: 2, other checks: 4."
)


class TestRunLevelWording:
    @pytest.mark.parametrize("kind", [PushKind.STATUS, PushKind.REPORT])
    def test_the_clear_text_uses_every_clear_phrase(self, kind):
        rendered = render_one(run_push(kind, RunStatus.CLEAR), mode="production")
        assert "No open findings; sources fresh; driver acceptance not verified." in (
            rendered.body
        )
        assert "Clear" in rendered.title
        assert all(w.search(all_text(rendered)) for w in CLEAR_WORDS)

    def test_status_and_report_titles(self):
        status = render_one(
            run_push(PushKind.STATUS, RunStatus.CLEAR), mode="production"
        )
        report = render_one(
            run_push(PushKind.REPORT, RunStatus.CLEAR), mode="production"
        )
        assert status.title == "Ride check: Clear"
        assert report.title == "Weekly ride report: Clear"

    def test_attention_required_gives_counts(self):
        rendered = render_one(
            run_push(PushKind.STATUS, RunStatus.ATTENTION_REQUIRED),
            mode="production",
            counts={"critical": 2, "warning": 1},
        )
        assert rendered.title == "Ride check: attention required"
        assert rendered.body == (
            "Open: 2 critical, 1 warning. Driver acceptance not verified."
        )
        assert not is_clear_run(rendered)

    def test_incomplete_status_lists_reasons_as_counts(self):
        """T-text-10 (incomplete half)."""
        counts = {
            "critical": 1,
            "warning": 2,
            "DATA-011": 1,
            "ROUTE-010": 3,
            "CAP-014": 2,
            "PARSE-001": 4,
            "BOGUS-999": 9,
            "incomplete": 99,
        }
        rendered = render_one(
            run_push(PushKind.STATUS, RunStatus.INCOMPLETE),
            mode="production",
            counts=counts,
        )
        assert rendered.title == "Ride check: could not fully verify"
        assert rendered.body.splitlines() == [
            "Ride check: could not fully verify.",
            REASONS_LINE,
            "Open: 1 critical, 2 warning. Driver acceptance not verified.",
        ]
        assert "BOGUS" not in rendered.body
        assert "99" not in rendered.body
        assert not is_clear_run(rendered)

    def test_incomplete_without_counts_still_says_so(self):
        rendered = render_one(
            run_push(PushKind.REPORT, RunStatus.INCOMPLETE), mode="production"
        )
        assert rendered.title == "Weekly ride report: could not fully verify"
        assert "could not fully verify" in rendered.body
        assert "Reasons" not in rendered.body
        assert not is_clear_run(rendered)

    @pytest.mark.parametrize("status", [None, RunStatus.FAILED])
    @pytest.mark.parametrize("kind", [PushKind.STATUS, PushKind.REPORT])
    def test_a_status_without_a_completed_run_is_never_clear(self, kind, status):
        rendered = render_one(run_push(kind, status), mode="production")
        assert rendered.title.endswith("check failed")
        assert "could not be verified" in rendered.body
        assert not is_clear_run(rendered)

    def test_counts_are_clamped_and_ignored_when_junk(self):
        rendered = render_one(
            run_push(PushKind.STATUS, RunStatus.ATTENTION_REQUIRED),
            mode="production",
            counts={"critical": -5, "warning": 3},
        )
        assert rendered.body == "Open: 0 critical, 3 warning. " + ACCEPTANCE

    def test_a_non_numeric_count_degrades_to_zero(self):
        junk: dict[str, object] = {
            "critical": "x",
            "warning": -1,
            "ROUTE-010": None,
            "DATA-011": 2.5,
            "CAP-014": float("nan"),
            "PARSE-001": True,
        }
        rendered = render_one(
            run_push(PushKind.STATUS, RunStatus.INCOMPLETE),
            mode="production",
            counts=junk,  # type: ignore[arg-type]
        )
        assert rendered.body.splitlines() == [
            "Ride check: could not fully verify.",
            "Open: 0 critical, 0 warning. " + ACCEPTANCE,
        ]

    @pytest.mark.parametrize("counts", [None, {}, {"critical": 0, "warning": 0}])
    def test_attention_required_never_claims_zero_open(self, counts):
        rendered = render_one(
            run_push(PushKind.STATUS, RunStatus.ATTENTION_REQUIRED),
            mode="production",
            counts=counts,
        )
        assert rendered.body == "Open findings need attention. " + ACCEPTANCE

    def test_the_clear_text_is_built_from_the_policy_phrases(self):
        rendered = render_one(
            run_push(PushKind.STATUS, RunStatus.CLEAR), mode="production"
        )
        word, no_findings, fresh = CLEAR_PHRASES
        assert rendered.title == f"Ride check: {word}"
        assert (
            rendered.body == f"{no_findings}; {fresh}; driver acceptance not verified."
        )

    def test_run_failed_push_wording(self):
        urgent = PlannedPush("run-failed", PushKind.URGENT, "fl-urgent", run_wide=True)
        notice = PlannedPush("run-failed", PushKind.NOTICE, "fl-warning", run_wide=True)
        urgent_text = render_one(urgent, mode="production")
        notice_text = render_one(notice, mode="production")
        assert (
            urgent_text.title
            == "Urgent: ride check failed, rides could not be verified"
        )
        assert notice_text.title == "Ride check failed, rides could not be verified"
        for rendered in (urgent_text, notice_text):
            assert "could not be verified" in rendered.body
            assert not is_clear_run(rendered)

    def test_ops_wording(self):
        failed = render_one(ops_push("ops:run_failed"), mode="production")
        incomplete = render_one(ops_push("ops:run_incomplete"), mode="production")
        other = render_one(ops_push("ops:something_else"), mode="production")
        assert failed.title == "Ops: ride check run failed"
        assert incomplete.title == "Ops: ride check could not fully verify"
        assert other.title == "Ops: service needs attention"
        for rendered in (failed, incomplete, other):
            assert not is_clear_run(rendered)

    def test_shadow_header_names_production_tag(self):
        """T-text-7."""
        cases = [
            (
                push(),
                "SHADOW: production would send this to fl-urgent (bypasses quiet hours).",
            ),
            (
                push(kind=PushKind.NOTICE),
                "SHADOW: production would send this to fl-warning (held during quiet hours).",
            ),
            (
                ops_push("ops:run_failed"),
                "SHADOW: production would send this to fl-ops (held during quiet hours).",
            ),
            (
                run_push(PushKind.STATUS, RunStatus.CLEAR),
                "SHADOW: production would send this to fl-status.",
            ),
            (
                run_push(PushKind.REPORT, RunStatus.CLEAR),
                "SHADOW: production would send this to fl-report.",
            ),
        ]
        for pushed, header in cases:
            rendered = render_one(pushed)
            assert rendered.title.startswith("[shadow] ")
            assert rendered.body.splitlines()[0] == header

    def test_the_header_uses_the_pushes_intended_tag_sanitised(self):
        rendered = render_one(push(tag="fl-custom CANARY\n{x}" + "z" * 80))
        first = rendered.body.splitlines()[0]
        assert first == (
            "SHADOW: production would send this to "
            f"fl-customCANARYx{'z' * 24} (bypasses quiet hours)."
        )

    @pytest.mark.parametrize("tag", ["", "  \n", "{}!"])
    def test_a_tag_that_sanitises_to_nothing_is_not_printed_blank(self, tag):
        urgent = render_one(push(tag=tag))
        status = render_one(
            PlannedPush(
                subject_key="run:r",
                kind=PushKind.STATUS,
                intended_tag=tag,
                run_status=RunStatus.CLEAR,
            )
        )
        assert urgent.body.splitlines()[0] == (
            "SHADOW: production would send this to an unknown tag "
            "(bypasses quiet hours)."
        )
        assert status.body.splitlines()[0] == (
            "SHADOW: production would send this to an unknown tag."
        )

    def test_clear_words_appear_nowhere_but_the_clear_text(self):
        """T-text-6 (templates and fixed text)."""
        for rule_id, template in RULE_TEMPLATES.items():
            for part in (template.label, template.line):
                assert not any(w.search(part) for w in CLEAR_WORDS), rule_id
        assert not CLEAR_REGEX.search(
            " ".join(t.label + t.line for t in RULE_TEMPLATES.values())
        )
        for mode in ("shadow", "production"):
            non_clear = [
                push(kind=PushKind.URGENT),
                push(kind=PushKind.NOTICE),
                run_push(PushKind.STATUS, RunStatus.ATTENTION_REQUIRED),
                run_push(PushKind.REPORT, RunStatus.INCOMPLETE),
                run_push(PushKind.STATUS, RunStatus.FAILED),
                run_push(PushKind.STATUS, None),
                ops_push("ops:run_failed"),
                ops_push("ops:run_incomplete"),
                PlannedPush("run-failed", PushKind.URGENT, "fl-urgent"),
                PlannedPush("run-failed", PushKind.NOTICE, "fl-warning"),
            ]
            for pushed in non_clear:
                rendered = render_one(pushed, mode, counts={"critical": 1})
                assert not is_clear_run(rendered)
                assert not any(w.search(all_text(rendered)) for w in CLEAR_WORDS)

    def test_the_regex_itself_passes_unclear(self):
        assert not CLEAR_REGEX.search("unclear")
        assert CLEAR_REGEX.search("all clear")


# ---------------------------------------------------------------------------
# Never clear for a failed, incomplete or unavailable run (properties)
# ---------------------------------------------------------------------------

# No per-example deadline: each example renders whole push sets, and the default
# 200 ms limit would make a loaded CI runner fail a correct property.
PROPS = settings(
    max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
RIDE = trip(notes(driver="Riley"), uid="ride")
RIDE_KEY = RIDE.key.as_str()
RIDES = RideIndex.build(current=(RIDE,), previous=(), unparsed=())
EMPTY_RIDES = RideIndex.build(current=(), previous=(), unparsed=())
TRIGGERS = st.sampled_from(["daily", "weekly", "on_demand", "incremental"])
MODES = st.sampled_from(["shadow", "production"])
RULE_IDS = st.sampled_from(sorted(RULES))
SEVERITIES = st.sampled_from(list(Severity))
LIFECYCLES = st.sampled_from(list(Lifecycle))
KINDS = st.sampled_from([PushKind.URGENT, PushKind.NOTICE])
OFFSETS = st.integers(min_value=-24 * 60, max_value=24 * 60)


@st.composite
def findings(draw, canary=None):
    rule = draw(RULE_IDS)
    severity = draw(SEVERITIES)
    tail = canary or "k"
    keys = draw(
        st.sampled_from(
            [
                (RIDE_KEY, "route"),
                (RIDE_KEY, "passenger:sam", "destination"),
                ("source:icloud",),
                ("person:alex", "day:2026-10-23", "school"),
                (f"cal-{tail}|uid-{tail}|-", f"passenger:{tail}", f"source:{tail}"),
            ]
        )
    )
    return make_finding(
        rule, f"{tail} {ADDRESS}", keys, suggestion=f"{tail} fix", severity=severity
    )


def planned_of(result, trigger, rides, now, mode):
    return plan(
        run_id="run-2",
        trigger=trigger,
        result=result,
        rides=rides,
        now=now,
        policy=policy(mode),
    )


def render_all(built: Plan, mode: str, counts=None) -> list[RenderedPush]:
    return [
        render(chunk, names=NAMES, policy=policy(mode), counts=counts)
        for chunk in chunk_pushes(built.pushes)
    ]


class TestNeverClear:
    @PROPS
    @given(st.lists(findings(), max_size=5), TRIGGERS, st.booleans(), OFFSETS, MODES)
    def test_a_failed_run_never_renders_clear_wording(
        self, found, trigger, with_rides, offset, mode
    ):
        """T-text-6 (failed run)."""
        result = failed_result(NOW, "boom CANARYERR", found)
        built = planned_of(
            result,
            trigger,
            RIDES if with_rides else None,
            NOW + timedelta(minutes=offset),
            mode,
        )
        rendered = render_all(built, mode)
        assert rendered
        for each in rendered:
            assert not is_clear_run(each)
            assert not any(w.search(all_text(each)) for w in CLEAR_WORDS)

    @PROPS
    @given(OFFSETS, MODES)
    def test_an_unavailable_run_never_renders_clear_wording(self, offset, mode):
        built = plan_unavailable(
            now=NOW + timedelta(minutes=offset), policy=policy(mode)
        )
        rendered = render_all(built, mode)
        assert len(rendered) == 2
        assert all(not is_clear_run(each) for each in rendered)

    @PROPS
    @given(st.lists(findings(), max_size=5), TRIGGERS, st.booleans(), OFFSETS, MODES)
    def test_an_incomplete_run_never_renders_clear_wording(
        self, found, trigger, with_rides, offset, mode
    ):
        result = AssessmentResult(
            RunStatus.INCOMPLETE,
            tuple(TrackedFinding(f, Lifecycle.NEW) for f in found),
            (),
            (),
            NOW,
        )
        built = planned_of(
            result,
            trigger,
            RIDES if with_rides else EMPTY_RIDES,
            NOW + timedelta(minutes=offset),
            mode,
        )
        counts = {"critical": 1, "warning": 1, "DATA-011": 1}
        for each in render_all(built, mode, counts):
            assert not is_clear_run(each)

    @PROPS
    @given(
        st.lists(findings(), min_size=1, max_size=5),
        TRIGGERS,
        MODES,
        st.sampled_from([RunStatus.ATTENTION_REQUIRED, RunStatus.INCOMPLETE]),
    )
    def test_an_open_finding_is_never_reported_as_clear(
        self, found, trigger, mode, status
    ):
        result = AssessmentResult(
            status, tuple(TrackedFinding(f, Lifecycle.NEW) for f in found), (), (), NOW
        )
        built = planned_of(result, trigger, RIDES, NOW, mode)
        for each in render_all(built, mode):
            assert not is_clear_run(each)

    @PROPS
    @given(TRIGGERS, MODES)
    def test_only_a_clear_run_renders_clear_wording(self, trigger, mode):
        result = AssessmentResult(RunStatus.CLEAR, (), (), (), NOW)
        built = planned_of(result, trigger, RIDES, NOW, mode)
        rendered = render_all(built, mode)
        assert all(is_clear_run(each) for each in rendered)
        assert len(rendered) == (0 if trigger == "incremental" else 1)


# ---------------------------------------------------------------------------
# T-text-2: no free text reaches a payload
# ---------------------------------------------------------------------------

TOKENS = st.text(alphabet=string.ascii_letters + string.digits, min_size=4, max_size=12)
SUBJECTS = st.sampled_from(
    [
        subject(),
        subject((SAM, EXT_E)),
        subject((), driver_id=None),
        subject(due_at=None, due_end=None, day=date(2026, 10, 23)),
        subject(
            (("unregistered:CANARYname", PassengerKind.FAMILY),),
            "unregistered:CANARYdrv",
        ),
        subject((("CANARYid", PassengerKind.EXTERNAL),), "CANARYdrv"),
        Subject(),
    ]
)


RULE_IDS_WITH_UNKNOWN = st.sampled_from(
    [*sorted(RULES), "ZZZ-999", "ZZZ-999 CANARYRULE"]
)


def without_templates(enabled):
    """Patch ``RULE_TEMPLATES`` to empty so the fixed fallback is what renders."""
    if enabled:
        return patch.object(text_module, "RULE_TEMPLATES", {})
    return nullcontext()


class TestContentBoundary:
    @PROPS
    @given(
        TOKENS,
        RULE_IDS_WITH_UNKNOWN,
        SEVERITIES,
        LIFECYCLES,
        KINDS,
        MODES,
        SUBJECTS,
        st.booleans(),
    )
    def test_no_free_text_reaches_a_payload(
        self, token, rule, severity, lifecycle, kind, mode, subj, no_templates
    ):
        """T-text-2, through the templates and through the fallback."""
        canary = f"CANARY{token}"
        keys = (
            f"cal-{canary}|uid-{canary}|-",
            f"passenger:{canary}",
            f"family-name:{canary}",
            f"external-name:{canary}",
            f"source:{canary}",
            f"parse:{canary}",
        )
        found = Finding(
            rule,
            severity,
            f"{canary} {ADDRESS} {canary}-pickup {canary}-free-notes",
            keys,
            suggestion=f"{canary} move it to {ADDRESS}",
        )
        with without_templates(no_templates):
            rendered = render_one(
                push(found, kind=kind, lifecycle=lifecycle, subj=subj), mode
            )
        shown = all_text(rendered)
        for forbidden in (canary, "CANARY", ADDRESS, "Example", "cal-", "uid-", "|"):
            assert forbidden not in shown
        assert found.message not in shown
        assert found.suggestion not in shown
        assert all(key not in shown for key in found.entity_keys)

    @PROPS
    @given(
        TOKENS,
        st.lists(RULE_IDS, min_size=1, max_size=6),
        TRIGGERS,
        MODES,
        st.booleans(),
        st.booleans(),
    )
    def test_no_free_text_survives_planning_and_chunking(
        self, token, rules, trigger, mode, failed, no_templates
    ):
        canary = f"CANARY{token}"
        found = [
            make_finding(
                rule,
                f"{canary} {ADDRESS}",
                (
                    f"cal-{canary}|uid-{canary}|-",
                    f"source:{canary}",
                    f"passenger:{canary}",
                ),
                suggestion=canary,
                severity=severity,
            )
            for rule in rules
            for severity in (Severity.CRITICAL, Severity.WARNING, Severity.INCOMPLETE)
        ]
        if failed:
            result = failed_result(NOW, f"{canary} error text {ADDRESS}", found)
        else:
            result = AssessmentResult(
                RunStatus.ATTENTION_REQUIRED,
                tuple(TrackedFinding(f, Lifecycle.NEW) for f in found),
                (),
                (),
                NOW,
            )
        built = planned_of(result, trigger, RIDES, NOW, mode)
        with without_templates(no_templates):
            rendered = render_all(built, mode)
        assert rendered
        for each in rendered:
            shown = all_text(each)
            assert "CANARY" not in shown
            assert "Example" not in shown
            assert "error text" not in shown

    def test_trip_text_never_reaches_a_payload(self):
        """Pickup, destination, free notes and the event UID stay out."""
        risky = trip(
            notes(
                pickup="Music Studio",
                destinations=(("Sam", "HOME"),),
                free="bring the blue cello case",
            ),
            uid="uid-CANARYuid",
        )
        found = make_finding(
            "DEST-004",
            "Music Studio then HOME bring the blue cello case",
            (risky.key.as_str(), "passenger:sam", "destination"),
            suggestion="Music Studio",
        )
        for mode in ("shadow", "production"):
            shown = all_text(render_one(push(found), mode))
            for forbidden in (
                "Music Studio",
                "HOME",
                "cello",
                "CANARYuid",
                "cal-family",
            ):
                assert forbidden not in shown

    def test_an_error_text_never_reaches_a_payload(self):
        """T-text-10 (failed half)."""
        result = failed_result(NOW, "ConnectionError to host.example CANARYERR", ())
        built = planned_of(result, "daily", None, NOW, "shadow")
        for each in render_all(built, "shadow"):
            assert "CANARYERR" not in all_text(each)
            assert "host.example" not in all_text(each)
            assert "ConnectionError" not in all_text(each)

    def test_the_friday_external_passenger_flow_renders_only_initials(self):
        """The Friday scenario end to end: the dropped leg names the child by initial."""
        friday = trip(
            notes(
                driver="Riley",
                family="Sam",
                external="Passenger E",
                pickup="Music Studio",
                destinations=(("Sam", "HOME"), ("Passenger E", "DEFAULT")),
            ),
            uid="friday-lesson",
        )
        found = make_finding(
            "PASS-003",
            "Passenger E lost a ride to 742 Example Terrace",
            (friday.key.as_str(), "passenger:ext-e"),
            severity=Severity.CRITICAL,
        )
        result = AssessmentResult(
            RunStatus.ATTENTION_REQUIRED,
            (TrackedFinding(found, Lifecycle.NEW),),
            (),
            (),
            friday.start - timedelta(hours=1),
        )
        rides = RideIndex.build(current=(), previous=(friday,), unparsed=())
        built = planned_of(
            result, "on_demand", rides, friday.start - timedelta(hours=1), "production"
        )
        item = next(
            r
            for r in render_all(built, "production", {"critical": 1})
            if r.title.startswith("[CRITICAL]")
        )
        assert item.body.startswith("- [CRITICAL] P.: a ride (Fri 23 Oct 15:30)")
        assert "driver was Riley" in item.body
        shown = all_text(item)
        assert "Passenger" not in shown
        assert "Example" not in shown
        assert "Sam" not in shown


# ---------------------------------------------------------------------------
# T-text-8 and chunking
# ---------------------------------------------------------------------------

LONG = "Abcdefghijklmnopqrstuvwxyz"


def max_names() -> NameDirectory:
    people = [(f"p{i}", f"{LONG} Example", "child") for i in range(6)]
    people.append(("drv", f"{LONG} Example", "contact"))
    return NameDirectory.from_rows(people, [(f"x{i}", LONG) for i in range(6)])


def crowd_subject() -> Subject:
    kids = tuple((f"p{i}", PassengerKind.FAMILY) for i in range(6))
    kids += tuple((f"x{i}", PassengerKind.EXTERNAL) for i in range(6))
    return subject(kids, "drv")


class TestLengths:
    @pytest.mark.parametrize("mode", ["shadow", "production"])
    @pytest.mark.parametrize("rule_id", sorted(RULES))
    def test_lengths_within_client_limits(self, rule_id, mode):
        """T-text-8: a full chunk at maximum name lengths."""
        chunk = [
            push(
                finding(
                    rule_id,
                    keys=(f"k{i}", "source:icloud"),
                    severity=Severity.INCOMPLETE,
                ),
                kind=PushKind.NOTICE,
                lifecycle=Lifecycle.REGRESSED,
                subj=crowd_subject(),
            )
            for i in range(MAX_ITEMS_PER_PUSH)
        ]
        rendered = render(chunk, names=max_names(), policy=policy(mode))
        assert len(rendered.title) <= TITLE_LIMIT
        assert len(rendered.body) <= BODY_LIMIT
        assert rendered.body.count("ref ") == MAX_ITEMS_PER_PUSH
        assert LONG not in all_text(rendered)

    def test_an_oversized_chunk_drops_whole_lines_and_says_so(self):
        chunk = [
            push(
                finding("TIME-007", keys=(f"k{i}",)),
                kind=PushKind.NOTICE,
                subj=crowd_subject(),
            )
            for i in range(40)
        ]
        rendered = render(chunk, names=max_names(), policy=policy("shadow"))
        assert len(rendered.body) <= BODY_LIMIT
        assert re.search(r"and \d+ more", rendered.body.splitlines()[-1])
        assert all(
            line.startswith(("- ", "SHADOW", "...", "and "))
            for line in rendered.body.splitlines()
        )

    def test_a_chunk_title_counts_its_items(self):
        chunk = [push(finding(keys=(f"k{i}",)), kind=PushKind.NOTICE) for i in range(3)]
        rendered = render(chunk, names=NAMES, policy=policy("production"))
        assert rendered.title == "Ride check: 3 items need attention"
        assert rendered.body.count("\n") == 2

    def test_a_very_long_title_is_clipped(self):
        long_label = RuleTemplate("x" * 200, "{who}")
        with patch.object(text_module, "RULE_TEMPLATES", {"DEST-004": long_label}):
            rendered = render_one(push())
        assert len(rendered.title) == TITLE_LIMIT
        assert rendered.title.endswith("...")
        assert rendered.title.startswith("[shadow] [CRITICAL] Urgent ride check: xxx")

    @pytest.mark.parametrize(
        "other",
        [
            run_push(PushKind.STATUS, RunStatus.CLEAR),
            run_push(PushKind.REPORT, RunStatus.ATTENTION_REQUIRED),
            ops_push("ops:run_failed"),
            PlannedPush("run-failed", PushKind.URGENT, "fl-urgent"),
        ],
    )
    def test_render_refuses_a_chunk_mixing_run_text_with_findings(self, other):
        mixed = [push(kind=PushKind.NOTICE), other]
        reversed_mix = [other, push(kind=PushKind.NOTICE)]
        shown_policy = policy()
        with pytest.raises(ValueError, match="findings"):
            render(mixed, names=NAMES, policy=shown_policy)
        with pytest.raises(ValueError, match="findings"):
            render(reversed_mix, names=NAMES, policy=shown_policy)

    def test_render_refuses_an_empty_chunk(self):
        empty: list[PlannedPush] = []
        shown_policy = policy()
        with pytest.raises(ValueError, match="at least one"):
            render(empty, names=NAMES, policy=shown_policy)


class TestChunking:
    def test_notices_are_chunked_most_severe_and_soonest_first(self):
        start = FRIDAY_START
        order = []
        pushes = []
        for i in range(19):
            severity = (Severity.WARNING, Severity.INCOMPLETE, Severity.CRITICAL)[i % 3]
            due = start + timedelta(hours=(19 - i))
            pushes.append(
                push(
                    finding("DEST-004", keys=(f"k{i}",), severity=severity),
                    kind=PushKind.NOTICE,
                    subj=subject(due_at=due, due_end=due + timedelta(minutes=30)),
                )
            )
            order.append((severity, due))
        chunks = chunk_pushes(pushes)
        assert [len(c) for c in chunks] == [8, 8, 3]
        flat = [p for c in chunks for p in c]
        assert sorted(map(id, flat)) == sorted(map(id, pushes))
        ranks = {Severity.CRITICAL: 0, Severity.INCOMPLETE: 1, Severity.WARNING: 2}
        keys = [(ranks[p.severity], p.subject.due_at) for p in flat]
        assert keys == sorted(keys)

    def test_other_kinds_are_one_per_chunk_and_order_is_kept(self):
        urgent = push(kind=PushKind.URGENT)
        notices = [
            push(finding(keys=(f"k{i}",)), kind=PushKind.NOTICE) for i in range(2)
        ]
        status = run_push(PushKind.STATUS, RunStatus.CLEAR)
        ops = ops_push("ops:run_failed")
        chunks = chunk_pushes([urgent, *notices, status, ops])
        assert [len(c) for c in chunks] == [1, 2, 1, 1]
        assert chunks[0] == (urgent,)
        assert chunks[2] == (status,)
        assert chunks[3] == (ops,)

    def test_urgent_pushes_are_never_grouped(self):
        urgents = [push(finding(keys=(f"u{i}",))) for i in range(3)]
        assert [len(c) for c in chunk_pushes(urgents)] == [1, 1, 1]

    def test_no_pushes_no_chunks(self):
        assert chunk_pushes([]) == ()

    def test_a_notice_without_a_time_sorts_last(self):
        timed = push(finding(keys=("a",)), kind=PushKind.NOTICE)
        untimed = push(finding(keys=("b",)), kind=PushKind.NOTICE, subj=Subject())
        (chunk,) = chunk_pushes([untimed, timed])
        assert chunk == (timed, untimed)

    def test_naive_and_aware_times_sort_together_as_utc(self):
        aware = push(
            finding(keys=("a",)),
            kind=PushKind.NOTICE,
            subj=subject(due_at=datetime(2026, 10, 23, 22, 30, tzinfo=UTC)),
        )
        naive_earlier = push(
            finding(keys=("b",)),
            kind=PushKind.NOTICE,
            subj=subject(due_at=datetime.fromisoformat("2026-10-23T22:00:00")),
        )
        (chunk,) = chunk_pushes([aware, naive_earlier])
        assert chunk == (naive_earlier, aware)

    def test_chunks_use_the_policy_item_limit(self):
        many = [push(finding(keys=(f"k{i}",)), kind=PushKind.NOTICE) for i in range(9)]
        assert [len(c) for c in chunk_pushes(many)] == [MAX_ITEMS_PER_PUSH, 1]


class TestRenderedPush:
    def test_it_is_frozen(self):
        rendered = render_one(push())
        with pytest.raises(AttributeError):
            rendered.title = "x"  # type: ignore[misc]

    def test_render_reads_no_clock_and_logs_nothing(self):
        source = inspect.getsource(text_module)
        for needle in (
            ".now(",
            ".today(",
            "utcnow",
            "time.time(",
            "monotonic(",
            "getLogger",
            "import logging",
            "structlog",
            "open(",
        ):
            assert needle not in source

    def test_a_time_before_the_epoch_does_not_matter(self):
        early = subject(due_at=datetime(1999, 1, 1, tzinfo=UTC), due_end=None)
        assert "(Thu 31 Dec 16:00)" in render_one(push(subj=early)).body


# ---------------------------------------------------------------------------
# status_counts: the pinned shape render() expects
# ---------------------------------------------------------------------------


def open_result(*found: Finding, status=RunStatus.INCOMPLETE) -> AssessmentResult:
    return AssessmentResult(
        status, tuple(TrackedFinding(f, Lifecycle.NEW) for f in found), (), (), NOW
    )


class TestStatusCounts:
    def test_it_pins_the_keys_and_counts(self):
        many = [
            finding("TIME-007", keys=("a",)),
            finding("DEST-004", keys=("b",)),
            finding("CONF-008", keys=("c",)),
            finding("ROUTE-010", keys=("d",)),
            finding("ROUTE-010", keys=("e",)),
            finding("ROUTE-010", keys=("f",)),
            finding("DATA-011", keys=("source:icloud",)),
            finding("CAP-014", keys=("g",), severity=Severity.INCOMPLETE),
            finding("CAP-014", keys=("h",), severity=Severity.WARNING),
        ]
        counts = status_counts(open_result(*many))
        assert dict(counts) == {
            "critical": 2,
            "warning": 2,
            "ROUTE-010": 3,
            "DATA-011": 1,
            "CAP-014": 1,
        }

    def test_an_empty_result_has_zero_open_counts_only(self):
        assert dict(status_counts(open_result(status=RunStatus.CLEAR))) == {
            "critical": 0,
            "warning": 0,
        }

    def test_it_is_not_the_engine_counts_shape(self):
        result = open_result(finding("ROUTE-010", keys=("a",)))
        assert "incomplete" in result.counts()
        assert "incomplete" not in status_counts(result)

    def test_a_failed_result_counts_its_carried_findings(self):
        failed = failed_result(NOW, "boom", [finding(keys=("a",))])
        assert status_counts(failed)["critical"] == 1

    def test_an_incomplete_run_shows_its_reasons_in_the_rendered_status(self):
        result = open_result(
            finding("ROUTE-010", keys=("a",)),
            finding("ROUTE-010", keys=("b",)),
            finding("DATA-011", keys=("source:ha_snapshot",)),
            finding("TIME-007", keys=("c",)),
        )
        rendered = render_one(
            run_push(PushKind.STATUS, RunStatus.INCOMPLETE),
            mode="production",
            counts=status_counts(result),
        )
        assert rendered.body.splitlines() == [
            "Ride check: could not fully verify.",
            "Reasons: data sources: 1, travel times or places: 2.",
            "Open: 1 critical, 0 warning. " + ACCEPTANCE,
        ]

    def test_the_engine_counts_would_lose_the_reasons(self):
        """Why PR 5 must not pass ``result.counts()``."""
        result = open_result(finding("ROUTE-010", keys=("a",)))
        rendered = render_one(
            run_push(PushKind.STATUS, RunStatus.INCOMPLETE),
            mode="production",
            counts=result.counts(),
        )
        assert "Reasons" not in rendered.body


# ---------------------------------------------------------------------------
# escalated: the "now critical" lifecycle word (ADR D2)
# ---------------------------------------------------------------------------


class TestEscalated:
    def test_the_field_is_last_and_defaults_false(self):
        names = [f.name for f in fields(PlannedPush)]
        assert names[-1] == "escalated"
        assert PlannedPush("k", PushKind.NOTICE, "fl-warning").escalated is False

    @pytest.mark.parametrize("lifecycle", list(Lifecycle))
    def test_an_escalated_critical_finding_says_now_critical(self, lifecycle):
        found = finding()
        escalated = replace(push(found, lifecycle=lifecycle), escalated=True)
        rendered = render_one(escalated)
        assert f"(now critical, ref {found.issue_id})" in rendered.body
        for word in ("new", "back again", "still open"):
            assert f"({word}," not in rendered.body

    def test_an_escalated_incomplete_finding_is_not_called_critical(self):
        found = finding("ROUTE-010", severity=Severity.INCOMPLETE)
        rendered = render_one(replace(push(found), escalated=True))
        assert f"(now could not verify, ref {found.issue_id})" in rendered.body
        assert "now critical" not in rendered.body

    def test_not_escalated_keeps_the_lifecycle_word(self):
        found = finding()
        rendered = render_one(push(found, lifecycle=Lifecycle.ONGOING))
        assert f"(still open, ref {found.issue_id})" in rendered.body
