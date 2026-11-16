"""W2 contract tests for the crystallizer unit (CHARGE-COMPOSE-NEXT-CYCLE-1).

Real execution evidence, no mocking: each test runs a fresh subprocess pinned
to the campaign root (where ``micro_eviction`` imports resolve) and asserts on
MEASURED exit codes and parsed output. These tests are the polyguard-anchored
test-execution evidence for the crystallizer unit; they deliberately re-run
the cycle's own gates rather than trusting cached state.
"""

import json
import os
import subprocess
import sys

UNIT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMPAIGN_ROOT = os.path.abspath(os.path.join(UNIT_DIR, os.pardir, os.pardir))
PACKAGE_OF_RECORD = os.path.join(
    "micro_eviction", "crystallizer", "out", "pkg-wave1-external-record-v1.json"
)


def _run(args, timeout):
    return subprocess.run(
        args,
        cwd=CAMPAIGN_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_crystallizer_suite_green():
    """The full campaign-root crystallizer suite passes (zero skips/xfails)."""
    r = _run([sys.executable, "-m", "pytest", "-q", "tests/crystallizer"], 600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-500:]
    tail = r.stdout.strip().splitlines()[-1]
    assert " passed" in tail, tail
    assert " skipped" not in tail and " xfailed" not in tail, tail


def test_package_of_record_validates_clean():
    """validate_package on the package of record: exit 0, zero FAIL checks."""
    r = _run(
        [
            sys.executable,
            "-m",
            "micro_eviction.crystallizer.cli",
            "validate",
            "--package",
            PACKAGE_OF_RECORD,
        ],
        300,
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-500:]
    summary = json.loads(r.stdout)["summary"]
    assert summary["fail"] == 0, summary
    assert summary["pass"] >= 8, summary
    # Vacuous gates are NOT_EXERCISED, never PASS-by-default or FAIL.
    assert summary["not_exercised"] == 5, summary
    assert summary["stale_demoted"] == 0, summary
