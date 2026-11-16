"""Crystallizer CLI: emit | validate | run-check.

``python -m micro_eviction.crystallizer.cli emit [--plan PATH] [--manifest
PATH] [--out-dir PATH]`` emits the package of record plus EMIT-RECEIPT.json
(§2.5). ``validate [--package PATH] [--manifest PATH] [--query-time F]``
runs validate_package and prints the report; exit 0 iff fail == 0 (D-09).
``run-check <check_id> --mode <clean|seeded> [--package PATH]`` dispatches
through the ASSERTIONS registry (D-17). Mode inputs are pinned by D-18:
clean loads the gate's PASS fixture (or the package of record via --package);
seeded loads the gate's FAIL fixture. Exit contract: clean exits 0; seeded
exits non-zero printing the violation id on stdout. Any request to execute
outside the designated sandbox raises SandboxRefused and exits 2 (scoped to
C9 run-check executions; gate commands run at the campaign root, D-19).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from micro_eviction.crystallizer import emit as emit_mod
from micro_eviction.crystallizer import sandbox
from micro_eviction.crystallizer.corpus import CAMPAIGN_ROOT, CorpusError, load_manifest
from micro_eviction.crystallizer.validate import (
    ASSERTIONS,
    FAIL,
    NOT_EXERCISED,
    STALE_DEMOTED,
    validate_package,
)

DEFAULT_QUERY_TIME = 1793664000.0  # wave-1 query_time_epoch (D-10)


def _load_package(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        pkg = json.load(f)
    if not isinstance(pkg, dict):
        raise CorpusError(f"package is not an object: {path}")
    return pkg


def _fixture_package(check_id: str, mode: str) -> dict:
    name = sandbox.FIXTURES.get((check_id, mode))
    if name is None:
        raise CorpusError(
            f"fixture_absent: no {mode} fixture registered for {check_id}")
    path = sandbox.FIXTURES_DIR / name
    if not path.is_file():
        raise CorpusError(f"fixture_absent: {path} missing")
    return _load_package(path)


def run_check(check_id: str, mode: str, package_path=None,
              query_time: float = DEFAULT_QUERY_TIME) -> int:
    """Run one registered check; return the process exit code."""
    fn = ASSERTIONS.get(check_id)
    if fn is None:
        raise CorpusError(f"unknown check id: {check_id}")
    if package_path is not None:
        package = _load_package(Path(package_path))
    else:
        package = _fixture_package(check_id, mode)
    package_sha = hashlib.sha256(
        json.dumps(package, sort_keys=True, ensure_ascii=False).encode(
            "utf-8")).hexdigest()
    ctx = {"manifest": load_manifest(), "query_time": float(query_time),
           "package_sha256": package_sha}
    res = fn(package, ctx)
    state = res.get("state")
    if state == FAIL:
        for v in res.get("violations") or []:
            print(v.get("id", "<missing-id>"))
        return 1
    if state in (NOT_EXERCISED, STALE_DEMOTED, "PASS"):
        return 0
    raise CorpusError(f"check {check_id} returned unknown state {state!r}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m micro_eviction.crystallizer.cli")
    sub = parser.add_subparsers(dest="command")
    em = sub.add_parser("emit")
    em.add_argument("--plan", default=None)
    em.add_argument("--manifest", default=None)
    em.add_argument("--out-dir", default=None)
    va = sub.add_parser("validate")
    va.add_argument("--package", default=None)
    va.add_argument("--manifest", default=None)
    va.add_argument("--query-time", type=float, default=DEFAULT_QUERY_TIME)
    rc = sub.add_parser("run-check")
    rc.add_argument("check_id")
    rc.add_argument("--mode", required=True, choices=list(sandbox.MODES))
    rc.add_argument("--package", default=None)
    rc.add_argument("--query-time", type=float, default=DEFAULT_QUERY_TIME)
    args = parser.parse_args(argv)
    if args.command == "emit":
        try:
            receipt = emit_mod.emit(
                plan_path=Path(args.plan) if args.plan else emit_mod.PLAN_PATH,
                manifest_path=Path(args.manifest) if args.manifest else None,
                out_dir=Path(args.out_dir) if args.out_dir else emit_mod.OUT_DIR,
            )
        except CorpusError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        try:
            package = _load_package(
                Path(args.package) if args.package else emit_mod.PKG_PATH)
            manifest = (load_manifest(Path(args.manifest))
                        if args.manifest else load_manifest())
            report = validate_package(
                package, manifest, query_time=args.query_time)
        except CorpusError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["summary"]["fail"] == 0 else 1
    if args.command != "run-check":
        parser.error(f"unknown command {args.command!r}")
    try:
        sandbox.designated_cwd(Path.cwd())
    except sandbox.SandboxRefused as e:
        print(f"SandboxRefused: {e}", file=sys.stderr)
        return 2
    try:
        return run_check(args.check_id, args.mode, args.package,
                         args.query_time)
    except sandbox.SandboxRefused as e:
        print(f"SandboxRefused: {e}", file=sys.stderr)
        return 2
    except CorpusError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
