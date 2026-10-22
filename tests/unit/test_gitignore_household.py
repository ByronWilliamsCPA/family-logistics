"""The household-data .gitignore globs must match at any depth.

This repository is public. A registry, backup, report or credential file in
any subdirectory must stay ignored, while the ``registry*.example.*``
templates stay committable. ``git check-ignore --no-index`` evaluates the
real ``.gitignore`` exactly as Git would.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(
    GIT is None or not (REPO_ROOT / ".git").exists(),
    reason="needs git and a checkout",
)

MUST_IGNORE = [
    "registry.yaml",
    "config/registry.yaml",
    "deploy/registry.json",
    "a/b/registry-site.yml",
    "backups/db.bak",
    "x/backups/a",
    "deploy/reports/r.md",
    "deploy/assessments/a.json",
    "deploy/llm-transcripts/t.txt",
    "home/.aws/credentials",
    "home/.ssh/id_ed25519",
    "deploy/config/local/site.yaml",
    "tests/fixtures/real/cal.ics",
    "deploy/state.db",
    "deploy/export.ics",
]

MUST_NOT_IGNORE = [
    "registry.example.yaml",
    "config/registry.example.yaml",
    "deploy/registry.example.json",
    "tests/fixtures/synthetic/week.ics",
]


def _ignored(path: str) -> bool:
    """Return True when Git would ignore ``path``."""
    assert GIT is not None
    result = subprocess.run(  # noqa: S603 - fixed git binary, literal paths
        [GIT, "check-ignore", "--no-index", "-q", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode == 0


@pytest.mark.parametrize("path", MUST_IGNORE)
def test_household_paths_ignored_at_any_depth(path: str) -> None:
    """Household-data and credential paths are ignored wherever they appear."""
    assert _ignored(path) is True


@pytest.mark.parametrize("path", MUST_NOT_IGNORE)
def test_example_templates_not_ignored(path: str) -> None:
    """Example templates and synthetic fixtures remain committable."""
    assert _ignored(path) is False
