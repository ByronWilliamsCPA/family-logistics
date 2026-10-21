"""Confirmation revision mechanism (round-2 decision 3)."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fl_factories import FRIDAY_END, FRIDAY_START, notes, trip

from family_logistics.domain.confirmation import (
    RecordedConfirmation,
    canonical_material_text,
    confirmation_code,
    confirmation_findings,
    evaluate_confirmation,
    is_confirmation_current,
)
from family_logistics.domain.models import OccurrenceKind, TripStatus


def confirmed(base_notes: str, **kwargs):
    """Return a trip whose CONFIRM matches its own content."""
    draft = trip(base_notes, **kwargs)
    code = confirmation_code(draft)
    return dataclasses.replace(draft, confirm_code=code)


@pytest.mark.unit
class TestCode:
    def test_code_is_six_uppercase_hex(self):
        code = confirmation_code(trip(notes()))
        assert len(code) == 6
        assert code == code.upper()
        int(code, 16)

    def test_canonical_text_has_identity_and_material_fields(self):
        text = canonical_material_text(trip(notes()))
        assert text.splitlines()[0] == "FLC1"
        assert "OCCURRENCE=2026-10-23T15:30" in text
        assert "DRIVER=riley" in text
        assert "PASSENGER=sam>home" in text
        assert "STOP_ORDER=AUTO" in text

    @pytest.mark.parametrize(
        "change",
        [
            {"driver": "Morgan"},
            {"pickup": "School"},
            {"destinations": (("Sam", "Library"),)},
        ],
    )
    def test_material_change_changes_code(self, change):
        assert confirmation_code(trip(notes())) != confirmation_code(
            trip(notes(**change))
        )

    def test_time_change_changes_code(self):
        moved = trip(notes(), start=FRIDAY_START + timedelta(minutes=15))
        assert confirmation_code(moved) != confirmation_code(trip(notes()))

    def test_free_text_and_status_do_not_change_code(self):
        a = trip(notes(free="one", status="tentative"))
        b = trip(notes(free="two", status="confirmed"))
        assert confirmation_code(a) == confirmation_code(b)

    def test_same_content_on_another_date_has_another_code(self):
        week_later = trip(
            notes(),
            start=FRIDAY_START + timedelta(days=7),
            end=FRIDAY_END + timedelta(days=7),
        )
        assert confirmation_code(week_later) != confirmation_code(trip(notes()))


@pytest.mark.unit
class TestRecorded:
    def test_matching_code_is_current(self):
        t = confirmed(notes())
        assert is_confirmation_current(t)
        assert confirmation_findings(evaluate_confirmation(t, now=FRIDAY_START)) == []

    def test_tentative_is_conf_008(self):
        t = trip(notes(status="tentative"))
        state = evaluate_confirmation(t, now=FRIDAY_START)
        assert state.recorded is RecordedConfirmation.TENTATIVE
        assert [f.rule_id for f in confirmation_findings(state)] == ["CONF-008"]

    def test_confirmed_without_code_is_conf_008(self):
        state = evaluate_confirmation(trip(notes()), now=FRIDAY_START)
        assert state.recorded is RecordedConfirmation.MISSING_CODE
        assert confirmation_findings(state)[0].rule_id == "CONF-008"

    def test_edit_after_confirmation_makes_code_stale(self):
        original = confirmed(notes())
        edited = dataclasses.replace(
            trip(notes(driver="Morgan")), confirm_code=original.confirm_code
        )
        state = evaluate_confirmation(edited, now=FRIDAY_START)
        assert state.recorded is RecordedConfirmation.STALE_CODE
        assert not is_confirmation_current(edited)

    def test_driver_acceptance_is_always_unverified(self):
        state = evaluate_confirmation(confirmed(notes()), now=FRIDAY_START)
        assert state.is_current
        assert state.driver_acceptance == "unverified"


@pytest.mark.unit
class TestRecurrence:
    def test_series_code_confirms_unmodified_instances_of_any_date(self):
        week1 = trip(notes(), occurrence=OccurrenceKind.SERIES_INSTANCE)
        week2 = trip(
            notes(),
            occurrence=OccurrenceKind.SERIES_INSTANCE,
            recurrence_id="20261030T223000Z",
            start=FRIDAY_START + timedelta(days=7),
            end=FRIDAY_END + timedelta(days=7),
        )
        series_code = confirmation_code(week1)
        assert confirmation_code(week2) == series_code
        assert is_confirmation_current(
            dataclasses.replace(week2, confirm_code=series_code)
        )

    def test_override_with_series_code_needs_its_own_confirmation(self):
        instance = trip(notes(), occurrence=OccurrenceKind.SERIES_INSTANCE)
        series_code = confirmation_code(instance)
        override = trip(
            notes(confirm=series_code),
            occurrence=OccurrenceKind.OVERRIDE,
            recurrence_id="20261023T223000Z",
        )
        assert not is_confirmation_current(override)
        state = evaluate_confirmation(override, now=FRIDAY_START)
        assert state.recorded is RecordedConfirmation.STALE_CODE

    def test_override_with_its_own_code_is_confirmed(self):
        override = confirmed(
            notes(),
            occurrence=OccurrenceKind.OVERRIDE,
            recurrence_id="20261023T223000Z",
        )
        assert is_confirmation_current(override)

    def test_series_edit_does_not_reconfirm(self):
        old = trip(notes(), occurrence=OccurrenceKind.SERIES_INSTANCE)
        old_code = confirmation_code(old)
        edited = trip(
            notes(driver="Morgan", confirm=old_code),
            occurrence=OccurrenceKind.SERIES_INSTANCE,
        )
        assert not is_confirmation_current(edited)


@pytest.mark.unit
class TestHistory:
    def test_past_occurrence_snapshot_is_not_recomputed(self):
        t = confirmed(notes())
        snapshot = evaluate_confirmation(t, now=FRIDAY_START - timedelta(hours=2))
        assert snapshot.is_current
        later_edit = dataclasses.replace(t, status=TripStatus.TENTATIVE)
        after_end = FRIDAY_END + timedelta(days=1)
        again = evaluate_confirmation(later_edit, now=after_end, previous=snapshot)
        assert again is snapshot

    def test_future_occurrence_is_reevaluated(self):
        t = confirmed(notes())
        snapshot = evaluate_confirmation(t, now=FRIDAY_START - timedelta(days=1))
        edited = dataclasses.replace(t, status=TripStatus.TENTATIVE)
        again = evaluate_confirmation(
            edited, now=FRIDAY_START - timedelta(hours=1), previous=snapshot
        )
        assert again.recorded is RecordedConfirmation.TENTATIVE
