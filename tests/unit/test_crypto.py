"""Protected address encryption (privacy spec section 5)."""

from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr, ValidationError

from family_logistics.core.config import Settings
from family_logistics.core.crypto import (
    AddressCipher,
    ProtectedDataError,
    generate_key,
    require_cipher,
)
from family_logistics.core.exceptions import ConfigurationError
from family_logistics.domain.models import SensitiveStr

ADDRESS = "1 Example Way, Sample Town"


@pytest.mark.unit
def test_round_trip_and_token_hides_plaintext():
    cipher = AddressCipher(SecretStr(generate_key()))
    token = cipher.encrypt(SensitiveStr(ADDRESS))
    assert "Example" not in token
    assert cipher.decrypt(token).reveal() == ADDRESS


@pytest.mark.unit
def test_wrong_key_fails_closed():
    token = AddressCipher(SecretStr(generate_key())).encrypt(SensitiveStr(ADDRESS))
    with pytest.raises(ProtectedDataError):
        AddressCipher(SecretStr(generate_key())).decrypt(token)


@pytest.mark.unit
def test_storing_without_key_is_refused():
    with pytest.raises(ConfigurationError):
        require_cipher(None)
    assert isinstance(require_cipher(SecretStr(generate_key())), AddressCipher)


@pytest.mark.unit
def test_settings_validate_data_key():
    with pytest.raises(ValidationError):
        Settings.model_validate({"data_key": "not-a-fernet-key"})
    key = generate_key()
    settings = Settings.model_validate({"data_key": key})
    assert key not in repr(settings)


@pytest.mark.unit
def test_decrypted_value_never_reaches_log_text(caplog):
    cipher = AddressCipher(SecretStr(generate_key()))
    value = cipher.decrypt(cipher.encrypt(SensitiveStr(ADDRESS)))
    with caplog.at_level(logging.INFO):
        logging.getLogger("fl.test").info("routing to %s (%r)", value, value)
    assert "Example" not in caplog.text
