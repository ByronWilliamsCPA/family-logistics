"""Hermetic settings for unit tests.

``Settings`` reads ``FL_*`` variables and, for local development, a ``.env``
file. Neither may leak into a test: a developer shell with an exported
``FL_HA_TOKEN`` or a ``.env`` in the working directory must not change results.
Tests that exercise ``.env`` loading pass an explicit file to ``Settings``.
"""

from __future__ import annotations

import os

import pytest

from family_logistics.core.config import Settings


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ``FL_*`` variables and disable implicit ``.env`` discovery."""
    for name in [name for name in os.environ if name.upper().startswith("FL_")]:
        monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
