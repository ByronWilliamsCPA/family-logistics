"""Constant-time bearer token check for Home Assistant routes."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from family_logistics.api.ha import verify_ha_token

TOKEN = SecretStr("t" * 40)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "expected", "ok"),
    [
        (f"Bearer {'t' * 40}", TOKEN, True),
        (f"Bearer {'t' * 39}", TOKEN, False),
        (f"bearer {'t' * 40}", TOKEN, False),
        ("t" * 40, TOKEN, False),
        (None, TOKEN, False),
        ("Bearer ", TOKEN, False),
        (f"Bearer {'t' * 40}", None, False),
    ],
)
def test_verify_ha_token(header, expected, ok):
    assert verify_ha_token(header, expected) is ok
