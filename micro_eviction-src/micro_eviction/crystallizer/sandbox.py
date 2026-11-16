"""C9a/C9c sandboxed runner (§2.6).

sandbox.run(check_id, package_path, mode) executes
``python -m micro_eviction.crystallizer.cli run-check <check_id> --mode
<clean|seeded>`` with cwd inside .sandbox/crystallizer-runs/<run_id>/, a
scrubbed environment, and a wall-clock timeout. Any request to execute
outside the designated sandbox raises SandboxRefused (CLI exit 2).

Mode inputs (D-18): --mode clean loads the gate's PASS fixture (or the
package of record when package_path is given); --mode seeded loads the
gate's FAIL fixture from tests/crystallizer/fixtures/. Clean must exit 0;
seeded must exit non-zero naming the violation id on stdout.

The run report records command, exit code, stdout/stderr sha256, cwd,
timeout, and the fixed evidence.note sandbox-limit string.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from .corpus import CAMPAIGN_ROOT

SANDBOX_LIMIT_NOTE = (
    "process-level sandbox: cwd containment, environment scrubbing, "
    "timeout; not kernel isolation"
)

MODES = ("clean", "seeded")
DEFAULT_TIMEOUT = 60.0

SANDBOX_RUNS = CAMPAIGN_ROOT / ".sandbox" / "crystallizer-runs"
FIXTURES_DIR = CAMPAIGN_ROOT / "tests" / "crystallizer" / "fixtures"

RUN_CHECK_ARGV0 = (
    "python -m micro_eviction.crystallizer.cli run-check"
)

# D-18 mode inputs, registered per gate. T6 completes the per-gate set and
# asserts the registration is exhaustive over the implemented gates.
FIXTURES: dict[tuple[str, str], str] = {
    ("C1", "clean"): "pass_package.json",
    ("C1", "seeded"): "fail_c1_paraphrase.json",
    ("C2", "clean"): "pass_package.json",
    ("C2", "seeded"): "fail_c2_dangling.json",
    ("C3", "clean"): "pass_package.json",
    ("C3", "seeded"): "fail_c3_missing_residual.json",
    ("C4", "clean"): "pass_package.json",
    ("C4", "seeded"): "fail_c4_mismatch_fresh.json",
    ("C5", "clean"): "pass_c5_nonempty.json",
    ("C5", "seeded"): "fail_c5_rewritten.json",
    ("C6", "clean"): "pass_c6_dag.json",
    ("C6", "seeded"): "fail_c6_cycle.json",
    ("C7", "clean"): "pass_c7_spans.json",
    ("C7", "seeded"): "fail_c7_missing_span.json",
    ("C8", "clean"): "pass_package.json",
    ("C8", "seeded"): "fail_c8_untagged.json",
    ("C9a", "clean"): "pass_package.json",
    ("C9a", "seeded"): "fail_c9a_no_executable.json",
    ("C9c", "clean"): "pass_package.json",
    ("C9c", "seeded"): "fail_c9c_empty_checks.json",
    ("E-A8", "clean"): "pass_package.json",
    ("E-A8", "seeded"): "fail_e_a8_triple_incomplete.json",
    ("E-A9", "clean"): "pass_package.json",
    ("E-A9", "seeded"): "fail_e_a9_bare_residual.json",
}


class SandboxRefused(Exception):
    """Refusal to execute outside the sandbox contract (CLI exit 2)."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def designated_cwd(path: Path) -> Path:
    """Return the resolved path, or raise SandboxRefused if it escapes."""
    try:
        resolved = Path(path).resolve()
        resolved.relative_to(SANDBOX_RUNS.resolve())
    except (OSError, ValueError):
        raise SandboxRefused(
            f"cwd outside designated sandbox {SANDBOX_RUNS}: {path}"
        ) from None
    return resolved


def scrubbed_env() -> dict[str, str]:
    """No inherited credentials; PYTHONPATH limited to the campaign root."""
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": str(CAMPAIGN_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }


def run(check_id: str, package_path=None, mode: str = "clean", *,
        timeout: float = DEFAULT_TIMEOUT, cwd=None, run_id: str | None = None,
        ) -> dict:
    """Run one check in the sandbox and return its run report."""
    if mode not in MODES:
        raise SandboxRefused(f"mode not in {MODES}: {mode!r}")
    if run_id is None:
        run_id = f"{check_id}-{mode}-{uuid.uuid4().hex[:8]}"
    run_dir = designated_cwd(cwd) if cwd is not None else (
        designated_cwd(SANDBOX_RUNS / run_id)
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "micro_eviction.crystallizer.cli",
            "run-check", str(check_id), "--mode", mode]
    if package_path is not None:
        argv += ["--package", str(package_path)]
    command = f"{RUN_CHECK_ARGV0} {check_id} --mode {mode}"
    try:
        proc = subprocess.run(
            argv, cwd=str(run_dir), env=scrubbed_env(), timeout=timeout,
            capture_output=True, text=True,
        )
        exit_code = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        exit_code = 124
        stdout = e.stdout.decode("utf-8", "replace") if e.stdout else ""
        stderr = e.stderr.decode("utf-8", "replace") if e.stderr else ""
    report = {
        "check_id": check_id,
        "mode": mode,
        "command": command,
        "exit": exit_code,
        "stdout_sha256": _sha256(stdout),
        "stderr_sha256": _sha256(stderr),
        "cwd": str(run_dir),
        "timeout": timeout,
        "note": SANDBOX_LIMIT_NOTE,
        "stdout": stdout,
        "stderr": stderr,
    }
    (run_dir / "run-report.json").write_text(
        json.dumps(report, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return report


def report_record(report: dict) -> dict:
    """The recorded run-report fields (no raw output)."""
    keys = ("check_id", "mode", "command", "exit", "stdout_sha256",
            "stderr_sha256", "cwd", "timeout", "note")
    return {k: report[k] for k in keys}


def run_contract_violations(report: dict) -> list[str]:
    """Clean must exit 0; seeded must exit non-zero naming the violation id."""
    mode = report.get("mode")
    exit_code = report.get("exit")
    if mode == "clean":
        if exit_code != 0:
            return [f"clean run exited {exit_code} (must exit 0)"]
        return []
    if mode == "seeded":
        if exit_code == 0:
            return ["seeded run exited 0 (must fire: non-zero + violation id)"]
        if not (report.get("stdout") or "").strip():
            return ["seeded run printed no violation id on stdout"]
        return []
    return [f"run report has unknown mode {mode!r}"]
