"""``registry load`` and ``registry show`` commands."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet

from family_logistics import cli as cli_module
from family_logistics.cli import SCHEMA_BEHIND_MESSAGE, cli
from family_logistics.core import config as config_module

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "registry.example.yaml"
EXAMPLE_ADDRESSES = ("100 Example Lane", "200 Sample Road")


@pytest.fixture
def env(tmp_path, monkeypatch, audit_key_text):
    config_module.get_settings.cache_clear()
    monkeypatch.setenv("FL_DATABASE_PATH", str(tmp_path / "fl.db"))
    monkeypatch.setenv("FL_DATA_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FL_AUDIT_KEY", audit_key_text)
    monkeypatch.delenv("FL_REGISTRY_SEED", raising=False)
    yield tmp_path
    config_module.get_settings.cache_clear()


def run(*args: str):
    return CliRunner().invoke(cli, list(args))


def migrated() -> None:
    result = run("migrate")
    assert result.exit_code == 0, result.output


@pytest.mark.unit
class TestRegistryLoad:
    def test_dry_run_then_load_then_unchanged(self, env):
        migrated()
        dry = run("registry", "load", str(EXAMPLE), "--allow-repo-path", "--dry-run")
        assert dry.exit_code == 0, dry.output
        assert dry.output.startswith("Dry run: create 18")
        applied = run("registry", "load", str(EXAMPLE), "--allow-repo-path")
        assert applied.exit_code == 0, applied.output
        assert applied.output.startswith("Applied: create 18")
        assert "create     person:alex" in applied.output
        again = run("registry", "load", str(EXAMPLE), "--allow-repo-path")
        assert again.output.startswith(
            "Applied: create 0, update 0, deactivate 0, unchanged 18"
        )

    def test_load_uses_the_configured_local_day(self, env, monkeypatch):
        migrated()
        zone = "Pacific/Kiritimati"
        monkeypatch.setenv("FL_TIMEZONE", zone)
        config_module.get_settings.cache_clear()
        seen: dict[str, object] = {}
        real_apply = cli_module.apply_seed

        def spy(*args, **kwargs):
            seen["today"] = kwargs.get("today")
            return real_apply(*args, **kwargs)

        monkeypatch.setattr(cli_module, "apply_seed", spy)
        result = run("registry", "load", str(EXAMPLE), "--allow-repo-path")
        assert result.exit_code == 0, result.output
        assert seen["today"] == datetime.now(ZoneInfo(zone)).date()

    def test_seed_path_from_settings(self, env, monkeypatch):
        migrated()
        seed = env / "registry.yaml"
        seed.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setenv("FL_REGISTRY_SEED", str(seed))
        config_module.get_settings.cache_clear()
        result = run("registry", "load", "--dry-run")
        assert result.exit_code == 0, result.output

    def test_repo_path_refused(self, env):
        migrated()
        result = run("registry", "load", str(EXAMPLE))
        assert result.exit_code == 2
        assert "inside a git working tree" in result.output

    def test_unreadable_seed_is_a_clean_error(self, env):
        migrated()
        seed = env / "registry.yaml"
        # A Windows editor's "Unicode" save is UTF-16, not UTF-8.
        seed.write_bytes(EXAMPLE.read_text(encoding="utf-8").encode("utf-16"))
        result = run("registry", "load", str(seed))
        assert result.exit_code == 2
        assert "could not be read" in result.output
        assert "Traceback" not in result.output

    def test_no_path(self, env):
        migrated()
        result = run("registry", "load")
        assert result.exit_code == 2
        assert "FL_REGISTRY_SEED" in result.output

    def test_schema_behind(self, env):
        result = run("registry", "load", str(EXAMPLE), "--allow-repo-path")
        assert result.exit_code == 2
        assert SCHEMA_BEHIND_MESSAGE in result.output

    def test_missing_database_directory(self, env, monkeypatch):
        monkeypatch.setenv("FL_DATABASE_PATH", str(env / "nope" / "fl.db"))
        config_module.get_settings.cache_clear()
        result = run("registry", "show")
        assert result.exit_code == 2
        assert "does not exist" in result.output

    def test_addresses_need_a_key(self, env, monkeypatch):
        migrated()
        monkeypatch.delenv("FL_DATA_KEY")
        config_module.get_settings.cache_clear()
        result = run("registry", "load", str(EXAMPLE), "--allow-repo-path")
        assert result.exit_code == 2
        assert "FL_DATA_KEY is required" in result.output

    def test_load_needs_an_audit_key(self, env, monkeypatch):
        migrated()
        monkeypatch.delenv("FL_AUDIT_KEY")
        config_module.get_settings.cache_clear()
        result = run("registry", "load", str(EXAMPLE), "--allow-repo-path")
        assert result.exit_code == 2
        assert "FL_AUDIT_KEY is required" in result.output

    def test_overlong_actor_is_a_clean_error(self, env):
        migrated()
        result = run(
            "registry", "load", str(EXAMPLE), "--allow-repo-path", "--actor", "a" * 65
        )
        assert result.exit_code == 2
        assert "audit actor must be 1-64 characters" in result.output


@pytest.mark.unit
class TestRegistryShow:
    def test_show_lists_without_addresses(self, env):
        migrated()
        assert run("registry", "load", str(EXAMPLE), "--allow-repo-path").exit_code == 0
        result = run("registry", "show")
        assert result.exit_code == 0, result.output
        assert "People (7):" in result.output
        assert "address=stored" in result.output
        assert "seats=unverified" in result.output
        assert "last_success=never" in result.output
        assert "alex             1" in result.output
        for address in EXAMPLE_ADDRESSES:
            assert address not in result.output

    def test_show_empty_registry(self, env):
        migrated()
        result = run("registry", "show")
        assert result.exit_code == 0, result.output
        assert "People (0):" in result.output


@pytest.mark.unit
def test_blank_seed_setting_is_unset(monkeypatch):
    monkeypatch.setenv("FL_REGISTRY_SEED", "  ")
    assert config_module.Settings().registry_seed is None
