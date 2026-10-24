"""Hermetic settings for unit tests.

``Settings`` reads ``FL_*`` variables and, for local development, a ``.env``
file. Neither may leak into a test: a developer shell with an exported
``FL_HA_TOKEN`` or a ``.env`` in the working directory must not change results.
Tests that exercise ``.env`` loading pass an explicit file to ``Settings``.
"""

from __future__ import annotations

import base64
import os

import pytest
from pydantic import SecretStr

from family_logistics.core.config import Settings


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ``FL_*`` variables and disable implicit ``.env`` discovery."""
    for name in [name for name in os.environ if name.upper().startswith("FL_")]:
        monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


def make_audit_key(fill: int = 0) -> str:
    """Return a valid ``FL_AUDIT_KEY`` value built at run time.

    Built from bytes rather than written as a literal so no high-entropy string
    sits in the repository for secret scanners to flag.

    Args:
        fill: Offset added to each byte, so different values give different keys.

    Returns:
        44 characters of url-safe base64 for 32 bytes.
    """
    return base64.urlsafe_b64encode(bytes((i + fill) % 256 for i in range(32))).decode()


@pytest.fixture
def audit_key_text() -> str:
    """A valid audit key as the environment would carry it."""
    return make_audit_key()


@pytest.fixture
def audit_key(audit_key_text: str) -> SecretStr:
    """A valid audit key as ``Settings.audit_key`` holds it."""
    return SecretStr(audit_key_text)


@pytest.fixture
def other_audit_key() -> SecretStr:
    """A second valid audit key that differs from ``audit_key``."""
    return SecretStr(make_audit_key(fill=1))
