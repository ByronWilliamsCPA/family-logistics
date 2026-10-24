"""Settings never leak secrets (field coverage lives in test_config_fields.py)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from family_logistics.cli import cli
from family_logistics.core import config as config_module
from family_logistics.core.config import Settings, load_settings
from family_logistics.core.exceptions import ConfigurationError

LEAKY_TOKEN = "shortSECRETvalue-do-not-print"
LEAKY_KEY = "not-a-fernet-SECRET-key"


@pytest.fixture
def fresh_settings():
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


@pytest.mark.unit
class TestNoSecretEcho:
    def test_validation_error_hides_token(self):
        with pytest.raises(ValidationError) as caught:
            Settings.model_validate({"ha_token": LEAKY_TOKEN})
        assert LEAKY_TOKEN not in str(caught.value)

    def test_validation_error_hides_data_key(self):
        with pytest.raises(ValidationError) as caught:
            Settings.model_validate({"data_key": LEAKY_KEY})
        assert LEAKY_KEY not in str(caught.value)

    def test_load_settings_names_field_only(self, monkeypatch):
        monkeypatch.setenv("FL_HA_TOKEN", LEAKY_TOKEN)
        with pytest.raises(ConfigurationError) as caught:
            load_settings()
        assert "FL_HA_TOKEN" in str(caught.value)
        assert LEAKY_TOKEN not in str(caught.value)
        assert LEAKY_TOKEN not in repr(caught.value.details)

    @pytest.mark.parametrize(
        ("variable", "value"),
        [("FL_HA_TOKEN", LEAKY_TOKEN), ("FL_DATA_KEY", LEAKY_KEY)],
    )
    def test_cli_output_never_contains_bad_secret(
        self, monkeypatch, fresh_settings, variable, value
    ):
        monkeypatch.setenv(variable, value)
        for command in (["config"], ["serve"], ["healthcheck"], ["migrate"]):
            result = CliRunner().invoke(cli, command)
            assert result.exit_code == 2, command
            assert value not in result.output
            assert variable in result.output

    def test_load_settings_drops_the_pydantic_error_chain(self, monkeypatch):
        monkeypatch.setenv("FL_HA_TOKEN", LEAKY_TOKEN)
        with pytest.raises(ConfigurationError) as caught:
            load_settings()
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_load_settings_does_not_echo_a_bad_cidr(self, monkeypatch):
        monkeypatch.setenv("FL_PROXY_TRUSTED_CIDRS", "SECRETVALUE")
        with pytest.raises(ConfigurationError) as caught:
            load_settings()
        assert "FL_PROXY_TRUSTED_CIDRS" in str(caught.value)
        assert "SECRETVALUE" not in str(caught.value)
        assert "SECRETVALUE" not in repr(caught.value.details)

    def test_non_ascii_data_key_is_rejected_without_echo(self):
        with pytest.raises(ValidationError) as caught:
            Settings.model_validate({"data_key": "k\u00e9y" * 12})
        assert "k\u00e9y" not in str(caught.value)

    @pytest.mark.parametrize("length", [31, 32])
    def test_ha_token_length_boundary(self, length):
        token = "t" * length
        if length < 32:
            with pytest.raises(ValidationError):
                Settings.model_validate({"ha_token": token})
        else:
            assert Settings.model_validate({"ha_token": token}).ha_token is not None
