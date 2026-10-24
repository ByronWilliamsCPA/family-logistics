#!/usr/bin/env python3
"""Block commits that contain a household's private terms.

This repository is public, so real names, places and other household details
must never be committed. The list of forbidden terms cannot live in the
repository either (the list itself would disclose them), so it is read from
outside the working tree:

1. ``FL_PRIVATE_TERMS``: newline-separated terms (the CI secret), or
2. the file named by ``FL_PRIVATE_TERMS_FILE`` (an error when it is set but
   names no readable file), or
3. ``~/.config/family-logistics/private-terms.txt``.

Blank lines and lines starting with ``#`` in the list are ignored. A UTF-8 BOM
and CRLF line endings are tolerated.

Matching is case-insensitive and ignores accents, zero-width characters and
Unicode normalization form. A term matches when it is not glued to a longer
word: ``smith_family``, ``smith2``, ``Smiths`` and ``Smith's`` match ``Smith``,
``Smithson`` does not. The words of a multi-word term may be separated by up
to eight spaces, line breaks, comment markers, dots, hyphens or underscores,
or by nothing (``John Smith``, ``john_smith``, ``JohnSmith`` and a wrapped
line all match).

What is scanned:

* the content of every file argument (or ``--files-from`` entry), and the text
  of symbolic links;
* every file path;
* with ``--git-range``, every commit in the range: author and committer names
  and e-mail addresses, the message, and the full diff including the paths in
  the diff headers, so a term added and later removed is still caught.

Because commit identities are scanned, the term list must not contain anything
that appears in a maintainer's own commit name or e-mail address (such as the
family surname): identity scanning would then fail every commit. The intended
terms are children's names, school names and street names.

Files are decoded as UTF-8 when possible. A file that is not valid UTF-8 is
never skipped: it is scanned as Latin-1, as UTF-16 and UTF-32 (with or without
a BOM) and with NUL bytes removed, so a term inside a UTF-16 document, a Latin-1
file or a binary asset is still found. Git diffs are taken with ``--text`` for
the same reason.

Output never contains a private term. Findings report the file, the line and
the term's number in the list (``private term #3``). A path that matches a
term is printed with each offending segment replaced by ``[term #N]`` (the
whole path when no single segment matches). Summary lines carry counts only.

Fail-closed behavior: a missing or unreadable file argument, an unusable term
list and a failing git command all stop the run with a non-zero status. The
only entries skipped are directories (submodules) and other non-regular files,
and the summary counts them. Not covered: pull request titles and bodies,
branch names and tag annotations, which git history does not carry.

When no list is configured the check skips with a warning, because a
contributor without the list cannot run it. Pass ``--require-list`` or set
``FL_PRIVATE_TERMS_REQUIRED=1`` to fail closed instead (CI and the owner's
machines).

Exit codes: 0 clean (or skipped), 1 a private term was found, 2 the term list
is missing (when required), unreadable or contains an empty term, 3 git failed
or is not installed while reading ``--git-range``, 4 a file could not be read.
"""

from __future__ import annotations

import argparse
import bisect
import codecs
import os
import re
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LIST = Path.home() / ".config" / "family-logistics" / "private-terms.txt"

# A neighbouring letter makes a match part of a longer word. Digits, underscores
# and punctuation do not: smith_family and smith2 are still Smith.
_LETTER = r"[^\W\d_]"
# Separators allowed between the words of a multi-word term.
_SEPARATOR = r"[\W_]{0,8}"
# Plural and possessive endings that still count as the term.
_SUFFIX = r"(?:'s|es|s)?"
# Characters that split a term into words.
_WORD_SPLIT = re.compile(r"[\s\-'._]+")

_TRANSLATE = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u02bc": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)

_INVISIBLE_CATEGORIES = frozenset({"Mn", "Cf"})


class TermListError(Exception):
    """The term list is unusable. The message never contains a term."""


@dataclass
class Stats:
    """Counters for the summary line (counts only, never names)."""

    files: int = 0
    commits: int = 0
    skipped: int = 0
    errors: int = 0
    terms: int = 0
    unreadable: list[str] = field(default_factory=list)


def _strip_format_chars(text: str) -> str:
    """Remove invisible format characters such as a BOM or zero-width space."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def fold(text: str) -> str:
    """Normalize text so visually equal spellings compare equal.

    Decomposes, drops accents and zero-width characters, unifies curly quotes
    and dashes, and case-folds. The number of newlines never changes, so line
    numbers computed on the folded text are the original line numbers.

    Args:
        text: Text to normalize.

    Returns:
        The folded text.
    """
    if text.isascii():
        return text.casefold()
    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(
        c for c in decomposed if unicodedata.category(c) not in _INVISIBLE_CATEGORIES
    )
    return kept.translate(_TRANSLATE).casefold()


def load_terms() -> list[str]:
    """Load the private terms from the environment or the local list file.

    Returns:
        The configured terms, or an empty list when none are configured.

    Raises:
        TermListError: When FL_PRIVATE_TERMS_FILE names no readable file, or the
            list file is not readable UTF-8 text.
    """
    raw = os.environ.get("FL_PRIVATE_TERMS")
    if raw is None:
        configured = os.environ.get("FL_PRIVATE_TERMS_FILE")
        path = Path(configured) if configured else DEFAULT_LIST
        if not path.is_file():
            if configured:
                msg = "FL_PRIVATE_TERMS_FILE is set but does not name a readable file."
                raise TermListError(msg)
            return []
        try:
            raw = path.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            msg = (
                "The private-terms list is not readable UTF-8 text "
                f"({type(exc).__name__})."
            )
            raise TermListError(msg) from None
    terms: list[str] = []
    for line in raw.splitlines():
        cleaned = _strip_format_chars(line).strip()
        if cleaned and not cleaned.startswith("#"):
            terms.append(cleaned)
    return terms


class Matcher:
    """Case-, accent- and spacing-tolerant matcher for the private terms."""

    def __init__(self, terms: list[str]) -> None:
        """Compile one pattern per term.

        Args:
            terms: Private terms, in list order (their number is the report key).

        Raises:
            TermListError: When a term has no letters or digits left after
                normalization, which would match everything.
        """
        self.patterns: list[re.Pattern[str]] = []
        self._first_words: list[str] = []
        for number, term in enumerate(terms, start=1):
            words = [w for w in _WORD_SPLIT.split(fold(term)) if w]
            if not words:
                msg = f"Private term #{number} is empty after normalization."
                raise TermListError(msg)
            body = _SEPARATOR.join(re.escape(word) for word in words)
            # The leading word boundary is checked by hand in hits(): a pattern
            # that starts with a lookbehind cannot use the regex engine's fast
            # literal-prefix search, which made large scans 10x slower.
            self.patterns.append(re.compile(rf"{body}{_SUFFIX}(?!{_LETTER})"))
            self._first_words.append(words[0])

    def hits(self, text: str) -> set[tuple[int, int]]:
        """Find private terms in text.

        Args:
            text: Text to search.

        Returns:
            Pairs of (line number, term number); a match is reported on the line
            where it starts.
        """
        folded = fold(text)
        # Every match contains its first word verbatim, so a plain substring
        # test rules out most terms (and most files) cheaply.
        candidates = [
            number
            for number, word in enumerate(self._first_words, start=1)
            if word in folded
        ]
        if not candidates:
            return set()
        newlines = [m.start() for m in re.finditer("\n", folded)]
        found: set[tuple[int, int]] = set()
        for number in candidates:
            pattern = self.patterns[number - 1]
            pos = 0
            while (match := pattern.search(folded, pos)) is not None:
                start = match.start()
                glued = start > 0 and _is_letter(folded[start - 1])
                if not glued:
                    found.add((bisect.bisect_left(newlines, start) + 1, number))
                pos = start + 1
        return found


def _is_letter(char: str) -> bool:
    """Report whether a character is a letter (digits and underscores are not)."""
    return char.isalpha()


def _decode(data: bytes) -> str:
    """Decode as UTF-8 when valid, otherwise as Latin-1 (which cannot fail)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _is_utf8(data: bytes) -> bool:
    """Report whether the bytes are valid UTF-8."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def decode_variants(data: bytes) -> list[str]:
    """Return every plausible text reading of a byte string.

    Args:
        data: File or git output bytes.

    Returns:
        The primary reading first, then UTF-16/UTF-32 readings and a NUL-free
        reading when the data contains NUL bytes.
    """
    variants = [_decode(data)]
    if not _is_utf8(data):
        # Binary or legacy-encoded data: bytes above 0x7f are not letters, so a
        # term next to them still has a word boundary.
        variants.append(data.decode("ascii", errors="replace"))
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        variants.append(data.decode("utf-32", errors="replace"))
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        variants.append(data.decode("utf-16", errors="replace"))
    if b"\x00" in data:
        stripped = data.replace(b"\x00", b"")
        variants.append(_decode(stripped))
        variants.append(stripped.decode("ascii", errors="replace"))
        variants.append(data.decode("utf-16-le", errors="replace"))
        variants.append(data.decode("utf-16-be", errors="replace"))
    return variants


def blob_hits(data: bytes, matcher: Matcher) -> set[tuple[int, int]]:
    """Find private terms in bytes, trying every plausible encoding.

    Args:
        data: Bytes to search.
        matcher: The compiled private terms.

    Returns:
        Pairs of (line number, term number) over all readings.
    """
    found: set[tuple[int, int]] = set()
    for text in decode_variants(data):
        found |= matcher.hits(text)
    return found


def safe_path(name: str, matcher: Matcher) -> str:
    """Return a printable form of a path that never contains a private term.

    Args:
        name: The path as given.
        matcher: The compiled private terms.

    Returns:
        The path unchanged when it holds no term; otherwise each matching
        segment is replaced by ``[term #N]``, or the whole path when no single
        segment matches.
    """
    if not matcher.hits(name):
        return name
    parts: list[str] = []
    for segment in name.split("/"):
        numbers = sorted({n for _, n in matcher.hits(segment)})
        parts.append(
            "[term " + ",".join(f"#{n}" for n in numbers) + "]" if numbers else segment
        )
    masked = "/".join(parts)
    if matcher.hits(masked):
        numbers = sorted({n for _, n in matcher.hits(name)})
        return "[path hidden: term " + ",".join(f"#{n}" for n in numbers) + "]"
    return masked


def _report(label: str, hits: set[tuple[int, int]]) -> int:
    """Print one redacted finding per (line, term) and return how many."""
    for lineno, number in sorted(hits):
        print(f"{label}:{lineno}: private term #{number}")
    return len(hits)


def scan_files(paths: list[str], matcher: Matcher, stats: Stats) -> int:
    """Scan file paths and file contents.

    Args:
        paths: Files to scan.
        matcher: The compiled private terms.
        stats: Counters updated for the summary.

    Returns:
        The number of findings.
    """
    findings = 0
    for name in paths:
        label = safe_path(name, matcher)
        findings += _report(f"{label} (path)", matcher.hits(name))
        path = Path(name)
        if path.is_symlink():
            findings += _report(
                f"{label} (symlink target)", matcher.hits(os.readlink(path))
            )
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            if path.is_symlink():
                stats.files += 1
                continue
            print(f"{label}: file not found")
            stats.errors += 1
            continue
        except OSError as exc:
            print(f"{label}: cannot read ({type(exc).__name__})")
            stats.errors += 1
            continue
        if not stat.S_ISREG(mode):
            stats.skipped += 1
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(f"{label}: cannot read ({type(exc).__name__})")
            stats.errors += 1
            continue
        stats.files += 1
        findings += _report(label, blob_hits(data, matcher))
    return findings


def _git(*args: str) -> bytes:
    """Run a read-only git command and return its raw output.

    Args:
        *args: Arguments after ``git``.

    Returns:
        Standard output bytes.

    Raises:
        subprocess.CalledProcessError: When git exits non-zero.
        OSError: When git cannot be started (for example it is not installed).
    """
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        check=True,
        capture_output=True,
    )
    return result.stdout


_COMMIT_FORMAT = "--format=%an%n%ae%n%cn%n%ce%n%B"


def scan_git_range(rev_range: str, matcher: Matcher, stats: Stats) -> int:
    """Scan every commit in a revision range.

    Author and committer identity, the message and the whole diff (binary and
    non-UTF-8 content included, paths included) are checked.

    Args:
        rev_range: A git revision range such as ``base..head``, or a single
            revision to scan its whole history.
        matcher: The compiled private terms.
        stats: Counters updated for the summary.

    Returns:
        The number of findings.

    Raises:
        subprocess.CalledProcessError: When git fails.
        OSError: When git cannot be started.
    """
    findings = 0
    shas = _git("rev-list", "--end-of-options", rev_range).decode("ascii").split()
    for sha in shas:
        shown = _git(
            "show",
            "--no-color",
            "--no-textconv",
            "--no-ext-diff",
            "--text",
            _COMMIT_FORMAT,
            "-p",
            sha,
        )
        stats.commits += 1
        findings += _report(f"commit {sha[:12]}", blob_hits(shown, matcher))
    return findings


def _read_file_list(source: str) -> list[str]:
    """Read a NUL-separated path list from a file, or from stdin for ``-``."""
    if source == "-":
        data = sys.stdin.buffer.read()
    else:
        data = Path(source).read_bytes()
    return [os.fsdecode(item) for item in data.split(b"\x00") if item]


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("files", nargs="*", help="files to scan")
    parser.add_argument(
        "--files-from",
        metavar="FILE",
        help="also scan the NUL-separated paths in FILE ('-' reads stdin), as "
        "written by git ls-files -z",
    )
    parser.add_argument(
        "--require-list",
        action="store_true",
        help="fail when no private-terms list is configured (CI, owner machines)",
    )
    parser.add_argument(
        "--git-range",
        metavar="RANGE",
        help="also scan commit identities, messages and diffs in this git "
        "revision range",
    )
    return parser


def _print_summary(stats: Stats, *, scanned_history: bool) -> None:
    """Print the counts-only summary line."""
    parts = [f"{stats.terms} term(s) loaded", f"{stats.files} file(s) scanned"]
    if scanned_history:
        parts.append(f"{stats.commits} commit(s) scanned")
    parts.append(f"{stats.skipped} non-file entries skipped")
    parts.append(f"{stats.errors} unreadable")
    print("private-terms: " + ", ".join(parts))


def main() -> int:
    """Run the check.

    Returns:
        The process exit code.
    """
    args = _build_parser().parse_args()
    required = args.require_list or os.environ.get("FL_PRIVATE_TERMS_REQUIRED") == "1"
    stats = Stats()
    try:
        terms = load_terms()
        matcher = Matcher(terms) if terms else None
    except TermListError as exc:
        print(str(exc))
        return 2
    if matcher is None:
        if required:
            print("No private-terms list configured (FL_PRIVATE_TERMS).")
            return 2
        print(
            "WARNING private-terms: no list configured, check SKIPPED. Create "
            f"{DEFAULT_LIST} (or set FL_PRIVATE_TERMS_REQUIRED=1 to fail closed)."
        )
        return 0
    stats.terms = len(terms)
    paths = list(args.files)
    if args.files_from:
        try:
            paths.extend(_read_file_list(args.files_from))
        except OSError as exc:
            print(f"Cannot read the --files-from list ({type(exc).__name__}).")
            return 4
    findings = scan_files(paths, matcher, stats)
    if args.git_range:
        try:
            findings += scan_git_range(args.git_range, matcher, stats)
        except (subprocess.CalledProcessError, OSError, UnicodeDecodeError):
            print("git failed while reading --git-range (bad revision or git missing).")
            return 3
    _print_summary(stats, scanned_history=bool(args.git_range))
    if findings:
        print(
            f"{findings} private term(s) found. Replace them with the fictional "
            "household used in tests and docs before committing."
        )
        return 1
    if stats.errors:
        print(f"{stats.errors} file(s) could not be read; the check is incomplete.")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
