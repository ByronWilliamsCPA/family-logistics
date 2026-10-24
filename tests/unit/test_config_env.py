"""``.env`` loading, precedence, blank values and the shipped template."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from family_logistics.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
ENV_NAME = re.compile(r"^#?\s*(FL_[A-Z0-9_]+)=", re.MULTILINE)


def _lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.unit
class TestDotenv:
    def test_dotenv_file_is_read(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("FL_TIMEZONE=UTC\nFL_PORT=9090\n", encoding="utf-8")
        settings = Settings(_env_file=env_file)
        assert (settings.timezone, settings.port) == ("UTC", 9090)

    def test_dotenv_is_the_default_source(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("FL_TIMEZONE=UTC\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        # Restore the real default that tests/unit/conftest.py switched off.
        monkeypatch.setitem(Settings.model_config, "env_file", ".env")
        assert Settings().timezone == "UTC"

    def test_process_environment_wins_over_dotenv(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("FL_TIMEZONE=UTC\n", encoding="utf-8")
        monkeypatch.setenv("FL_TIMEZONE", "Europe/Paris")
        assert Settings(_env_file=env_file).timezone == "Europe/Paris"

    def test_invalid_dotenv_value_fails_validation_without_echo(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("FL_HA_TOKEN=tooShortSECRET\n", encoding="utf-8")
        with pytest.raises(ValueError, match="FL_HA_TOKEN") as caught:
            Settings(_env_file=env_file)
        assert "tooShortSECRET" not in str(caught.value)


@pytest.mark.unit
class TestBlankValuesMeanUnset:
    @pytest.mark.parametrize(
        "name", ["FL_HA_TOKEN", "FL_DATA_KEY", "FL_APPRISE_URL", "FL_CALDAV_USERNAME"]
    )
    def test_blank_optional_value_is_none(self, monkeypatch, name):
        monkeypatch.setenv(name, "")
        settings = Settings()
        assert getattr(settings, name.removeprefix("FL_").lower()) is None

    def test_blank_does_not_hide_a_real_default(self, monkeypatch):
        monkeypatch.setenv("FL_PORT", "")
        assert Settings().port == 8080


@pytest.mark.unit
class TestEnvExample:
    def test_every_setting_is_documented_in_the_template(self):
        documented = set(ENV_NAME.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))
        expected = {f"FL_{name.upper()}" for name in Settings.model_fields}
        assert documented == expected

    def test_template_loads_verbatim_from_a_file(self):
        settings = Settings(_env_file=ENV_EXAMPLE)
        assert settings == Settings()

    def test_template_loads_verbatim_when_exported(self, monkeypatch):
        for name, value in dotenv_values(ENV_EXAMPLE).items():
            monkeypatch.setenv(name, value or "")
        assert Settings() == Settings(_env_file=None)

    def test_template_does_not_widen_the_native_bind_address(self):
        assert Settings(_env_file=ENV_EXAMPLE).bind_host == "127.0.0.1"


@pytest.mark.unit
class TestSecretsNeverShipped:
    @pytest.mark.parametrize("ignore_file", [".gitignore", ".dockerignore"])
    def test_dotenv_is_ignored(self, ignore_file):
        assert ".env" in _lines(ROOT / ignore_file)

    def test_template_holds_no_secret_values(self):
        values = {
            name: value
            for name, value in dotenv_values(ENV_EXAMPLE).items()
            if any(word in name for word in ("TOKEN", "KEY", "PASSWORD"))
            and name != "FL_APPRISE_CONFIG_KEY"
        }
        assert values
        assert all(not value for value in values.values())
