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


def test_path_label_never_prints_term(tmp_path: Path, base_env: dict[str, str]) -> None:
    """A file whose path holds a term is reported by argument number, not name."""
    clean = tmp_path / "clean.md"
    clean.write_text("nothing\n", encoding="utf-8")
    target = tmp_path / f"{TERM.lower()}_notes.md"
    target.write_text(f"see {TERM}\n", encoding="utf-8")
    env = {**base_env, "FL_PRIVATE_TERMS": TERM}
    result = run([clean.name, target.name], cwd=tmp_path, env=env)
    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "<file argument #2> (path): private term #1" in result.stdout
    assert "<file argument #2>:1: private term #1" in result.stdout
    assert TERM.lower() not in output


@pytest.mark.parametrize(
    ("terms", "text"),
    [
        (TERM, f"alex_{TERM.lower()}_route"),
        (TERM, f"{TERM}2 pickup"),
        ("Zebulonicà", f"{TERM} pickup"),
        (TERM, "ZEBULONICÁ pickup"),
        ("Quillon Vasterby", "quillon_vasterby"),
        ("Quillon Vasterby", "Quillon-Vasterby"),
        ("Quillon Vasterby", "Quillon   Vasterby"),
        (f"\ufeff  {TERM}\u200b  ", f"{TERM} pickup"),
        ("Unit #5 Quillon", "unit #5 quillon"),
    ],
)
def test_matcher_and_parser_have_no_false_negatives(
    tmp_path: Path, base_env: dict[str, str], terms: str, text: str
) -> None:
    """snake_case, digits, accents, separators, BOM and invisible characters."""
    target = tmp_path / "notes.md"
    target.write_text(f"{text}\n", encoding="utf-8")
    env = {**base_env, "FL_PRIVATE_TERMS": terms}
    result = run([target.name], cwd=tmp_path, env=env)
    assert result.returncode == 1


def test_list_file_with_bom(tmp_path: Path, base_env: dict[str, str]) -> None:
    """A list file saved with a UTF-8 BOM keeps its first term active."""
    terms = tmp_path / "terms.txt"
    terms.write_bytes(f"\ufeff{TERM}\n".encode())
    target = tmp_path / "notes.md"
    target.write_text(f"{TERM}\n", encoding="utf-8")
    env = {**base_env, "FL_PRIVATE_TERMS_FILE": str(terms)}
    assert run([target.name], cwd=tmp_path, env=env).returncode == 1


def test_unreadable_list_file(tmp_path: Path, base_env: dict[str, str]) -> None:
    """A list that is not UTF-8 is a configuration error, not a clean run."""
    terms = tmp_path / "terms.txt"
    terms.write_bytes(b"\xff\xfe\x00bad")
    env = {**base_env, "FL_PRIVATE_TERMS_FILE": str(terms)}
    assert run([], cwd=tmp_path, env=env).returncode == 2


def test_undecodable_file_fails_closed(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A file that is not UTF-8 is counted as unscannable and fails the run."""
    target = tmp_path / "legacy.txt"
    target.write_bytes(f"{TERM} caf\xe9".encode("latin-1"))
    env = {**base_env, "FL_PRIVATE_TERMS": TERM}
    result = run([target.name], cwd=tmp_path, env=env)
    assert result.returncode == 4
    assert "legacy.txt: unscannable" in result.stdout
    assert TERM.lower() not in result.stdout.lower()


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


def _repo_with_base(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.md").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "a.md")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_git_range_binary_change_fails_closed(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A binary blob in the range cannot be scanned, so the run fails."""
    repo = _repo_with_base(tmp_path)
    (repo / "blob.bin").write_bytes(b"\x00\x01" + TERM.encode() + b"\x00")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-q", "-m", "binary")
    env = {**base_env, "FL_PRIVATE_TERMS": TERM}
    result = run(["--git-range", "HEAD~1..HEAD"], cwd=repo, env=env)
    assert result.returncode == 4
    assert "binary change(s) unscannable" in result.stdout


def test_git_range_non_utf8_diff_fails_closed(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """Non-UTF-8 text in history is reported as unscannable."""
    repo = _repo_with_base(tmp_path)
    (repo / "a.md").write_bytes("caf\xe9 note\n".encode("latin-1"))
    _git(repo, "commit", "-q", "-am", "latin-1")
    env = {**base_env, "FL_PRIVATE_TERMS": TERM}
    result = run(["--git-range", "HEAD~1..HEAD"], cwd=repo, env=env)
    assert result.returncode == 4
    assert "not valid UTF-8" in result.stdout


def test_git_range_bad_revision(tmp_path: Path, base_env: dict[str, str]) -> None:
    """An unreadable range is reported as a git failure, not as clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    env = {**base_env, "FL_PRIVATE_TERMS": TERM}
    assert run(["--git-range", "nope..nada"], cwd=repo, env=env).returncode == 3
