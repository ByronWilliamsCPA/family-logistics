#!/usr/bin/env python3
"""Block commits that contain a household's private terms.

This repository is public, so real names, places and other household details
must never be committed. The list of forbidden terms cannot live in the
repository either (the list itself would disclose them), so it is read from
outside the working tree:

1. ``FL_PRIVATE_TERMS``: newline-separated terms (the CI secret), or
2. the file named by ``FL_PRIVATE_TERMS_FILE``, or
3. ``~/.config/family-logistics/private-terms.txt``.

List parsing: one term per line. A UTF-8 byte-order mark, surrounding
whitespace and invisible format characters (zero-width spaces, joiners) are
removed. Blank lines and lines whose first visible character is ``#`` are
comments; there are no inline comments, so ``#`` later in a line is part of
the term. A list that is not valid UTF-8 is a configuration error (exit 2).

Matching: text and terms are compared after Unicode NFKC normalisation,
accent stripping and case folding, so ``Zoe`` also matches ``Zoë``. A term
matches when it is not directly preceded or followed by a letter, so it is
found inside ``snake_case`` names, file names and next to digits
(``Riley_2``), but not inside a longer word. The words of a multi-word term
may be separated by any run of whitespace, ``_``, ``-`` or ``.``.

File contents and file paths are scanned; ``--git-range`` also scans every
commit message and diff in a revision range, so a term that was added and
later removed is still caught. Output never contains a term: findings give a
location and the term's number in the list, and a file whose path contains a
term is shown as ``<file argument #N>`` (its position on the command line)
instead of by name.

Fail closed: a file that is not valid UTF-8 or cannot be read, a binary change
in ``--git-range`` and history text that is not valid UTF-8 are reported as
unscannable and fail the run, because a term inside them could not be ruled
out. This repository tracks no binary files; adding one requires changing
this policy deliberately.

When no list is configured the check skips with a warning, because a
contributor without the list cannot run it. Pass ``--require-list`` or set
``FL_PRIVATE_TERMS_REQUIRED=1`` to fail closed instead (CI and the owner's
machines).

Exit codes: 0 clean (or skipped), 1 a private term was found, 2 a list is
required but none is configured (or the list is unreadable), 3 git failed
while reading ``--git-range``, 4 no term was found but some content could not
be scanned.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess  # nosec B404 - fixed argv lists, no shell
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIST = Path.home() / ".config" / "family-logistics" / "private-terms.txt"

_SEPARATOR = r"[\s_.\-]+"
_NOT_LETTER_BEFORE = r"(?<![^\W\d_])"
_NOT_LETTER_AFTER = r"(?![^\W\d_])"


class TermListError(Exception):
    """The configured private-terms list cannot be read."""


@dataclass
class Report:
    """Counts of findings and unscannable inputs."""

    findings: int = 0
    unscannable: int = 0

    @staticmethod
    def emit(line: str) -> None:
        """Print one output line; callers never pass a term."""
        print(line)


def normalise(text: str) -> str:
    """Normalise text for matching: NFKC, strip accents, case-fold.

    Args:
        text: Text to normalise.

    Returns:
        The normalised text.
    """
    decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", stripped).casefold()


def _clean_entry(line: str) -> str:
    """Remove BOM, format characters and surrounding whitespace from a line."""
    visible = "".join(ch for ch in line if unicodedata.category(ch) != "Cf")
    return visible.strip()


def parse_terms(raw: str) -> list[str]:
    """Parse a newline-separated term list.

    Args:
        raw: The list text.

    Returns:
        The terms, without comments, blanks or duplicates, in list order.
    """
    terms: list[str] = []
    for line in raw.splitlines():
        entry = _clean_entry(line)
        if entry and not entry.startswith("#") and entry not in terms:
            terms.append(entry)
    return terms


def load_terms() -> list[str]:
    """Load the private terms from the environment or the local list file.

    Returns:
        The configured terms, or an empty list when none are configured.

    Raises:
        TermListError: The list file exists but is not readable UTF-8.
    """
    raw = os.environ.get("FL_PRIVATE_TERMS")
    if raw is None:
        path = Path(os.environ.get("FL_PRIVATE_TERMS_FILE", str(DEFAULT_LIST)))
        if not path.is_file():
            return []
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise TermListError(type(exc).__name__) from None
    return parse_terms(raw)


def compile_patterns(terms: list[str]) -> list[re.Pattern[str]]:
    """Compile one pattern per term, for use on normalised text.

    Args:
        terms: Private terms to search for.

    Returns:
        The compiled patterns, in list order.
    """
    patterns: list[re.Pattern[str]] = []
    for term in terms:
        words = re.split(_SEPARATOR, normalise(term))
        body = _SEPARATOR.join(re.escape(word) for word in words if word)
        patterns.append(re.compile(f"{_NOT_LETTER_BEFORE}{body}{_NOT_LETTER_AFTER}"))
    return patterns


def matching_terms(text: str, patterns: list[re.Pattern[str]]) -> list[int]:
    """Return the 1-based numbers of the terms found in ``text``."""
    folded = normalise(text)
    return [i for i, pattern in enumerate(patterns, start=1) if pattern.search(folded)]


def scan_text(
    label: str, text: str, patterns: list[re.Pattern[str]], report: Report
) -> None:
    """Scan text and report one redacted finding per matching line and term.

    Args:
        label: Location prefix; must not contain a term.
        text: Text to scan.
        patterns: Compiled private-term patterns.
        report: Collects the findings.
    """
    for lineno, line in enumerate(text.splitlines(), start=1):
        for index in matching_terms(line, patterns):
            report.emit(f"{label}:{lineno}: private term #{index}")
            report.findings += 1


def scan_files(
    paths: list[str], patterns: list[re.Pattern[str]], report: Report
) -> None:
    """Scan file paths and file contents.

    Args:
        paths: Files to scan.
        patterns: Compiled private-term patterns.
        report: Collects findings and unscannable files.
    """
    for position, name in enumerate(paths, start=1):
        label = name
        hits = matching_terms(name, patterns)
        if hits:
            label = f"<file argument #{position}>"
            for index in hits:
                report.emit(f"{label} (path): private term #{index}")
                report.findings += 1
        path = Path(name)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.emit(f"{label}: unscannable ({type(exc).__name__})")
            report.unscannable += 1
            continue
        scan_text(label, text, patterns, report)


def _git(*args: str) -> bytes:
    """Run a read-only git command and return its raw output.

    Args:
        *args: Arguments after ``git``.

    Returns:
        Standard output as bytes.
    """
    result = subprocess.run(  # nosec B603 B607 - fixed git argv, no shell
        ["git", *args],
        check=True,
        capture_output=True,
    )
    return result.stdout


def scan_git_range(
    rev_range: str, patterns: list[re.Pattern[str]], report: Report
) -> None:
    """Scan every commit message and diff in a revision range.

    Binary changes (``-`` in ``--numstat``) and history text that is not
    valid UTF-8 are reported as unscannable.

    Args:
        rev_range: A git revision range such as ``base..head``, or a single
            revision to scan its whole history.
        patterns: Compiled private-term patterns.
        report: Collects findings and unscannable commits.
    """
    for sha in _git("rev-list", rev_range).decode("ascii").split():
        label = f"commit {sha[:12]}"
        numstat = _git("show", "--no-color", "--format=", "--numstat", sha)
        binary = sum(1 for row in numstat.splitlines() if row.startswith(b"-\t-\t"))
        if binary:
            report.emit(f"{label}: {binary} binary change(s) unscannable")
            report.unscannable += 1
        raw = _git("show", "--no-color", "--format=%B", "-p", sha)
        try:
            show = raw.decode("utf-8")
        except UnicodeDecodeError:
            report.emit(f"{label}: unscannable (not valid UTF-8)")
            report.unscannable += 1
            show = raw.decode("utf-8", errors="replace")
        scan_text(label, show, patterns, report)


def main() -> int:
    """Run the check.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="files to scan")
    parser.add_argument(
        "--require-list",
        action="store_true",
        help="fail when no private-terms list is configured (CI, owner machines)",
    )
    parser.add_argument(
        "--git-range",
        metavar="RANGE",
        help="also scan commit messages and diffs in this git revision range",
    )
    args = parser.parse_args()
    required = args.require_list or os.environ.get("FL_PRIVATE_TERMS_REQUIRED") == "1"
    try:
        terms = load_terms()
    except TermListError as exc:
        print(f"Private-terms list unreadable ({exc}); fix the list file.")
        return 2
    if not terms:
        if required:
            print("No private-terms list configured (FL_PRIVATE_TERMS).")
            return 2
        print(
            "WARNING private-terms: no list configured, check SKIPPED. Create "
            f"{DEFAULT_LIST} (or set FL_PRIVATE_TERMS_REQUIRED=1 to fail closed)."
        )
        return 0
    patterns = compile_patterns(terms)
    report = Report()
    scan_files(args.files, patterns, report)
    if args.git_range:
        try:
            scan_git_range(args.git_range, patterns, report)
        except subprocess.CalledProcessError:
            print("git failed while reading the --git-range revision range.")
            return 3
    if report.findings:
        print(
            f"{report.findings} private term(s) found. Replace them with the "
            "fictional household used in tests and docs before committing."
        )
        return 1
    if report.unscannable:
        print(
            f"{report.unscannable} input(s) could not be scanned; a private term "
            "cannot be ruled out, so the check fails closed."
        )
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
