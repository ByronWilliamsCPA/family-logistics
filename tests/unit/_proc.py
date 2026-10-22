"""Subprocess helper shared by the guard tests.

The tests that exercise git and the private-terms checker as real processes all
go through :func:`run_process`.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a fixed argv list without a shell and capture its output as bytes.

    Args:
        argv: Executable (an absolute path) followed by its arguments.
        cwd: Working directory for the process.
        env: Complete environment for the process (nothing is inherited).
        input_bytes: Optional bytes written to the process's standard input.

    Returns:
        The completed process. The exit status is not checked.
    """
    # Every caller passes a literal argv built from test constants and tmp_path
    # values, never external input; S603 and S607 are ignored for tests/ in
    # pyproject.toml.
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
