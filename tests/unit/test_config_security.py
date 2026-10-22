"""Settings never leak secrets and every field is consumed or scheduled."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from family_logistics.cli import cli
from family_logistics.core import config as config_module
from family_logistics.core.config import FIELD_STATUS, Settings, load_settings
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
            Settings(ha_token=LEAKY_TOKEN)  # type: ignore[arg-type]
        assert LEAKY_TOKEN not in str(caught.value)

    def test_validation_error_hides_data_key(self):
        with pytest.raises(ValidationError) as caught:
            Settings(data_key=LEAKY_KEY)  # type: ignore[arg-type]
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


@pytest.mark.unit
class TestCaldavUrl:
    def test_https_default(self):
        assert Settings().caldav_url.startswith("https://")

    @pytest.mark.parametrize("url", ["http://caldav.example.invalid", "ftp://x"])
    def test_non_https_rejected(self, url):
        with pytest.raises(ValidationError):
            Settings(caldav_url=url)


@pytest.mark.unit
def test_every_field_is_consumed_or_scheduled():
    """A new setting must be read by code or carry a milestone (no dead config)."""
    assert set(FIELD_STATUS) == set(Settings.model_fields)
    assert set(FIELD_STATUS.values()) <= {"consumed", "M1", "M2", "M3", "M4", "M5"}


@pytest.mark.unit
def test_consumed_fields_are_read_in_src():
    """A field marked consumed must be read (``.<field>``) outside config.py.

    A read through a ``Settings`` property counts when that property is
    itself read outside config.py (``calendar_list`` reads
    ``caldav_calendars``).
    """
    src = Path(__file__).resolve().parents[2] / "src" / "family_logistics"
    code = "\n".join(
        path.read_text(encoding="utf-8")
        for path in src.rglob("*.py")
        if path.name != "config.py"
    )
    via_property = {
        field
        for prop_name, prop in vars(Settings).items()
        if isinstance(prop, property)
        and prop.fget is not None
        and re.search(rf"\.{prop_name}\b", code)
        for field in re.findall(r"self\.(\w+)", inspect.getsource(prop.fget))
    }
    unread = [
        name
        for name, status in FIELD_STATUS.items()
        if status == "consumed"
        and name not in via_property
        and not re.search(rf"\.{name}\b", code)
    ]
    assert unread == []
