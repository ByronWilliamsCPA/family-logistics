"""Every setting is read by code or carries a milestone: no silently dead config."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from family_logistics.core.config import CONSUMED, FIELD_STATUS, Settings

if TYPE_CHECKING:
    from pydantic import BaseModel

SRC = Path(__file__).resolve().parents[2] / "src" / "family_logistics"
CONFIG_MODULE = SRC / "core" / "config.py"
MILESTONES = {"M1", "M2", "M3", "M4", "M5"}
SETTINGS_EXPRESSIONS = {
    "settings",
    "self.settings",
    "self._settings",
    "get_settings()",
}
"""How production code reaches a settings object. A read through any other
spelling is invisible to the scan below; add the spelling here when one is
introduced, so the scan keeps seeing it."""


def unlisted_fields(model: type[BaseModel], status: dict[str, str]) -> set[str]:
    """Return model fields that have no FIELD_STATUS entry."""
    return set(model.model_fields) - set(status)


def stale_entries(model: type[BaseModel], status: dict[str, str]) -> set[str]:
    """Return FIELD_STATUS entries that name no model field."""
    return set(status) - set(model.model_fields)


def fields_read_in_src() -> set[str]:
    """Return attribute names read from a settings object outside config.py."""
    read: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path == CONFIG_MODULE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and ast.unparse(node.value) in SETTINGS_EXPRESSIONS
            ):
                read.add(node.attr)
    return read


@pytest.mark.unit
class TestFieldStatus:
    def test_every_field_has_an_entry(self):
        assert unlisted_fields(Settings, FIELD_STATUS) == set()

    def test_no_entry_names_a_removed_field(self):
        assert stale_entries(Settings, FIELD_STATUS) == set()

    def test_values_are_consumed_or_a_milestone(self):
        assert set(FIELD_STATUS.values()) <= {CONSUMED} | MILESTONES

    def test_a_new_unlisted_field_is_detected(self):
        class WithNewField(Settings):
            brand_new_setting: int = 1

        assert unlisted_fields(WithNewField, FIELD_STATUS) == {"brand_new_setting"}

    def test_a_removed_field_entry_is_detected(self):
        status = {**FIELD_STATUS, "ghost_setting": "M3"}
        assert stale_entries(Settings, status) == {"ghost_setting"}

    def test_every_consumed_field_is_read_by_code(self):
        consumed = {name for name, state in FIELD_STATUS.items() if state == CONSUMED}
        assert consumed - fields_read_in_src() == set()

    def test_the_scan_sees_real_reads(self):
        assert {"port", "ha_token", "database_path"} <= fields_read_in_src()

    def test_the_scan_does_not_credit_unread_fields(self):
        # apprise_url has no reader yet; it must be labelled with a milestone.
        assert "apprise_url" not in fields_read_in_src()
        assert FIELD_STATUS["apprise_url"] in MILESTONES
