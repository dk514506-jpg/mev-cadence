#!/usr/bin/env python3
"""engraphis_backfill — G1 backfill writer (CHARGE-ENGRAPHIS-W4-BACKFILL §2 G1).

Ingests one memory per frontmatter-bearing seat-state record into workspace
``seat-state-staging`` via the production seam:

    sys.path[campaign root] -> mev_activation.make_service_factory()
    engraphis_sink.EngraphisSink(ownership=memos_push.resolve_induction_ownership(),
                                 store_path=<db>)
    sink.ingest_record(record, trusted=False)   # ONE sink per record (R15 law)

Laws (charge §1/§4):
  * content = the record's summary body, truncated <=2000 chars; slice text is
    NEVER ingested (slice_path + sha are pointers only).
  * trusted=False always.
  * Idempotent: R15 query-then-skip by record_id (the sink does the lookup).
  * Fail-closed PER RECORD: one bad record skips + logs, never aborts batch.
  * seat-state-staging ONLY (the frontmatter's own pinned target).
  * Live writes require --confirm (orchestrator-only phase G3); --dry-run
    writes zero bytes anywhere.

Metadata provenance carriers: the sink's METADATA_CARRIERS tuple is widened
IN-PROCESS ONLY (additive; the frozen engraphis_sink.py file is untouched) so
slice_path/slice_sha256_12/slice_bytes/writer/seat/checkpoint/tags survive the
sink's metadata filter alongside record_id/capture_sha/bounds/topic_hash.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

CAMPAIGN_ROOT = (
    "~/Desktop/roof/.worktrees/enpipe-w2/"
    "campaigns/bench/micro-eviction"
)
DEFAULT_ROOT = Path("~/Desktop/roof/digital_brain/ops_command/log/seat-state")
DEFAULT_DB = Path("~/.engraphis/memory.db").expanduser()
WORKSPACE = "seat-state-staging"  # pinned by charge §4 (never mev-staging)
MAX_CONTENT_CHARS = 2000
STAGING_TAG = "engraphis-backfill-w4"

# Extra metadata carriers this tool needs through the sink filter (in-process,
# additive; applied once at import of the sink module).
EXTRA_CARRIERS = (
    "slice_path", "slice_sha256_12", "slice_bytes",
    "writer", "seat", "checkpoint", "tags",
)


def _ensure_carriers(sink_mod) -> tuple:
    carriers = sink_mod.METADATA_CARRIERS
    missing = tuple(k for k in EXTRA_CARRIERS if k not in carriers)
    if missing:
        sink_mod.METADATA_CARRIERS = carriers + missing
    return sink_mod.METADATA_CARRIERS


# ── frontmatter parsing (stdlib; records are simple key: value YAML) ─────────

_LIST_RE = re.compile(r"^\[.*\]$")
_INT_RE = re.compile(r"^-?\d+$")


def _parse_scalar(raw: str):
    raw = raw.strip()
    if raw in ("None", "null", "~", ""):
        return None
    if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    if _LIST_RE.match(raw):
        try:
            return json.loads(raw)
        except ValueError:
            pass
    if _INT_RE.match(raw):
        return int(raw)
    return raw


def parse_frontmatter(text: str):
    """Return (fm_dict, body_text). fm is {} when no frontmatter block exists."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, text
    fm = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        fm[key.strip()] = _parse_scalar(raw)
    body = "\n".join(lines[end + 1:]).strip()
    return fm, body


def discover_records(root: Path, seat: str | None = None) -> list[Path]:
    """Enumerate STATE-*.md records (excluding .slice.jsonl) under the writer
    root. Names the enumerated roots when nothing is found (root-enumeration
    law)."""
    if seat is not None:
        roots = [root / seat]
    else:
        roots = sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    out = []
    for r in roots:
        if not r.is_dir():
            continue
        for p in sorted(r.glob("STATE-*.md")):
            if p.name.endswith(".slice.jsonl"):
                continue
            out.append(p)
    return out


def validate_fm(fm: dict) -> tuple[bool, str]:
    """Fail-closed per-record validation of the provenance chain."""
    rid = fm.get("record_id")
    if not rid or not isinstance(rid, str) or not rid.startswith("rec_"):
        return False, f"bad record_id: {fm.get('record_id')!r}"
    csha = fm.get("capture_sha")
    if not csha or not isinstance(csha, str) or len(csha) != 12:
        return False, f"bad capture_sha: {csha!r}"
    if fm.get("engraphis_workspace") != WORKSPACE:
        return False, ("not a seat-state-staging record "
                       f"(engraphis_workspace={fm.get('engraphis_workspace')!r})")
    if fm.get("engraphis_record_id") not in (None, "None"):
        return False, (f"already staged (engraphis_record_id="
                       f"{fm.get('engraphis_record_id')!r})")
    return True, ""


def build_record(fm: dict, body: str) -> dict:
    content = body[:MAX_CONTENT_CHARS]
    record = {
        "record_id": fm["record_id"],
        "session_id": fm.get("session_uuid") or fm.get("record_id"),
        "bounds": fm.get("bounds"),
        "type": "seat-state",
        "topic_hash": fm.get("topic_hash"),
        "capture_sha": fm["capture_sha"],
        "content": content,
        "slice_path": fm.get("slice_path"),
        "slice_sha256_12": fm.get("slice_sha256_12"),
        "slice_bytes": fm.get("slice_bytes"),
        "writer": fm.get("writer"),
        "seat": fm.get("seat"),
        "checkpoint": fm.get("checkpoint"),
        "tags": list(fm.get("tags") or []) + [STAGING_TAG],
    }
    return record


# ── service seam ──────────────────────────────────────────────────────────────

def make_service(db_path: str):
    """Production seam: campaign root on sys.path; make_service_factory()."""
    if CAMPAIGN_ROOT not in sys.path:
        sys.path.insert(0, CAMPAIGN_ROOT)
    import mev_activation
    return mev_activation.make_service_factory({
        "ENGRAPHIS_DB_PATH": str(db_path),
        "ENGRAPHIS_WORKSPACES": "seat-state-staging,mev-staging,reverie-staging",
        "PATH": __import__("os").environ.get("PATH", "/usr/bin"),
    })()


def ingest_one(service, sink_mod, record: dict, ownership: dict,
               store_path, staging_state: dict) -> dict:
    """ONE sink per record (R15 provenance binding law). Returns sink result."""
    sink = sink_mod.EngraphisSink(
        service, workspace=WORKSPACE, capture_sha=record["capture_sha"],
        ownership=ownership, staging_state=staging_state,
        store_path=store_path)
    return sink.ingest_record(record, trusted=False)


# ── batch ─────────────────────────────────────────────────────────────────────

def run(root: Path, db_path: str, *, dry_run: bool = False, limit: int | None = None,
        seat: str | None = None, confirm: bool = False,
        service=None) -> dict:
    """Returns a summary dict. Never raises into the caller for per-record
    faults; only setup faults (bad root, refused live run without --confirm)
    raise Refusal."""
    root = root.expanduser()
    if not root.is_dir():
        raise Refusal(f"record root not found: {root}")

    paths = discover_records(root, seat=seat)
    if limit is not None:
        paths = paths[:limit]

    parsed, skipped = [], []
    for p in paths:
        try:
            fm, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception as exc:  # fail closed per record
            skipped.append({"path": str(p), "reason": f"unreadable: {exc}"})
            continue
        ok, why = validate_fm(fm)
        if not ok:
            skipped.append({"path": str(p), "reason": why})
            continue
        parsed.append((p, fm, body))

    if dry_run or not confirm:
        mode = "dry-run" if dry_run else "no-confirm-refusal"
        if not dry_run and not confirm and parsed:
            raise Refusal(
                f"{len(parsed)} records would be ingested but live writes "
                "require --confirm (orchestrator-only phase G3). "
                "Re-run with --dry-run to inspect, or --confirm to write.")
        ids = [fm["record_id"] for _, fm, _ in parsed]
        return {"mode": mode, "would_ingest": len(ids),
                "first_5_record_ids": ids[:5], "skipped": skipped}

    # ── live path (orchestrator G3 only) ──
    if service is None:
        service = make_service(db_path)
    if CAMPAIGN_ROOT not in sys.path:
        sys.path.insert(0, CAMPAIGN_ROOT)
    import engraphis_sink as sink_mod
    import memos_push
    _ensure_carriers(sink_mod)
    ownership = memos_push.resolve_induction_ownership()

    ingested, dup_skipped, failed = [], [], []
    seen_rids: set[str] = set()
    for p, fm, body in parsed:
        rid = fm["record_id"]
        try:
            if rid in seen_rids:
                dup_skipped.append(rid)
                continue
            record = build_record(fm, body)
            out = ingest_one(service, sink_mod, record, ownership,
                             store_path=db_path, staging_state={"records": 0, "bytes": 0})
            seen_rids.add(rid)
            op = out.get("op")
            if op == "skip":
                dup_skipped.append(rid)
            elif op == "ingest":
                ingested.append(rid)
            else:
                failed.append({"record_id": rid, "reason": f"op={op}"})
        except Exception as exc:  # fail closed per record, never abort batch
            failed.append({"record_id": rid, "reason": f"{type(exc).__name__}: {exc}"})
    return {"mode": "live", "ingested": ingested, "duplicate_skipped": dup_skipped,
            "failed": failed, "skipped": skipped}


class Refusal(RuntimeError):
    """Loud setup-level refusal (fail closed)."""


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DEFAULT_DB), help="Engraphis store path")
    ap.add_argument("--root", default=str(DEFAULT_ROOT),
                    help="seat-state writer root")
    ap.add_argument("--seat", default=None, help="restrict to one seat dir")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="print would-be ingest set (count + first 5 ids), zero writes")
    ap.add_argument("--confirm", action="store_true",
                    help="allow live writes (orchestrator G3 only)")
    args = ap.parse_args(argv)
    t0 = time.time()
    try:
        summary = run(Path(args.root), args.db, dry_run=args.dry_run,
                      limit=args.limit, seat=args.seat, confirm=args.confirm)
    except Refusal as exc:
        print(f"REFUSED: {exc}")
        return 2
    n_skip = len(summary.get("skipped", []))
    if summary["mode"] in ("dry-run", "no-confirm-refusal"):
        print(f"mode={summary['mode']} would_ingest={summary['would_ingest']} "
              f"skipped={n_skip}")
        print("first_5=" + ",".join(summary["first_5_record_ids"]))
    else:
        print(f"mode=live ingested={len(summary['ingested'])} "
              f"dup_skip={len(summary['duplicate_skipped'])} "
              f"failed={len(summary['failed'])} skipped={n_skip} "
              f"({time.time() - t0:.1f}s)")
        for f in summary["failed"][:10]:
            print(f"  FAIL {f['record_id']}: {f['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
