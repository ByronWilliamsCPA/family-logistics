"""Tests for scripts/check_private_terms.py (the public-repo household-data guard)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_private_terms.py"
TERM = "Zebulonica"


def run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run the checker with a controlled environment."""
    return subprocess.run(  # noqa: S603 - fixed interpreter and script path
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def base_env(tmp_path: Path) -> dict[str, str]:
    """Environment with HOME isolated so no real list is picked up."""
    return {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}


def test_skips_without_list(tmp_path: Path, base_env: dict[str, str]) -> None:
    """No list configured: warn and pass so contributors are not blocked."""
    result = run([], cwd=tmp_path, env=base_env)
    assert result.returncode == 0
    assert "SKIPPED" in result.stdout


@pytest.mark.parametrize("how", ["flag", "env"])
def test_fails_closed_when_required(
    tmp_path: Path, base_env: dict[str, str], how: str
) -> None:
    """--require-list or FL_PRIVATE_TERMS_REQUIRED=1 turns the skip into a failure."""
    env = dict(base_env)
    args: list[str] = []
    if how == "flag":
        args.append("--require-list")
    else:
        env["FL_PRIVATE_TERMS_REQUIRED"] = "1"
    result = run(args, cwd=tmp_path, env=env)
    assert result.returncode == 2


def test_empty_secret_counts_as_missing(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """An empty FL_PRIVATE_TERMS (unset CI secret) must not pass a required run."""
    env = {**base_env, "FL_PRIVATE_TERMS": ""}
    assert run(["--require-list"], cwd=tmp_path, env=env).returncode == 2


def test_finds_term_without_printing_it(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A match fails the run and the output never contains the term."""
    target = tmp_path / "notes.md"
    target.write_text(f"pickup for {TERM.lower()} at four\n", encoding="utf-8")
    env = {**base_env, "FL_PRIVATE_TERMS": f"# comment\n\n{TERM}\n"}
    result = run([str(target)], cwd=tmp_path, env=env)
    assert result.returncode == 1
    assert "private term #1" in result.stdout
    assert TERM.lower() not in (result.stdout + result.stderr).lower()


def test_word_boundary(tmp_path: Path, base_env: dict[str, str]) -> None:
    """A term embedded in a longer word does not match."""
    target = tmp_path / "notes.md"
    target.write_text(f"x{TERM}y\n", encoding="utf-8")
    env = {**base_env, "FL_PRIVATE_TERMS": TERM}
    assert run([str(target)], cwd=tmp_path, env=env).returncode == 0


def test_scans_file_paths(tmp_path: Path, base_env: dict[str, str]) -> None:
    """A private term in a file name is reported."""
    target = tmp_path / f"{TERM}-schedule.md"
    target.write_text("nothing here\n", encoding="utf-8")
    env = {**base_env, "FL_PRIVATE_TERMS": TERM}
    result = run([target.name], cwd=tmp_path, env=env)
    assert result.returncode == 1
    assert "(path)" in result.stdout


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed git argv
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_git_range_catches_removed_term(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A term added then removed is still found in history and commit messages."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.md").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "a.md")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "a.md").write_text(f"{TERM}\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "add")
    (repo / "a.md").write_text("clean again\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", f"remove {TERM}")
    env = {**base_env, "FL_PRIVATE_TERMS": TERM}
    clean_tree = run(["a.md"], cwd=repo, env=env)
    assert clean_tree.returncode == 0
    history = run(["--git-range", "HEAD~2..HEAD"], cwd=repo, env=env)
    assert history.returncode == 1
    assert history.stdout.count("commit ") >= 2
    assert TERM.lower() not in history.stdout.lower()


def test_git_range_bad_revision(tmp_path: Path, base_env: dict[str, str]) -> None:
    """An unreadable range is reported as a git failure, not as clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    env = {**base_env, "FL_PRIVATE_TERMS": TERM}
    assert run(["--git-range", "nope..nada"], cwd=repo, env=env).returncode == 3
