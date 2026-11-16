#!/usr/bin/env python3
"""memos_promote_2_0_34.py — micro-eviction promotion adapter for MemOS 2.0.34.

CHARGE-MEMOS-PROMOTION-ADAPTER-2026-10-30 (builder seat).

Promotes the 18 distinct member records of the 8 promotion transitions in
ledgers/lifecycle-transitions.jsonl into a NEW scratch cube on the MemOS
2.0.34 engine (http://127.0.0.1:7411) via the 2.0.34 /product API.

Engine contract (verified against memos-2.0.34 source and a live probe):

* POST /product/add with APIADDRequest. `memory_content` is deprecated but
  the model validator converts it into a {"type": "text", "text": ...}
  message part, which routes to TextContentParser.
* async_mode="sync" + mode="fast" is REQUIRED for byte fidelity: the fast
  reader stores content.strip() verbatim with no LLM rewrite. mode="fine"
  (the default for sync) LLM-extracts and rewrites the text, which would
  break the sha256_12 read-back gate.
* The fast reader pops `custom_tags` out of the persisted info, so tag
  values (topic_hash, to-state, type, session ids) are ALSO mirrored into
  the `info` dict, which persists. AddHandler drops info keys that collide
  with TextualMemoryItem fields ("type", "source", ...), hence record_type /
  source_type naming.
* The Neo4j layer flattens metadata.info into top-level node properties, so
  read-back via POST /product/get_memory exposes metadata.record_id etc.
* The engine performs no content dedup on add (MERGE on fresh UUIDs);
  idempotency is enforced by this adapter via a pre-write record_id scan of
  the target cube.

Modes:
  dry-run (default)  print the 18-record plan with per-record content
                     sha256_12 and every request body; send nothing.
  live               requires --cube and --confirm-live; without either the
                     adapter refuses to write. --cube must name the scratch
                     cube in the charged namespace mev-promo-scratch-<UTCYYYYMMDDHHMM>:
                     if the cube already exists in the engine it is targeted
                     (idempotent re-run), otherwise it is created via
                     /product/create_cube (charge A2). The live cube
                     "mev-promotions" is refused as out of scope (guarded)
                     EXCEPT under the narrow CHARGE-MEMOS-PROMOTION-LIVE-2026-10-30
                     amendment: permitted only when ALL hold —
                     --live-promotions-approved 2026-10-30 (exact string), a plan
                     whose record_ids equal the 18 distinct ledger member ids,
                     and --confirm-live.

A4 fidelity: if any record's text cannot be read from disk, the adapter
HALTS and reports the id list. It never fabricates or paraphrases content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CAMPAIGN = Path.home() / "Desktop" / "roof" / "campaigns" / "bench" / "micro-eviction"
LEDGER_PATH = CAMPAIGN / "ledgers" / "lifecycle-transitions.jsonl"
LEDGERS_DIR = CAMPAIGN / "ledgers"
LAB_DB = Path.home() / "scratch" / "mem-migration" / "lab" / "memos-db" / "memos.db"
GATE_SCRIPT = Path.home() / "scratch" / "mem-migration" / "memos-2.0.34" / "assert-engine-version.sh"

EXPECTED_LEDGER_SHA12 = "3f581aa6b865"
EXPECTED_RECORDS = 18
EXPECTED_TOPICS = 4
LIVE_CUBE_FORBIDDEN = "mev-promotions"
LIVE_APPROVAL_STAMP = "2026-10-30"
LIVE_CHARGE = "CHARGE-MEMOS-PROMOTION-LIVE-2026-10-30"

DEFAULT_ENGINE = "http://127.0.0.1:7411"
DEFAULT_USER_ID = "mev-promotion"
DEFAULT_SOURCE_TAG = "micro-eviction-ledger"
CUBE_OWNER = "root"


class ChargeHalt(Exception):
    pass


def sha256_12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def http_post(base: str, path: str, body: dict, timeout: float = 120.0):
    url = base.rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {"raw": "unreadable"}
        return e.code, payload


def run_gate() -> dict:
    proc = subprocess.run(
        ["bash", str(GATE_SCRIPT)], capture_output=True, text=True, timeout=60
    )
    output = (proc.stdout or "").strip()
    ok = proc.returncode == 0 and "2.0.34 OK" in output
    return {"script": str(GATE_SCRIPT), "returncode": proc.returncode,
            "output": output, "stderr": (proc.stderr or "").strip(), "ok": ok}


def load_ledger() -> tuple[list[dict], str]:
    raw = LEDGER_PATH.read_bytes()
    digest12 = hashlib.sha256(raw).hexdigest()[:12]
    lines = [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    return lines, digest12


def build_plan(transitions: list[dict]) -> list[dict]:
    promoted = [t for t in transitions if t.get("kind") == "promotion"]
    records: dict[str, dict] = {}
    order: list[str] = []
    for idx, t in enumerate(promoted, start=1):
        line_no = idx
        for rid in t["members"]:
            if rid not in records:
                records[rid] = {
                    "record_id": rid,
                    "topic_hash": t["topic_hash"],
                    "record_type": t["type"],
                    "kind": t["kind"],
                    "to_states": [],
                    "ledger_lines": [],
                    "sessions": set(),
                    "counts": {},
                    "evidence": None,
                    "via": None,
                    "artifact_sha256_12": None,
                }
                order.append(rid)
            rec = records[rid]
            if rec["topic_hash"] != t["topic_hash"]:
                raise ChargeHalt(
                    f"A1 violation: {rid} appears under two topic_hashes "
                    f"({rec['topic_hash']}, {t['topic_hash']})"
                )
            if t["to"] not in rec["to_states"]:
                rec["to_states"].append(t["to"])
            rec["ledger_lines"].append(line_no)
            rec["sessions"].update(t.get("sessions", []))
            if t.get("evidence") is not None:
                rec["evidence"] = t["evidence"]
            if t.get("via") is not None:
                rec["via"] = t["via"]
            rec["counts"] = dict(t.get("counts", {}))
    calcified = {}
    calc_path = CAMPAIGN / "calcified.jsonl"
    if calc_path.exists():
        for l in calc_path.read_text(encoding="utf-8").splitlines():
            if l.strip():
                d = json.loads(l)
                calcified[d["topic_hash"]] = d
    for rid in order:
        rec = records[rid]
        rec["sessions"] = sorted(rec["sessions"])
        rec["to_terminal"] = rec["to_states"][-1]
        c = calcified.get(rec["topic_hash"])
        if c:
            rec["artifact_sha256_12"] = (c.get("provenance") or {}).get("artifact_sha256_12")
    return [records[r] for r in order]


def scan_capture_products() -> tuple[dict[str, dict], dict[str, list]]:
    contents: dict[str, dict] = {}
    corroboration: dict[str, list] = {}
    ledger_files = sorted(
        p for p in LEDGERS_DIR.glob("*.jsonl")
        if p.name != "lifecycle-transitions.jsonl" and not p.name.endswith(".spine.jsonl")
    )
    for path in ledger_files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            d = json.loads(line)
            rid = d.get("record_id")
            if rid and d.get("content") is not None:
                contents[rid] = {
                    "content": d["content"],
                    "source": {
                        "file": str(path),
                        "line": lineno,
                        "session_id": d.get("session_id"),
                        "turn_id": d.get("turn_id"),
                        "confidence": d.get("confidence"),
                        "record_type": d.get("type"),
                        "provenance": d.get("provenance"),
                    },
                }
    for path in sorted(CAMPAIGN.rglob("reports/manifests/*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in d.get("refused", []) + d.get("items", []):
            for rid in item.get("record_ids", []) or []:
                corroboration.setdefault(rid, []).append(str(path))
    calc_path = CAMPAIGN / "calcified.jsonl"
    if calc_path.exists():
        for line in calc_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                for rid in d.get("members", []):
                    corroboration.setdefault(rid, []).append(str(calc_path))
    return contents, corroboration


def scan_lab_db() -> dict[str, list]:
    found: dict[str, list] = {}
    if not LAB_DB.exists():
        return found
    con = sqlite3.connect(f"file:{LAB_DB}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT id, metadata FROM nodes")
        for nid, meta in cur.fetchall():
            if not meta:
                continue
            try:
                d = json.loads(meta) if isinstance(meta, str) else meta
            except Exception:
                continue
            for rid in d.get("record_ids", []) or []:
                found.setdefault(rid, []).append(nid)
    finally:
        con.close()
    return found


def attach_content(plan: list[dict]) -> list[str]:
    contents, corroboration = scan_capture_products()
    db_refs = scan_lab_db()
    missing = []
    for rec in plan:
        rid = rec["record_id"]
        hit = contents.get(rid)
        if hit is None:
            missing.append(rid)
            rec["content"] = None
            rec["content_sha256_12"] = None
            rec["content_source"] = None
            continue
        text = hit["content"]
        if text != text.strip():
            raise ChargeHalt(
                f"A4 fidelity halt: {rid} text has leading/trailing whitespace; "
                f"the 2.0.34 fast parser strips stored text, so byte identity "
                f"is impossible. Refusing to write a mutated record."
            )
        rec["content"] = text
        rec["content_sha256_12"] = sha256_12(text)
        rec["content_source"] = hit["source"]
        rec["corroboration"] = {
            "capture_products": corroboration.get(rid, []),
            "lab_memos_db_nodes": db_refs.get(rid, []),
        }
    return missing


def cube_exists_in_engine(cube_id: str, engine: str = DEFAULT_ENGINE) -> bool:
    status, body = http_post(engine, "/product/exist_mem_cube_id", {"mem_cube_id": cube_id})
    if status != 200:
        return False
    return bool((body.get("data") or {}).get(cube_id))


def export_cube_nodes(base: str, cube_id: str) -> list[dict]:
    status, body = http_post(base, "/product/get_memory", {
        "mem_cube_id": cube_id,
        "user_id": DEFAULT_USER_ID,
        "include_preference": False,
        "include_tool_memory": False,
        "include_skill_memory": False,
        "page": None,
        "page_size": None,
    })
    if status != 200:
        raise ChargeHalt(f"get_memory failed for cube {cube_id}: {status} {body}")
    nodes = []
    for entry in (body.get("data") or {}).get("text_mem", []):
        nodes.extend(entry.get("memories", []))
    return nodes


def nodes_by_record_id(nodes: list[dict]) -> dict[str, dict]:
    out = {}
    for n in nodes:
        rid = (n.get("metadata") or {}).get("record_id")
        if rid:
            out.setdefault(rid, []).append(n)
    return out


def build_add_body(cube_id: str, rec: dict) -> dict:
    info = {
        "record_id": rec["record_id"],
        "ledger_line": rec["ledger_lines"][0],
        "ledger_lines": rec["ledger_lines"],
        "sha256_12": rec["content_sha256_12"],
        "topic_hash": rec["topic_hash"],
        "to": rec["to_terminal"],
        "to_states": rec["to_states"],
        "record_type": rec["record_type"],
        "kind": rec["kind"],
        "sessions": rec["sessions"],
        "counts": rec["counts"],
        "source_type": DEFAULT_SOURCE_TAG,
        "origin_session_id": rec["content_source"]["session_id"],
        "origin_turn_id": rec["content_source"]["turn_id"],
        "origin_confidence": rec["content_source"]["confidence"],
        "content_source_file": rec["content_source"]["file"],
        "content_source_line": rec["content_source"]["line"],
        "adapter": "memos_promote_2_0_34.py",
        "charge": "CHARGE-MEMOS-PROMOTION-ADAPTER-2026-10-30",
    }
    if rec["evidence"] is not None:
        info["evidence"] = rec["evidence"]
    if rec["via"] is not None:
        info["via"] = rec["via"]
    if rec["artifact_sha256_12"]:
        info["artifact_sha256_12"] = rec["artifact_sha256_12"]
    return {
        "user_id": DEFAULT_USER_ID,
        "session_id": rec["content_source"]["session_id"] or "default_session",
        "mem_cube_id": cube_id,
        "writable_cube_ids": [cube_id],
        "memory_content": rec["content"],
        "async_mode": "sync",
        "mode": "fast",
        "custom_tags": [rec["topic_hash"], rec["to_terminal"], rec["record_type"], *rec["sessions"]],
        "info": info,
        "source": DEFAULT_SOURCE_TAG,
    }


def print_plan(plan: list[dict]) -> None:
    print(f"{'#':>2} {'record_id':<22} {'topic_hash':<18} {'type':<9} {'to':<10} "
          f"{'lines':<10} {'sessions':<42} {'sha256_12'}")
    for i, rec in enumerate(plan, start=1):
        print(f"{i:>2} {rec['record_id']:<22} {rec['topic_hash']:<18} "
              f"{rec['record_type']:<9} {rec['to_terminal']:<10} "
              f"{str(rec['ledger_lines']):<10} {','.join(rec['sessions']):<42} "
              f"{rec['content_sha256_12']}")
    print()
    for i, rec in enumerate(plan, start=1):
        src = rec["content_source"]
        print(f"[{i:02d}] {rec['record_id']}  sha256_12={rec['content_sha256_12']}")
        print(f"     topic={rec['topic_hash']} type={rec['record_type']} "
              f"to_states={rec['to_states']} terminal_to={rec['to_terminal']}")
        print(f"     ledger_lines={rec['ledger_lines']} sessions={rec['sessions']}")
        print(f"     content_source={src['file']}:{src['line']} "
              f"(session={src['session_id']} turn={src['turn_id']})")
        print(f"     content={rec['content']!r}")
        if rec.get("evidence"):
            print(f"     evidence={rec['evidence']} via={rec['via']} "
                  f"artifact_sha256_12={rec.get('artifact_sha256_12')}")
        print()


def dry_run(plan: list[dict], stamp: str, cube_id: str | None = None) -> int:
    cube_id = cube_id or f"mev-promo-scratch-{stamp}"
    print("=== DRY RUN — no requests sent ===")
    print(f"ledger={LEDGER_PATH}")
    print(f"planned records={len(plan)} (charge A1 expects {EXPECTED_RECORDS})")
    print(f"target cube (would create if absent)={cube_id}")
    print()
    print_plan(plan)
    print("=== request bodies that WOULD be sent ===")
    print("--- POST /product/create_cube")
    print(json.dumps({"cube_name": cube_id, "owner_id": CUBE_OWNER, "cube_id": cube_id}, indent=2))
    for i, rec in enumerate(plan, start=1):
        print(f"--- POST /product/add  [{i}/{len(plan)}] {rec['record_id']}")
        print(json.dumps(build_add_body(cube_id, rec), indent=2, ensure_ascii=False))
    print(f"=== DRY RUN COMPLETE: {len(plan)} records planned, 0 sent ===")
    return 0


def live(plan: list[dict], cube_id: str, stamp: str, receipt_dir: Path,
         engine: str, live_amendment: dict | None = None) -> int:
    gate = run_gate()
    print(f"[gate] {gate['output']}")
    if not gate["ok"]:
        raise ChargeHalt(f"engine version gate failed: {gate}")

    if live_amendment is None and not re.fullmatch(r"mev-promo-scratch-\d{12}", cube_id):
        raise ChargeHalt(
            f"--cube {cube_id!r} is outside the charged scratch namespace "
            f"mev-promo-scratch-<UTCYYYYMMDDHHMM>; refusing"
        )
    if cube_exists_in_engine(cube_id, engine):
        created = False
        print(f"[cube] targeting existing cube {cube_id}")
    else:
        status, body = http_post(engine, "/product/create_cube", {
            "cube_name": cube_id, "owner_id": CUBE_OWNER, "cube_id": cube_id,
        })
        if status != 200 or body.get("code") != 200:
            raise ChargeHalt(f"create_cube failed: {status} {body}")
        created = True
        print(f"[cube] created {cube_id} (owner={CUBE_OWNER})")

    ns = re.fullmatch(r"mev-promo-scratch-(\d{12})", cube_id)
    if created and ns:
        receipt_stamp = ns.group(1)
    elif ns:
        receipt_stamp = f"{ns.group(1)}-rerun{stamp}"
    else:
        receipt_stamp = stamp

    existing_nodes = export_cube_nodes(engine, cube_id)
    existing_ids = set(nodes_by_record_id(existing_nodes).keys())
    preexisting = [r["record_id"] for r in plan if r["record_id"] in existing_ids]

    results = []
    written = skipped = 0
    for i, rec in enumerate(plan, start=1):
        rid = rec["record_id"]
        if rid in existing_ids:
            skipped += 1
            results.append({"record_id": rid, "write": {"status": "skipped_duplicate"}})
            print(f"[write {i:02d}/{len(plan)}] SKIP (already in cube): {rid}")
            continue
        body = build_add_body(cube_id, rec)
        status, resp = http_post(engine, "/product/add", body)
        memory_id = None
        ok = status == 200 and resp.get("code") == 200
        if ok and resp.get("data"):
            memory_id = resp["data"][0].get("memory_id")
        if not ok:
            raise ChargeHalt(f"add failed for {rid}: {status} {json.dumps(resp)[:500]}")
        written += 1
        results.append({
            "record_id": rid,
            "write": {"status": "written", "http_status": status, "memory_id": memory_id},
        })
        print(f"[write {i:02d}/{len(plan)}] WROTE {rid} -> memory_id={memory_id}")
        time.sleep(0.3)

    time.sleep(1.0)
    final_nodes = export_cube_nodes(engine, cube_id)
    by_rid = nodes_by_record_id(final_nodes)

    mismatches = []
    for i, rec in enumerate(plan, start=1):
        rid = rec["record_id"]
        entry = next((r for r in results if r["record_id"] == rid), None)
        nodes = by_rid.get(rid, [])
        if not nodes:
            entry["readback"] = {"status": "missing"}
            mismatches.append(rid)
            continue
        node = nodes[0]
        stored = node.get("memory")
        stored_sha = sha256_12(stored or "")
        text_match = stored == rec["content"]
        sha_match = stored_sha == rec["content_sha256_12"]
        md = node.get("metadata") or {}
        meta_ok = (
            md.get("record_id") == rid
            and md.get("topic_hash") == rec["topic_hash"]
            and md.get("to") == rec["to_terminal"]
            and md.get("record_type") == rec["record_type"]
            and md.get("sessions") == rec["sessions"]
            and md.get("sha256_12") == rec["content_sha256_12"]
        )
        if rec["evidence"]:
            meta_ok = meta_ok and md.get("evidence") == rec["evidence"] and md.get("via") == rec["via"]
        status = "ok" if (text_match and sha_match and meta_ok) else "mismatch"
        if status != "ok":
            mismatches.append(rid)
        entry["readback"] = {
            "status": status,
            "memory_id": node.get("id"),
            "text_match": text_match,
            "stored_sha256_12": stored_sha,
            "source_sha256_12": rec["content_sha256_12"],
            "metadata_check": meta_ok,
            "stored_memory": stored,
        }
        print(f"[readback {i:02d}/{len(plan)}] {rid} {status} "
              f"(text_match={text_match} sha_match={sha_match} meta_ok={meta_ok})")

    receipt = {
        "charge": "CHARGE-MEMOS-PROMOTION-ADAPTER-2026-10-30",
        "adapter": "memos_promote_2_0_34.py",
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "live",
        "engine": {
            "url": engine,
            "version_gate": gate,
        },
        "ledger": {
            "path": str(LEDGER_PATH),
            "sha256_12": sha256_12(LEDGER_PATH.read_text(encoding="utf-8")),
            "expected_sha256_12": EXPECTED_LEDGER_SHA12,
        },
        "cube": {"cube_id": cube_id, "created_this_run": created, "owner_id": CUBE_OWNER},
        "user_id": DEFAULT_USER_ID,
        "source_tag": DEFAULT_SOURCE_TAG,
        "counts": {
            "planned": len(plan),
            "written": written,
            "skipped_duplicate": skipped,
            "preexisting_record_ids": preexisting,
            "readback_ok": len(plan) - len(mismatches),
            "readback_mismatch": len(mismatches),
            "missing_content": 0,
        },
        "live_cube_written": cube_id == LIVE_CUBE_FORBIDDEN,
        "live_cube_name_guard": LIVE_CUBE_FORBIDDEN,
        "live_amendment": live_amendment,
        "records": [
            {
                "record_id": rec["record_id"],
                "topic_hash": rec["topic_hash"],
                "record_type": rec["record_type"],
                "to_terminal": rec["to_terminal"],
                "to_states": rec["to_states"],
                "ledger_lines": rec["ledger_lines"],
                "sessions": rec["sessions"],
                "counts": rec["counts"],
                "evidence": rec.get("evidence"),
                "via": rec.get("via"),
                "artifact_sha256_12": rec.get("artifact_sha256_12"),
                "content_sha256_12": rec["content_sha256_12"],
                "content_source": rec["content_source"],
                "corroboration": rec.get("corroboration", {}),
                "result": next(
                    (r for r in results if r["record_id"] == rec["record_id"]), None
                ),
            }
            for rec in plan
        ],
    }
    receipt_dir.mkdir(parents=True, exist_ok=True)
    if live_amendment:
        receipt_path = receipt_dir / f"memos-promotion-live-{live_amendment['approved_stamp']}.json"
    else:
        receipt_path = receipt_dir / f"memos-promotion-scratch-{receipt_stamp}.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print(f"=== LIVE RUN COMPLETE ===")
    print(f"cube={cube_id} written={written} skipped={skipped} "
          f"readback_ok={len(plan) - len(mismatches)} mismatch={len(mismatches)}")
    if mismatches:
        print(f"MISMATCHES: {mismatches}")
    print(f"receipt={receipt_path}")
    return 0 if not mismatches else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", default=DEFAULT_ENGINE)
    ap.add_argument("--cube", default=None,
                    help="REQUIRED in live mode: scratch cube id in the charged "
                         "namespace mev-promo-scratch-<UTCYYYYMMDDHHMM>; created via "
                         "/product/create_cube if absent, targeted idempotently if present")
    ap.add_argument("--confirm-live", action="store_true",
                    help="required for live mode; without it the adapter refuses to write")
    ap.add_argument("--live-promotions-approved", default=None,
                    help=f"narrow guard amendment ({LIVE_CHARGE}): permits --cube "
                         f"{LIVE_CUBE_FORBIDDEN} ONLY when this is exactly "
                         f"{LIVE_APPROVAL_STAMP!r}, the plan's record_ids equal the 18 "
                         f"distinct ledger member ids, and --confirm-live is also passed")
    ap.add_argument("--receipt-dir", default=str(CAMPAIGN / "reviews"))
    args = ap.parse_args(argv)

    transitions, digest12 = load_ledger()
    if digest12 != EXPECTED_LEDGER_SHA12:
        raise ChargeHalt(
            f"ledger sha256_12 {digest12} != charged {EXPECTED_LEDGER_SHA12}; refusing"
        )
    if len(transitions) != 8:
        raise ChargeHalt(f"ledger has {len(transitions)} lines, expected 8")

    plan = build_plan(transitions)
    topics = {r["topic_hash"] for r in plan}
    if len(plan) != EXPECTED_RECORDS or len(topics) != EXPECTED_TOPICS:
        raise ChargeHalt(
            f"A1 violation: plan has {len(plan)} records across {len(topics)} topics; "
            f"expected {EXPECTED_RECORDS}/{EXPECTED_TOPICS}"
        )
    missing = attach_content(plan)
    if missing:
        print(f"A4 HALT — content missing on disk for {len(missing)} record(s):")
        for rid in missing:
            print(f"  {rid}")
        print("No writes performed. Do NOT fabricate content; re-charge with sources.")
        return 3

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    receipt_dir = Path(args.receipt_dir)

    if args.cube == LIVE_CUBE_FORBIDDEN:
        # CHARGE-MEMOS-PROMOTION-LIVE-2026-10-30 narrow guard amendment:
        # mev-promotions is permitted ONLY when ALL hold:
        #   a. --live-promotions-approved 2026-10-30 (exact string)
        #   b. plan record_ids == the 18 distinct ledger member ids
        #      (sha256_12 of the sorted id list is recorded in the receipt;
        #       abort if not equal)
        #   c. --confirm-live (enforced by the live branch below; without it
        #      the adapter dry-runs the plan and sends nothing)
        # CHARGE-MEMOS-PROMOTION-LIVE-FIX-2026-10-30 (F1): this check runs
        # BEFORE the dry-run branch, so conditions a+b are enforced in
        # dry-run mode too.
        member_ids = sorted({rid for t in transitions if t.get("kind") == "promotion"
                             for rid in t.get("members", [])})
        plan_ids = sorted(r["record_id"] for r in plan)
        ids_sha12 = sha256_12(json.dumps(plan_ids, separators=(",", ":")))
        unmet = []
        if args.live_promotions_approved != LIVE_APPROVAL_STAMP:
            unmet.append(f"--live-promotions-approved {LIVE_APPROVAL_STAMP!r} (exact) "
                         f"required, got {args.live_promotions_approved!r}")
        if plan_ids != member_ids:
            unmet.append(f"plan record_ids != 18 distinct ledger member ids "
                         f"(plan={len(plan_ids)}, ledger_members={len(member_ids)})")
        if unmet:
            raise ChargeHalt(
                f"refusing cube {LIVE_CUBE_FORBIDDEN}: amendment conditions unmet: "
                + "; ".join(unmet)
            )
        print(f"[amendment] {LIVE_CHARGE}: exception granted for {LIVE_CUBE_FORBIDDEN} "
              f"(approved={LIVE_APPROVAL_STAMP} records={len(plan_ids)} "
              f"sorted_ids_sha256_12={ids_sha12})")
        if not args.confirm_live:
            return dry_run(plan, stamp, args.cube)
        return live(plan, args.cube, stamp, receipt_dir, args.engine,
                    live_amendment={
                        "charge": LIVE_CHARGE,
                        "approved_stamp": args.live_promotions_approved,
                        "member_ids": plan_ids,
                        "member_ids_count": len(plan_ids),
                        "member_ids_sha256_12": ids_sha12,
                        "member_ids_sha256_12_input":
                            "json.dumps(sorted_ids, separators=(',',':'))",
                    })

    if not args.confirm_live:
        return dry_run(plan, stamp, args.cube)

    if not args.cube:
        print("[refuse] live mode requires --cube <scratch cube id> and --confirm-live;",
              "without --cube no write is attempted.", file=sys.stderr)
        return 2
    return live(plan, args.cube, stamp, receipt_dir, args.engine)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ChargeHalt as e:
        print(f"CHARGE HALT: {e}", file=sys.stderr)
        sys.exit(4)
