"""Regression tests for the household-data guards in .gitignore.

This repository is public. The globs that keep real registry seeds, backups,
reports and credentials out of Git must match at any depth, while the
``*.example.*`` templates must stay trackable at any depth. The tests copy
.gitignore into a scratch repository and ask real git, so a pattern that is
anchored by mistake fails here instead of silently leaking data.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.unit._proc import run_process

GITIGNORE = Path(__file__).resolve().parents[2] / ".gitignore"

IGNORED = [
    "registry.yaml",
    "registry.yml",
    "registry.json",
    "registry-prod.yaml",
    "config/registry.yaml",
    "config/registry.yml",
    "deploy/registry.json",
    "deploy/site/registry.yaml",
    "backups/fl.db.bak",
    "sub/backups/x",
    "ops/nightly/backups/x.json",
    "reports/week.md",
    "docs/reports/week.md",
    "assessments/a.md",
    "sub/assessments/a.md",
    "llm-transcripts/t.json",
    "sub/llm-transcripts/t.json",
    ".aws/credentials",
    "home/.aws/credentials",
    ".ssh/id_ed25519",
    "home/.ssh/id_ed25519",
    ".gcloud/creds",
    "nested/.gcloud/creds",
    "config/local/site.yaml",
    "fl.db",
    "var/data/fl.sqlite3",
    "site.env",
    "deploy/stack.env",
    "calendars/real.ics",
    "pseudonyms.yaml",
    "private-terms.txt",
    "notes/denylist-extra.txt",
]

NOT_IGNORED = [
    "registry.example.yaml",
    "registry.example.yml",
    "registry.example.json",
    "config/registry.example.yaml",
    "config/registry.example.json",
    "deploy/site/registry.example.yml",
    "tests/fixtures/synthetic/week.ics",
    "src/family_logistics/core/config.py",
    "docs/index.md",
    "scripts/check_private_terms.py",
]


@pytest.fixture(scope="module")
def check_ignore(tmp_path_factory: pytest.TempPathFactory):
    """Return a function that asks real git whether a path is ignored.

    The scratch repository gets a copy of .gitignore and an isolated git
    configuration, so neither the developer's global excludes nor
    ``.git/info/exclude`` can change the answer.
    """
    git = shutil.which("git")
    assert git is not None, "git is required to verify the .gitignore guards"
    repo = tmp_path_factory.mktemp("gitignore-repo")
    env = {
        "HOME": str(repo),
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    init = run_process([git, "init", "-q"], cwd=repo, env=env)
    assert init.returncode == 0, init.stderr
    shutil.copyfile(GITIGNORE, repo / ".gitignore")

    def _is_ignored(path: str) -> bool:
        result = run_process(
            [git, "check-ignore", "--no-index", "-q", "--", path],
            cwd=repo,
            env=env,
        )
        # 0 = ignored, 1 = not ignored; anything else is a git failure.
        assert result.returncode in (0, 1), result.stderr
        return result.returncode == 0

    return _is_ignored


@pytest.mark.unit
@pytest.mark.parametrize("path", IGNORED)
def test_household_data_paths_are_ignored(check_ignore, path: str) -> None:
    """Household data and credentials are ignored at every depth."""
    assert check_ignore(path), f"{path} must be ignored"


@pytest.mark.unit
@pytest.mark.parametrize("path", NOT_IGNORED)
def test_templates_and_source_stay_trackable(check_ignore, path: str) -> None:
    """Example templates, synthetic fixtures and source files are not ignored."""
    assert not check_ignore(path), f"{path} must stay trackable"
