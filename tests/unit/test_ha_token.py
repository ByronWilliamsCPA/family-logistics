"""Constant-time bearer token check for Home Assistant routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import SecretStr

from family_logistics.api import ha
from family_logistics.api.ha import verify_ha_token

TOKEN = SecretStr("t" * 40)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "expected", "ok"),
    [
        (f"Bearer {'t' * 40}", TOKEN, True),
        # RFC 7235 section 2.1 / RFC 6750: the auth scheme is case-insensitive.
        (f"bearer {'t' * 40}", TOKEN, True),
        (f"BEARER {'t' * 40}", TOKEN, True),
        (f"Bearer  {'t' * 40}", TOKEN, True),
        (f"Bearer {'t' * 39}", TOKEN, False),
        (f"Bearer {'t' * 41}", TOKEN, False),
        # The token itself is case-sensitive.
        (f"Bearer {'T' * 40}", TOKEN, False),
        ("t" * 40, TOKEN, False),
        (f"Basic {'t' * 40}", TOKEN, False),
        (f"Bearer{'t' * 40}", TOKEN, False),
        (f"Bearer Bearer {'t' * 40}", TOKEN, False),
        (f"Bearer {'t' * 40}\n", TOKEN, False),
        (f"Bearer {'t' * 40} extra", TOKEN, False),
        (f"Bearer {'t' * 39}é", TOKEN, False),
        ("", TOKEN, False),
        (None, TOKEN, False),
        ("Bearer ", TOKEN, False),
        ("Bearer", TOKEN, False),
        (f"Bearer {'t' * 40}", None, False),
        (None, None, False),
    ],
)
def test_verify_ha_token(header, expected, ok):
    assert verify_ha_token(header, expected) is ok


@pytest.mark.unit
class TestEmptyConfiguredToken:
    """A function-level guard: settings validation is not the only line."""

    @pytest.mark.parametrize("header", ["Bearer ", "Bearer", "", None])
    def test_empty_secret_denies_everything(self, header):
        assert verify_ha_token(header, SecretStr("")) is False


@pytest.mark.unit
def test_comparison_is_constant_time():
    with patch.object(ha.hmac, "compare_digest", wraps=ha.hmac.compare_digest) as spy:
        assert verify_ha_token(f"Bearer {'t' * 40}", TOKEN) is True
    spy.assert_called_once_with(b"t" * 40, b"t" * 40)
