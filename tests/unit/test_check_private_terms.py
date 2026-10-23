"""Tests for scripts/check_private_terms.py (the public-repo household-data guard).

The checker runs as a real process, the way CI and pre-commit run it. Every
test that produces output also asserts that the output never contains a
private term, because the CI logs of this repository are public.
"""

from __future__ import annotations

import shutil
import unicodedata
from pathlib import Path

import pytest

from tests.unit._proc import run_process

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_private_terms.py"
TERM = "Zebulonica"
PYTHON = shutil.which("python3") or "/usr/bin/python3"
GIT = shutil.which("git")


class Result:
    """Decoded outcome of one checker run."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def output(self) -> str:
        """Combined stdout and stderr."""
        return self.stdout + self.stderr


def run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin: bytes | None = None,
) -> Result:
    """Run the checker with a controlled environment."""
    done = run_process(
        [PYTHON, "-I", str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        input_bytes=stdin,
    )
    return Result(
        done.returncode,
        done.stdout.decode("utf-8", errors="replace"),
        done.stderr.decode("utf-8", errors="replace"),
    )


def assert_no_leak(result: Result, *terms: str) -> None:
    """Fail when the output reveals any term, in any case, in any encoding form."""
    shown = unicodedata.normalize("NFKC", result.output).lower()
    for term in terms or (TERM,):
        assert term.lower() not in shown


@pytest.fixture
def base_env(tmp_path: Path) -> dict[str, str]:
    """Environment with HOME isolated so no real list is picked up."""
    git_dir = str(Path(GIT).parent) if GIT else "/usr/bin"
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{git_dir}:/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def with_terms(env: dict[str, str], terms: str) -> dict[str, str]:
    """Return env with the term list set the way the CI secret sets it."""
    return {**env, "FL_PRIVATE_TERMS": terms}


# ---------------------------------------------------------------------------
# List configuration
# ---------------------------------------------------------------------------


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
    assert (
        run(["--require-list"], cwd=tmp_path, env=with_terms(base_env, "")).returncode
        == 2
    )


@pytest.mark.parametrize("raw", ["# only a comment\n\n   \n", "\ufeff# commented\r\n"])
def test_comment_only_list_counts_as_missing(
    tmp_path: Path, base_env: dict[str, str], raw: str
) -> None:
    """A list with no usable term must not pass a required run."""
    result = run(["--require-list"], cwd=tmp_path, env=with_terms(base_env, raw))
    assert result.returncode == 2


def test_list_file_is_read(tmp_path: Path, base_env: dict[str, str]) -> None:
    """FL_PRIVATE_TERMS_FILE names a list file whose terms are enforced."""
    terms = tmp_path / "terms.txt"
    terms.write_text(f"# comment\n{TERM}\n", encoding="utf-8")
    target = tmp_path / "notes.md"
    target.write_text(f"{TERM}\n", encoding="utf-8")
    env = {**base_env, "FL_PRIVATE_TERMS_FILE": str(terms)}
    result = run([str(target)], cwd=tmp_path, env=env)
    assert result.returncode == 1
    assert_no_leak(result)


def test_default_list_location_is_read(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """With no env settings the list under ~/.config/family-logistics is used."""
    default = tmp_path / "home" / ".config" / "family-logistics" / "private-terms.txt"
    default.parent.mkdir(parents=True)
    default.write_text(f"{TERM}\n", encoding="utf-8")
    target = tmp_path / "notes.md"
    target.write_text(f"{TERM}\n", encoding="utf-8")
    assert run([str(target)], cwd=tmp_path, env=base_env).returncode == 1


def test_configured_but_missing_list_file_fails(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A FL_PRIVATE_TERMS_FILE that names nothing is a misconfiguration, not a skip."""
    env = {**base_env, "FL_PRIVATE_TERMS_FILE": str(tmp_path / "missing.txt")}
    result = run([], cwd=tmp_path, env=env)
    assert result.returncode == 2
    assert "FL_PRIVATE_TERMS_FILE" in result.stdout


def test_list_file_that_is_not_utf8_fails(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A mis-encoded list is rejected instead of silently dropping terms."""
    terms = tmp_path / "terms.txt"
    terms.write_bytes(b"Zebulonica\n\xff\xfe\xfa\n")
    env = {**base_env, "FL_PRIVATE_TERMS_FILE": str(terms)}
    assert run([], cwd=tmp_path, env=env).returncode == 2


def test_bom_does_not_disable_the_first_term(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A UTF-8 BOM and CRLF line endings in the list do not hide term #1."""
    terms = tmp_path / "terms.txt"
    terms.write_bytes(b"\xef\xbb\xbf" + f"{TERM}\r\nOther\r\n".encode())
    target = tmp_path / "notes.md"
    target.write_text(f"{TERM}\n", encoding="utf-8")
    env = {**base_env, "FL_PRIVATE_TERMS_FILE": str(terms)}
    result = run([str(target)], cwd=tmp_path, env=env)
    assert result.returncode == 1
    assert "private term #1" in result.stdout


def test_term_that_normalizes_to_nothing_fails(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A term with no letters or digits would match everything: reject it."""
    env = with_terms(base_env, f"{TERM}\n...\n")
    result = run(["--require-list"], cwd=tmp_path, env=env)
    assert result.returncode == 2
    assert "#2" in result.stdout


def test_summary_reports_counts_only(tmp_path: Path, base_env: dict[str, str]) -> None:
    """A clean run says how many terms and files were checked, never which."""
    target = tmp_path / "notes.md"
    target.write_text("nothing private\n", encoding="utf-8")
    env = with_terms(base_env, f"{TERM}\nOther\n")
    result = run([str(target)], cwd=tmp_path, env=env)
    assert result.returncode == 0
    assert "2 term(s)" in result.stdout
    assert "1 file(s)" in result.stdout


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_finds_term_without_printing_it(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A match fails the run and the output never contains the term."""
    target = tmp_path / "notes.md"
    target.write_text(f"pickup for {TERM.lower()} at four\n", encoding="utf-8")
    result = run(
        [str(target)], cwd=tmp_path, env=with_terms(base_env, f"# c\n\n{TERM}\n")
    )
    assert result.returncode == 1
    assert "private term #1" in result.stdout
    assert_no_leak(result)


def test_word_boundary(tmp_path: Path, base_env: dict[str, str]) -> None:
    """A term embedded in a longer word does not match."""
    target = tmp_path / "notes.md"
    target.write_text(f"x{TERM}y\n{TERM}son\n{TERM}sonian\n", encoding="utf-8")
    assert (
        run([str(target)], cwd=tmp_path, env=with_terms(base_env, TERM)).returncode == 0
    )


def test_glued_occurrence_does_not_hide_a_later_real_one(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A rejected glued match (xTERM) must not stop the scan from finding TERM."""
    target = tmp_path / "notes.md"
    target.write_text(f"x{TERM}\n{TERM}\n", encoding="utf-8")
    result = run([str(target)], cwd=tmp_path, env=with_terms(base_env, TERM))
    assert result.returncode == 1
    assert f"{target}:2: private term #1" in result.stdout
    assert f"{target}:1:" not in result.stdout


@pytest.mark.parametrize(
    "text",
    [
        f"{TERM.lower()}_family notes",
        f"calendar_{TERM}.ics",
        f"{TERM}2 pickup",
        f"{TERM}s are late",
        f"{TERM}'s car",
        f"{TERM}\u2019s car",
        f"{TERM.upper()}",
        f"{TERM[:5]}\u200b{TERM[5:]}",
        f"{TERM[:5]}\u00ad{TERM[5:]}",
    ],
)
def test_matcher_catches_identifier_plural_and_invisible_variants(
    tmp_path: Path, base_env: dict[str, str], text: str
) -> None:
    """Underscores, digits, plurals, possessives and zero-width characters match."""
    target = tmp_path / "notes.md"
    target.write_text(f"{text}\n", encoding="utf-8")
    result = run([str(target)], cwd=tmp_path, env=with_terms(base_env, TERM))
    assert result.returncode == 1, text
    assert_no_leak(result)


@pytest.mark.parametrize(
    ("terms", "text"),
    [
        (TERM, f"alex_{TERM.lower()}_route"),
        ("Zebulonic\u00e0", f"{TERM} pickup"),
        (TERM, "ZEBULONIC\u00c1 pickup"),
        ("Quillon Vasterby", "Quillon   Vasterby"),
        (f"\ufeff  {TERM}\u200b  ", f"{TERM} pickup"),
        ("Unit #5 Quillon", "unit #5 quillon"),
    ],
)
def test_list_entries_are_cleaned_and_folded_like_text(
    tmp_path: Path, base_env: dict[str, str], terms: str, text: str
) -> None:
    """Accents, a BOM, zero-width characters and ``#`` inside a term all match."""
    target = tmp_path / "notes.md"
    target.write_text(f"{text}\n", encoding="utf-8")
    result = run([str(target)], cwd=tmp_path, env=with_terms(base_env, terms))
    assert result.returncode == 1, repr(terms)
    assert_no_leak(result)


@pytest.mark.parametrize(
    "text",
    [
        "John Smith",
        "john  smith",
        "John\tSmith",
        "john_smith",
        "john-smith",
        "JohnSmith",
        "John\nSmith",
        "john\n# smith",
        "John.Smith",
    ],
)
def test_multi_word_term_matches_spacing_and_wrapping(
    tmp_path: Path, base_env: dict[str, str], text: str
) -> None:
    """Internal spacing, separators and a line wrap do not hide a multi-word term."""
    target = tmp_path / "notes.md"
    target.write_text(f"{text}\n", encoding="utf-8")
    result = run([str(target)], cwd=tmp_path, env=with_terms(base_env, "John Smith"))
    assert result.returncode == 1, repr(text)
    assert_no_leak(result, "john smith", "smith")


def test_multi_word_term_reports_the_line_it_starts_on(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A wrapped match is reported at the line where it begins."""
    target = tmp_path / "notes.md"
    target.write_text("one\ntwo\njohn\nsmith\nfive\n", encoding="utf-8")
    result = run([str(target)], cwd=tmp_path, env=with_terms(base_env, "John Smith"))
    assert f"{target}:3: private term #1" in result.stdout


def test_unicode_normalization_and_accents(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """NFD text, accent-free spellings and curly apostrophes match the listed term."""
    target = tmp_path / "notes.md"
    nfd = unicodedata.normalize("NFD", "Jos\u00e9 O\u2019Brien")
    target.write_text(f"{nfd}\nJose OBrien\n", encoding="utf-8")
    env = with_terms(base_env, "Jos\u00e9 O'Brien")
    result = run([str(target)], cwd=tmp_path, env=env)
    assert result.returncode == 1
    assert result.stdout.count("private term #1") == 2


@pytest.mark.parametrize(("term", "text"), [("C++", "we use C++ here"), ("a.b", "a.b")])
def test_regex_special_terms_are_literal(
    tmp_path: Path, base_env: dict[str, str], term: str, text: str
) -> None:
    """Regex metacharacters in a term are matched literally."""
    target = tmp_path / "notes.md"
    target.write_text(f"{text}\n", encoding="utf-8")
    result = run([str(target)], cwd=tmp_path, env=with_terms(base_env, term))
    assert result.returncode == 1


def test_each_matching_term_is_reported_by_number(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """Two listed terms on one line yield one finding each, numbered by list order."""
    target = tmp_path / "notes.md"
    target.write_text("alpha and beta\n", encoding="utf-8")
    result = run([str(target)], cwd=tmp_path, env=with_terms(base_env, "beta\nalpha"))
    assert f"{target}:1: private term #1" in result.stdout
    assert f"{target}:1: private term #2" in result.stdout


# ---------------------------------------------------------------------------
# Output never reveals a term (the CI logs are public)
# ---------------------------------------------------------------------------


def test_term_in_file_name_is_not_printed(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A private term in a file name is reported with the name masked."""
    target = tmp_path / f"{TERM}-schedule.md"
    target.write_text(f"{TERM} lives here\n", encoding="utf-8")
    result = run([target.name], cwd=tmp_path, env=with_terms(base_env, TERM))
    assert result.returncode == 1
    assert "(path)" in result.stdout
    assert result.stdout.count("private term #1") == 2
    assert_no_leak(result)


def test_path_label_masks_only_the_offending_argument(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A clean argument keeps its name; the offending one is masked everywhere."""
    clean = tmp_path / "clean.md"
    clean.write_text("nothing\n", encoding="utf-8")
    target = tmp_path / f"{TERM.lower()}_notes.md"
    target.write_text(f"see {TERM}\n", encoding="utf-8")
    result = run(
        [clean.name, target.name], cwd=tmp_path, env=with_terms(base_env, TERM)
    )
    assert result.returncode == 1
    assert "[term #1] (path):1: private term #1" in result.stdout
    assert "[term #1]:1: private term #1" in result.stdout
    assert "clean.md" not in result.stdout
    assert_no_leak(result)


def test_term_in_directory_name_is_not_printed(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """Only the segment holding the term is masked; the rest of the path is kept."""
    folder = tmp_path / "docs" / TERM.lower() / "plans"
    folder.mkdir(parents=True)
    (folder / "week.md").write_text("clean\n", encoding="utf-8")
    rel = f"docs/{TERM.lower()}/plans/week.md"
    result = run([rel], cwd=tmp_path, env=with_terms(base_env, TERM))
    assert result.returncode == 1
    assert "docs/" in result.stdout
    assert "/plans/week.md" in result.stdout
    assert_no_leak(result)


def test_term_spanning_path_segments_is_not_printed(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A term split over two directory names cannot leak through the label."""
    folder = tmp_path / "john" / "smith"
    folder.mkdir(parents=True)
    (folder / "a.md").write_text("clean\n", encoding="utf-8")
    result = run(
        ["john/smith/a.md"], cwd=tmp_path, env=with_terms(base_env, "John Smith")
    )
    assert result.returncode == 1
    assert_no_leak(result, "john", "smith")


def test_term_in_symlink_target_is_found_and_not_printed(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A dangling symlink whose target text holds a term is still checked."""
    link = tmp_path / "link"
    link.symlink_to(f"/nowhere/{TERM}")
    result = run(["link"], cwd=tmp_path, env=with_terms(base_env, TERM))
    assert result.returncode == 1
    assert_no_leak(result)


# ---------------------------------------------------------------------------
# Files that are not UTF-8 text are scanned, not skipped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(f"caf\u00e9 {TERM}\n".encode("latin-1"), id="latin1"),
        pytest.param(f"{TERM}\n".encode("utf-16"), id="utf16-bom"),
        pytest.param(f"{TERM}\n".encode("utf-16-le"), id="utf16le-no-bom"),
        pytest.param(f"{TERM}\n".encode("utf-16-be"), id="utf16be-no-bom"),
        pytest.param(f"{TERM}\n".encode("utf-32"), id="utf32-bom"),
        pytest.param(
            b"\x89PNG\r\n\x00\x01\xff" + TERM.encode() + b"\x00\xfe", id="bin"
        ),
    ],
)
def test_non_utf8_content_is_scanned(
    tmp_path: Path, base_env: dict[str, str], payload: bytes
) -> None:
    """A term hidden in a Latin-1, UTF-16, UTF-32 or binary file still fails the run."""
    target = tmp_path / "blob.dat"
    target.write_bytes(payload)
    result = run([str(target)], cwd=tmp_path, env=with_terms(base_env, TERM))
    assert result.returncode == 1
    assert_no_leak(result)


def test_clean_binary_file_passes(tmp_path: Path, base_env: dict[str, str]) -> None:
    """Binary content without a term is not a finding and not a failure."""
    target = tmp_path / "blob.dat"
    target.write_bytes(bytes(range(256)) * 4)
    result = run([str(target)], cwd=tmp_path, env=with_terms(base_env, TERM))
    assert result.returncode == 0
    assert "1 file(s)" in result.stdout


def test_missing_file_fails_instead_of_being_skipped(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A path that cannot be read is an error (exit 4), never a silent pass."""
    result = run(["gone.md"], cwd=tmp_path, env=with_terms(base_env, TERM))
    assert result.returncode == 4
    assert "gone.md" in result.stdout
    assert "Traceback" not in result.output


def test_directory_entry_is_counted_not_hidden(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """A submodule or directory entry is skipped, and the summary says so."""
    (tmp_path / "vendor").mkdir()
    result = run(["vendor"], cwd=tmp_path, env=with_terms(base_env, TERM))
    assert result.returncode == 0
    assert "1 non-file entr" in result.stdout


def test_files_from_stdin_is_nul_separated(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """--files-from - reads a NUL-separated list (git ls-files -z) from stdin."""
    clean = tmp_path / "clean file.md"
    clean.write_text("fine\n", encoding="utf-8")
    dirty = tmp_path / "dirty.md"
    dirty.write_text(f"{TERM}\n", encoding="utf-8")
    listing = b"clean file.md\x00dirty.md\x00"
    result = run(
        ["--files-from", "-"],
        cwd=tmp_path,
        env=with_terms(base_env, TERM),
        stdin=listing,
    )
    assert result.returncode == 1
    assert "dirty.md:1: private term #1" in result.stdout
    assert "2 file(s)" in result.stdout


# ---------------------------------------------------------------------------
# Git history
# ---------------------------------------------------------------------------


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    """Run git in the scratch repo with signing off and an isolated config."""
    assert GIT is not None, "git is required for the history tests"
    done = run_process(
        [
            GIT,
            "-c",
            "user.name=Test Author",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.decode("utf-8", errors="replace").strip()


@pytest.fixture
def repo(tmp_path: Path, base_env: dict[str, str]) -> Path:
    """An initialised scratch repository."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, base_env, "init", "-q")
    return path


def _commit(repo: Path, env: dict[str, str], message: str, *extra: str) -> str:
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", message, *extra)
    return _git(repo, env, "rev-parse", "HEAD")


def test_git_range_scans_every_commit_and_names_the_culprit(
    repo: Path, base_env: dict[str, str]
) -> None:
    """Only the commit that added the term is flagged; clean commits are not."""
    (repo / "a.md").write_text("clean\n", encoding="utf-8")
    base = _commit(repo, base_env, "base")
    (repo / "a.md").write_text(f"{TERM}\n", encoding="utf-8")
    add = _commit(repo, base_env, "add notes")
    (repo / "a.md").write_text("clean again\n", encoding="utf-8")
    remove = _commit(repo, base_env, "tidy notes")
    env = with_terms(base_env, TERM)
    assert run(["a.md"], cwd=repo, env=env).returncode == 0
    history = run(["--git-range", f"{base}..{remove}"], cwd=repo, env=env)
    assert history.returncode == 1
    assert f"commit {add[:12]}:" in history.stdout
    assert f"commit {remove[:12]}:" in history.stdout  # the removal diff shows it
    assert f"commit {base[:12]}:" not in history.stdout
    assert "2 commit(s)" in history.stdout
    assert_no_leak(history)


def test_git_range_scans_commit_messages(repo: Path, base_env: dict[str, str]) -> None:
    """A term in a commit message is found even when the diff is clean."""
    (repo / "a.md").write_text("clean\n", encoding="utf-8")
    base = _commit(repo, base_env, "base")
    (repo / "b.md").write_text("clean too\n", encoding="utf-8")
    tip = _commit(repo, base_env, f"notes for {TERM}")
    result = run(
        ["--git-range", f"{base}..{tip}"], cwd=repo, env=with_terms(base_env, TERM)
    )
    assert result.returncode == 1
    assert f"commit {tip[:12]}:" in result.stdout
    assert_no_leak(result)


def test_git_range_scans_author_and_committer_identity(
    repo: Path, base_env: dict[str, str]
) -> None:
    """A term in the author name or e-mail of a commit is found and not printed."""
    (repo / "a.md").write_text("clean\n", encoding="utf-8")
    base = _commit(repo, base_env, "base")
    (repo / "b.md").write_text("clean too\n", encoding="utf-8")
    tip = _commit(
        repo,
        base_env,
        "more notes",
        f"--author={TERM} Person <{TERM.lower()}@example.invalid>",
    )
    result = run(
        ["--git-range", f"{base}..{tip}"], cwd=repo, env=with_terms(base_env, TERM)
    )
    assert result.returncode == 1
    assert f"commit {tip[:12]}:" in result.stdout
    assert_no_leak(result)


def test_git_range_masks_terms_in_committed_file_names(
    repo: Path, base_env: dict[str, str]
) -> None:
    """A term in a committed path (non-ASCII quoting included) is not echoed."""
    (repo / "a.md").write_text("clean\n", encoding="utf-8")
    base = _commit(repo, base_env, "base")
    (repo / f"{TERM}-caf\u00e9.md").write_text("clean\n", encoding="utf-8")
    tip = _commit(repo, base_env, "add file")
    result = run(
        ["--git-range", f"{base}..{tip}"], cwd=repo, env=with_terms(base_env, TERM)
    )
    assert result.returncode == 1
    assert f"commit {tip[:12]}:" in result.stdout
    assert_no_leak(result)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(f"{TERM}\n".encode("utf-16"), id="utf16"),
        pytest.param(f"{TERM}\n".encode("utf-16-le"), id="utf16le-no-bom"),
        pytest.param(f"caf\u00e9 {TERM}\n".encode("latin-1"), id="latin1"),
        pytest.param(b"\x00\x01\x02" + TERM.encode() + b"\x00\xff", id="binary"),
    ],
)
def test_git_range_scans_non_text_content(
    repo: Path, base_env: dict[str, str], payload: bytes
) -> None:
    """History scanning sees terms inside UTF-16, Latin-1 and binary blobs."""
    (repo / "a.md").write_text("clean\n", encoding="utf-8")
    base = _commit(repo, base_env, "base")
    (repo / "blob.dat").write_bytes(payload)
    tip = _commit(repo, base_env, "add blob")
    result = run(
        ["--git-range", f"{base}..{tip}"], cwd=repo, env=with_terms(base_env, TERM)
    )
    assert result.returncode == 1, result.stdout
    assert f"commit {tip[:12]}:" in result.stdout
    assert_no_leak(result)


def test_empty_git_range_says_zero_commits(
    repo: Path, base_env: dict[str, str]
) -> None:
    """An empty range passes but reports that it scanned nothing."""
    (repo / "a.md").write_text("clean\n", encoding="utf-8")
    head = _commit(repo, base_env, "base")
    result = run(
        ["--git-range", f"{head}..{head}"], cwd=repo, env=with_terms(base_env, TERM)
    )
    assert result.returncode == 0
    assert "0 commit(s)" in result.stdout


def test_git_range_bad_revision(repo: Path, base_env: dict[str, str]) -> None:
    """An unreadable range is reported as a git failure, not as clean."""
    result = run(
        ["--git-range", "nope..nada"], cwd=repo, env=with_terms(base_env, TERM)
    )
    assert result.returncode == 3


def test_git_range_cannot_inject_options(repo: Path, base_env: dict[str, str]) -> None:
    """A range that looks like an option is a revision, so it fails as one."""
    result = run(["--git-range=--all"], cwd=repo, env=with_terms(base_env, TERM))
    assert result.returncode == 3


def test_missing_git_binary_is_a_git_failure(
    tmp_path: Path, base_env: dict[str, str]
) -> None:
    """No git on PATH exits 3 with a message, not a traceback that looks like 1."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    env = with_terms({**base_env, "PATH": str(empty)}, TERM)
    result = run(["--git-range", "HEAD"], cwd=tmp_path, env=env)
    assert result.returncode == 3
    assert "Traceback" not in result.output
