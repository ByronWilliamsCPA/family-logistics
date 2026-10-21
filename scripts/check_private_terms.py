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
case-insensitive on word boundaries. Findings report the file, line and term
number only, never the term, so CI logs do not reveal the list.

Exit codes: 0 clean (or no list configured, unless ``--require-list``),
1 a private term was found, 2 ``--require-list`` was given but no list exists.
"""

from __future__ import annotations

import argparse
import os
import re
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


def scan(paths: list[str], terms: list[str]) -> int:
    """Scan files for private terms and print redacted findings.

    Args:
        paths: Files to scan.
        terms: Private terms to search for.

    Returns:
        The number of findings.
    """
    patterns = [
        re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE) for term in terms
    ]
    findings = 0
    for name in paths:
        path = Path(name)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for index, pattern in enumerate(patterns, start=1):
                if pattern.search(line):
                    print(f"{name}:{lineno}: private term #{index}")
                    findings += 1
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
        help="fail when no private-terms list is configured (used in CI)",
    )
    args = parser.parse_args()
    terms = load_terms()
    if not terms:
        if args.require_list:
            print("No private-terms list configured (FL_PRIVATE_TERMS).")
            return 2
        print(
            "private-terms: no list configured, skipping. Create "
            f"{DEFAULT_LIST} to enable this check locally."
        )
        return 0
    findings = scan(args.files, terms)
    if findings:
        print(
            f"{findings} private term(s) found. Replace them with the fictional "
            "household used in tests and docs before committing."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
