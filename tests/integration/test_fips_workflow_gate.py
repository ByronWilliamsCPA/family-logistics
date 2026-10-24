"""Behavioral tests for the FIPS compatibility gate in the CI workflow.

The workflow step is extracted from ``fips-compatibility.yml`` and executed
under the shell options GitHub Actions applies to it (``bash -e`` by default,
``bash -eo pipefail`` when the step sets ``shell: bash``), with a stub ``uv``
standing in for the checker. This guards the original defect: the checker's
status was lost behind ``| tee``, so the gate could never fail.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "fips-compatibility.yml"

CLEAN_JSON = '{"summary": {"errors": 0, "warnings": 1, "info": 2}, "issues": []}'
ERROR_JSON = '{"summary": {"errors": 1, "warnings": 0, "info": 0}, "issues": []}'


def _fips_step() -> dict[str, Any]:
    workflow = YAML(typ="safe").load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["fips-check"]["steps"]
    matches = [step for step in steps if step.get("id") == "fips-check"]
    assert len(matches) == 1
    return matches[0]


def _write_stub_uv(bin_dir: Path, *, exit_code: int, json_body: str) -> None:
    """Create a fake ``uv`` that prints text or JSON and exits with a status."""
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/bin/bash\n"
        'if [[ "$*" == *--json* ]]; then\n'
        f"  printf '%s\\n' '{json_body}'\n"
        "else\n"
        "  echo 'checker text output'\n"
        "fi\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)


def _run_step(
    tmp_path: Path, *, exit_code: int, json_body: str
) -> tuple[int, dict[str, str]]:
    """Run the real workflow step and return (shell status, GITHUB_OUTPUT)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub_uv(bin_dir, exit_code=exit_code, json_body=json_body)
    script = tmp_path / "step.sh"
    script.write_text(_fips_step()["run"], encoding="utf-8")
    github_output = tmp_path / "github_output"
    github_output.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(github_output),
        "STRICT_MODE": "false",
    }
    step = _fips_step()
    shell_flags = ["-eo", "pipefail"] if step.get("shell") == "bash" else ["-e"]
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(
        [bash, *shell_flags, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = dict(
        line.split("=", 1)
        for line in github_output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return result.returncode, outputs


def test_step_uses_an_explicit_bash_shell() -> None:
    # Without `shell: bash`, `run` steps get no pipefail and tee masks the checker.
    assert _fips_step()["shell"] == "bash"


def test_checker_failure_is_reported_as_nonzero_exit_code(tmp_path: Path) -> None:
    status, outputs = _run_step(tmp_path, exit_code=1, json_body=ERROR_JSON)

    assert status == 0  # the step records the result; 'Check result' fails the job
    assert outputs["exit_code"] == "1"
    assert outputs["errors"] == "1"


def test_clean_run_reports_zero_and_summary_counts(tmp_path: Path) -> None:
    status, outputs = _run_step(tmp_path, exit_code=0, json_body=CLEAN_JSON)

    assert status == 0
    assert outputs == {
        "errors": "0",
        "warnings": "1",
        "info": "2",
        "exit_code": "0",
    }


def test_unparsable_json_report_fails_closed(tmp_path: Path) -> None:
    # A checker that exits 0 but leaves a broken report must not read as green.
    status, outputs = _run_step(tmp_path, exit_code=0, json_body="not json")

    assert status == 0
    assert outputs["exit_code"] == "1"
    assert "errors" not in outputs
