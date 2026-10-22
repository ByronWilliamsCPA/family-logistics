"""Regression guards for the repository's CI and agent-permission security config.

These assert behavior of the configuration, not just that files exist: which
commands the Claude Code allow list would auto-approve, and under which refs the
container workflow is allowed to publish and attest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
CONTAINER_IMAGE = REPO_ROOT / ".github" / "workflows" / "container-image.yml"

FILE_DISCLOSING_DIFF = "git diff --no-index .env /dev/null"
DESTRUCTIVE_MIGRATION = "uv run family-logistics migrate --downgrade --revision base"
READ_ONLY_CLI = "uv run family-logistics verify-db backup.sqlite"


def _rules(kind: str) -> list[str]:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return list(settings["permissions"][kind])


def _matches(rule: str, command: str) -> bool:
    """Match a legacy ``Bash(<prefix>:*)`` permission rule against a command."""
    found = re.fullmatch(r"Bash\((.+?)(:\*)?\)", rule)
    if found is None:
        return False
    prefix, is_prefix_rule = found.group(1), found.group(2) is not None
    if is_prefix_rule:
        return command == prefix or command.startswith(prefix + " ")
    return command == prefix


def _auto_approved(command: str) -> bool:
    """True when the command is allowed and neither denied nor sent to ask."""
    if any(_matches(rule, command) for rule in _rules("deny")):
        return False
    if any(_matches(rule, command) for rule in _rules("ask")):
        return False
    return any(_matches(rule, command) for rule in _rules("allow"))


def test_git_diff_no_index_cannot_read_denied_files() -> None:
    # `git diff --no-index` prints any readable file, bypassing Read(./.env).
    assert not _auto_approved(FILE_DISCLOSING_DIFF)
    assert any(_matches(rule, FILE_DISCLOSING_DIFF) for rule in _rules("deny"))


def test_destructive_migration_is_never_auto_approved() -> None:
    # `migrate --downgrade` drops the append-only audit_log table.
    assert not _auto_approved(DESTRUCTIVE_MIGRATION)


def test_read_only_cli_forms_stay_auto_approved() -> None:
    assert _auto_approved(READ_ONLY_CLI)
    assert _auto_approved("uv run family-logistics --help")


def _container_workflow() -> dict[str, Any]:
    return YAML(typ="safe").load(CONTAINER_IMAGE.read_text(encoding="utf-8"))


def _assert_limited_to_trusted_refs(condition: str) -> None:
    """Require main or a v* tag, never just 'not a pull request'."""
    assert "github.ref == 'refs/heads/main'" in condition
    assert "startsWith(github.ref, 'refs/tags/v')" in condition
    # A trusted-ref clause must be ANDed with the event check, not ORed away.
    assert condition.count("||") == 1
    assert condition.startswith(
        (
            "${{ github.event_name != 'pull_request' &&",
            "github.event_name != 'pull_request' &&",
        )
    )


def test_image_push_is_limited_to_trusted_refs() -> None:
    jobs = _container_workflow()["jobs"]

    _assert_limited_to_trusted_refs(str(jobs["image"]["with"]["push"]))


def test_attestation_is_limited_to_trusted_refs() -> None:
    jobs = _container_workflow()["jobs"]

    _assert_limited_to_trusted_refs(str(jobs["attest"]["if"]))


def test_trivy_scan_gates_on_fixable_findings() -> None:
    options = _container_workflow()["jobs"]["image"]["with"]

    assert options["enable-trivy-scan"] is True
    assert options["trivy-fail-on-vuln"] is True
    assert options["trivy-ignore-unfixed"] is True
