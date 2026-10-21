#!/usr/bin/env python3
"""Block commits that contain a household's private terms.

This repository is public, so real names, places and other household details
must never be committed. The list of forbidden terms cannot live in the
repository either (the list itself would disclose them), so it is read from
outside the working tree:

1. ``FL_PRIVATE_TERMS``: newline-separated terms (the CI secret), or
2. the file named by ``FL_PRIVATE_TERMS_FILE``, or
3. ``~/.config/family-logistics/private-terms.txt``.

Blank lines and lines starting with ``#`` in the list are ignored. Matching is
case-insensitive on word boundaries. File contents and file paths are scanned;
``--git-range`` also scans every commit message and diff in a revision range,
so a term that was added and later removed is still caught. Findings report
the location and term number only, never the term, so CI logs do not reveal
the list.

When no list is configured the check skips with a warning, because a
contributor without the list cannot run it. Pass ``--require-list`` or set
``FL_PRIVATE_TERMS_REQUIRED=1`` to fail closed instead (CI and the owner's
machines).

Exit codes: 0 clean (or skipped), 1 a private term was found, 2 a list is
required but none is configured, 3 git failed while reading ``--git-range``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess  # nosec B404 - fixed argv lists, no shell
import sys
from pathlib import Path

DEFAULT_LIST = Path.home() / ".config" / "family-logistics" / "private-terms.txt"


def load_terms() -> list[str]:
    """Load the private terms from the environment or the local list file.

    Returns:
        The configured terms, or an empty list when none are configured.
    """
    raw = os.environ.get("FL_PRIVATE_TERMS")
    if raw is None:
        path = Path(os.environ.get("FL_PRIVATE_TERMS_FILE", str(DEFAULT_LIST)))
        if not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8")
    return [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def compile_patterns(terms: list[str]) -> list[re.Pattern[str]]:
    """Compile one case-insensitive word-boundary pattern per term.

    Args:
        terms: Private terms to search for.

    Returns:
        The compiled patterns, in list order.
    """
    return [
        re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE) for term in terms
    ]


def scan_text(label: str, text: str, patterns: list[re.Pattern[str]]) -> int:
    """Scan text and print one redacted finding per matching line and term.

    Args:
        label: Location prefix printed with each finding.
        text: Text to scan.
        patterns: Compiled private-term patterns.

    Returns:
        The number of findings.
    """
    findings = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        for index, pattern in enumerate(patterns, start=1):
            if pattern.search(line):
                print(f"{label}:{lineno}: private term #{index}")
                findings += 1
    return findings


def scan_files(paths: list[str], patterns: list[re.Pattern[str]]) -> int:
    """Scan file paths and file contents.

    Args:
        paths: Files to scan.
        patterns: Compiled private-term patterns.

    Returns:
        The number of findings.
    """
    findings = 0
    for name in paths:
        findings += scan_text(f"{name} (path)", name, patterns)
        path = Path(name)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings += scan_text(name, text, patterns)
    return findings


def _git(*args: str) -> str:
    """Run a read-only git command and return its output.

    Args:
        *args: Arguments after ``git``.

    Returns:
        Standard output decoded as UTF-8 with replacement.
    """
    result = subprocess.run(  # nosec B603 B607 - fixed git argv, no shell
        ["git", *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def scan_git_range(rev_range: str, patterns: list[re.Pattern[str]]) -> int:
    """Scan every commit message and diff in a revision range.

    Args:
        rev_range: A git revision range such as ``base..head``, or a single
            revision to scan its whole history.
        patterns: Compiled private-term patterns.

    Returns:
        The number of findings.
    """
    findings = 0
    for sha in _git("rev-list", rev_range).split():
        show = _git("show", "--no-color", "--format=%B", "-p", sha)
        findings += scan_text(f"commit {sha[:12]}", show, patterns)
    return findings


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
    terms = load_terms()
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
    findings = scan_files(args.files, patterns)
    if args.git_range:
        try:
            findings += scan_git_range(args.git_range, patterns)
        except subprocess.CalledProcessError:
            print(f"git failed while reading range {args.git_range!r}.")
            return 3
    if findings:
        print(
            f"{findings} private term(s) found. Replace them with the fictional "
            "household used in tests and docs before committing."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
