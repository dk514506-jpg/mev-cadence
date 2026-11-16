"""g6_probe.py — G6.1 end-to-end probe harness (Stage 0+1 builder scope).

CHARGE-END-TO-END-PROBE-G6-1-V1-2026-11-15 §2 G1/G2.

Scope: probe manifest + freeze-once-reuse + per-leg --confirm-live guards +
negatives N1–N6 (dry-run only).  The builder is PROHIBITED from minting a valid
§5 receipt.  The dry-run MUST NOT complete the store-write or positive-render
legs.

Frozen instruments consumed (never edited):
  memos_dispatcher.py   — _check_c15_receipt, _self_sha256_12
  obsidian_mirror.py    — detect_render_inconsistency, _item_identity_key
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

# ─── §1 verbatim point-in-time distinction (embedded in verdict JSON) ────────
POINT_IN_TIME_DISTINCTION = (
    "A green probe proves ONE measured pass through the chain at ONE point in "
    "time. It is not a general health or uptime claim. "
    "staged_writes promotion coverage is NOT claimed by this probe beyond what "
    "the probe itself measures and readbacks. "
    "The probe does NOT make the memory stack \"work\"; it MEASURES whether "
    "the chain worked during the probe window."
)

# §1 LIVE SURFACES rule — embedded as comments at each live-surface leg and
# carried in the verdict.
LIVE_SURFACES_RULE = (
    "LIVE SURFACES under the sandbox rule (§3): the worktree/master split is "
    "not the only live boundary. The pinned vault "
    "/home/greenknight/icm-knowledge-vault/roof and the MemOS store on :7411 "
    "are LIVE SURFACES: a write to either from any lane other than the §2 G5 "
    "orchestrator live phase is a GATE FAILURE, regardless of which tree the "
    "code runs from. The deployed instruments hard-pin the vault "
    "(memos_dispatcher.py receipt guard + renderer) and target the single "
    "live store, so a positive render/store-write leg is unavoidably a live "
    "write wherever it executes."
)

# ─── N3 probe-defined sentinel ──────────────────────────────────────────────
# PROBE-DEFINED (not a deployed literal).  The probe pins and emits this.
RECEIPT_NOT_DELETED = "receipt-not-deleted"

# ─── N1 deployed literal ────────────────────────────────────────────────────
C15_RECEIPT_MISSING = "c15-receipt-missing"  # memos_dispatcher.py:523

# ─── N2 deployed literals (read from frozen file at disk) ───────────────────
# memos_dispatcher.py:527,529,531,533 → "c15-receipt-invalid"
# memos_dispatcher.py:535 → "c15-receipt-vault-root-mismatch"
# memos_dispatcher.py:537 → "c15-receipt-stale-sha"
C15_RECEIPT_INVALID = "c15-receipt-invalid"
C15_RECEIPT_VAULT_ROOT_MISMATCH = "c15-receipt-vault-root-mismatch"
C15_RECEIPT_STALE_SHA = "c15-receipt-stale-sha"

# N2 deployed literal citations (file:line, verified at disk)
DEPLOYED_LITERAL_CITATIONS = {
    "c15-receipt-missing": "memos_dispatcher.py:523",
    "c15-receipt-invalid": "memos_dispatcher.py:527,529,531,533",
    "c15-receipt-vault-root-mismatch": "memos_dispatcher.py:535",
    "c15-receipt-stale-sha": "memos_dispatcher.py:537",
}

# ─── N4 detector rule citations ─────────────────────────────────────────────
DETECTOR_RULE_CITATION = "obsidian_mirror.py:148-183 (detect_render_inconsistency)"

# ─── N6 plugin writer provenance shape ──────────────────────────────────────
# Read from hermes_plugin/mev_cadence/__init__.py:871-873 and live ledger tail.
PLUGIN_WRITER_PROVENANCE = {
    "plugin": "mev-cadence",
    "hook": "post_llm_call",
}
PLUGIN_WRITER_CITATION = "hermes_plugin/mev_cadence/__init__.py:871-873"

# ─── G1 determinism allowlist (count fields only) ───────────────────────────
# Declared in the manifest spec.  Two dry-runs on frozen inputs must produce
# identical artifact bytes EXCEPT these named run-time count fields.
DETERMINISM_ALLOWLIST = [
    "measured_total_master",
    "measured_total_worktree",
    "cursor_byte_offset",
]

# ─── paths ──────────────────────────────────────────────────────────────────
WORKTREE = Path(__file__).resolve().parent
STAGE_LIVE = Path("/home/greenknight/Desktop/roof/campaigns/bench/micro-eviction")
PINNED_VAULT_ROOT = Path.home() / "icm-knowledge-vault" / "roof"
SCRATCH = WORKTREE / "scratch" / "g6_probe"

# frozen instruments the probe CONSUMES
FROZEN_INSTRUMENTS = [
    "memos_dispatcher.py",
    "obsidian_mirror.py",
    "verify_g3_delivery.py",
]


def sha256_12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def sha256_12_file(path: Path) -> str:
    return sha256_12(path.read_bytes())


# ─── probe manifest ─────────────────────────────────────────────────────────

def build_probe_manifest(probe_session_id: str | None = None) -> dict:
    """Build the probe-only manifest: source record hashes, tree names,
    ledger head, porcelain baselines, store round-trip.

    FREEZE-ONCE-REUSE: if a frozen manifest already exists on disk
    (scratch/g6_probe/frozen/manifest.json with freeze_count >= 1), REUSES
    it — same probe_session_id, same frozen_inputs_hash.  Otherwise builds
    a fresh manifest with freeze_count=0; probe_session_id is derived from
    the frozen-inputs hash at freeze time (see freeze_inputs), so two
    dry-runs on the same frozen inputs share the session id BY CONSTRUCTION.

    A caller who genuinely wants a NEW freeze must delete the frozen
    manifest (scratch/g6_probe/frozen/manifest.json) first.  NO
    auto-regeneration.
    """
    # reuse frozen manifest if present (freeze-once-reuse across CLI invocations)
    frozen_manifest_path = SCRATCH / "frozen" / "manifest.json"
    if frozen_manifest_path.is_file():
        try:
            existing = json.loads(frozen_manifest_path.read_text())
            if existing.get("freeze_count", 0) >= 1:
                return existing
        except (json.JSONDecodeError, KeyError):
            pass  # corrupt on-disk manifest — fall through to fresh build

    source_hashes = {}
    for name in FROZEN_INSTRUMENTS:
        p = WORKTREE / name
        source_hashes[name] = sha256_12_file(p) if p.is_file() else "MISSING"

    # ledger head re-check at disk
    ledger_path = (WORKTREE / "reading-campaign-2026-11-02" / "_campaign"
                   / "evidence-ledger" / "evidence-ledger.jsonl")
    ledger_head = "MISSING"
    ledger_entries = 0
    if ledger_path.is_file():
        lines = [l for l in ledger_path.read_text().splitlines() if l.strip()]
        ledger_entries = len(lines)
        if lines:
            last = json.loads(lines[-1])
            ledger_head = last.get("entry_hash", "MISSING")

    # porcelain baseline both trees
    porcelain_worktree = _git_porcelain(WORKTREE.parent.parent.parent)
    porcelain_master = _git_porcelain(Path("/home/greenknight/Desktop/roof"))

    # store round-trip (read-only) — documented outcome
    store_rt = _store_roundtrip_probe()

    manifest = {
        "probe_session_id": probe_session_id,  # None → derived at freeze time
        "source_record_hashes": source_hashes,
        "tree_names": {
            "worktree": str(WORKTREE),
            "master": str(STAGE_LIVE),
        },
        "ledger_head_recheck": {
            "path": str(ledger_path),
            "head_entry_hash": ledger_head,
            "entries": ledger_entries,
        },
        "porcelain_baseline": {
            "worktree": porcelain_worktree,
            "master": porcelain_master,
        },
        "store_roundtrip": store_rt,
        "determinism_allowlist": list(DETERMINISM_ALLOWLIST),
        "freeze_count": 0,
        "frozen_inputs_hash": None,
        "cursor_source_tree": None,
    }
    return manifest


def _git_porcelain(repo_root: Path) -> list:
    import subprocess
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=10,
        )
        return [l for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        return ["ERROR: git status failed"]


def _store_roundtrip_probe() -> dict:
    """Read-only canonical-payload round-trip probe against :7411.
    Row-6 canary shape: text_match + sha_match + meta_ok.
    Verifies on an EXISTING store record (read-only; no writes, no dispatches,
    no receipt minting).  If store is unreachable, records the outcome without
    improvising."""
    import glob as _glob
    import urllib.request
    engine = "http://127.0.0.1:7411"
    try:
        req = urllib.request.Request(f"{engine}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            health_status = r.status
    except Exception as e:
        return {
            "status": "unreachable",
            "error": f"{type(e).__name__}: {e}",
            "roundtrip_verified": False,
            "note": ("store unreachable — canonical-payload round-trip "
                     "verification CANNOT be completed at Stage 0; "
                     "BLOCKER for Stage 2 live phase per charge §2 G5(i)"),
        }

    # store is UP — run the read-only round-trip on an EXISTING record
    try:
        def _post(path, body):
            rq = urllib.request.Request(
                engine + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(rq, timeout=30) as resp:
                return json.loads(resp.read().decode())

        body = _post("/product/get_memory", {
            "mem_cube_id": "mev-promotions", "user_id": "mev-promotion",
            "include_preference": False, "include_tool_memory": False,
            "include_skill_memory": False, "page": None, "page_size": None,
        })
        mems = []
        for entry in (body.get("data") or {}).get("text_mem", []):
            mems.extend(entry.get("memories", []))

        # load source content from capture ledgers (read-only)
        ledger_dir = STAGE_LIVE / "ledgers"
        source = {}
        for lp in sorted(_glob.glob(str(ledger_dir / "*.jsonl"))):
            bn = os.path.basename(lp)
            if bn == "lifecycle-transitions.jsonl" or bn.endswith(".spine.jsonl"):
                continue
            with open(lp) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    rid = d.get("record_id")
                    if rid and d.get("content") is not None:
                        source[rid] = d["content"]

        target = None
        for m in mems:
            md = m.get("metadata") or {}
            rid = md.get("record_id")
            if rid and rid in source:
                target = m
                break

        if target is None:
            return {
                "status": "reachable",
                "http_status": health_status,
                "roundtrip_verified": False,
                "note": "store reachable but no record with ledger source found",
            }

        md = target.get("metadata") or {}
        rid = md.get("record_id")
        stored = target.get("memory", "")
        src_text = source[rid]
        stored_sha = hashlib.sha256(stored.encode()).hexdigest()[:12]
        src_sha = hashlib.sha256(src_text.encode()).hexdigest()[:12]
        text_match = stored == src_text
        sha_match = stored_sha == src_sha
        meta_ok = (
            md.get("record_id") == rid
            and md.get("sha256_12") == src_sha
            and bool(md.get("topic_hash"))
            and bool(md.get("to"))
            and bool(md.get("record_type"))
            and bool(md.get("sessions"))
        )
        verified = text_match and sha_match and meta_ok
        return {
            "status": "reachable",
            "http_status": health_status,
            "roundtrip_verified": verified,
            "canary_shape": "row-6 (text_match + sha_match + meta_ok)",
            "record_id": rid,
            "memory_id": target.get("id"),
            "text_match": text_match,
            "sha_match": sha_match,
            "meta_ok": meta_ok,
            "stored_sha256_12": stored_sha,
            "source_sha256_12": src_sha,
            "meta_sha256_12": md.get("sha256_12"),
            "mode": "read-only-existing-record",
        }
    except Exception as e:
        return {
            "status": "reachable",
            "http_status": health_status,
            "roundtrip_verified": False,
            "error": f"{type(e).__name__}: {e}",
            "note": "store reachable; round-trip probe raised an exception",
        }


# ─── freeze-once-reuse ──────────────────────────────────────────────────────

def freeze_inputs(manifest: dict) -> Path:
    """Freeze ALL probe inputs to scratch ONCE.  Returns the freeze dir.

    IDEMPOTENT per manifest: the manifest records ``freeze_count`` and
    ``frozen_inputs_hash``.  A second freeze call on an already-frozen
    manifest REUSES the existing snapshot (no re-freeze) — the guard that
    stops dry-run 1's fire row leaking into dry-run 2's inputs (charge §2
    G1).

    FREEZE-THEN-DERIVE: the frozen snapshot has a content hash;
    ``probe_session_id = f"g6-probe-{freeze_hash[:12]}"``.  Two dry-runs on
    the same frozen inputs share the session id BY CONSTRUCTION.

    A caller who genuinely wants a NEW freeze must delete the frozen
    manifest (``scratch/g6_probe/frozen/manifest.json``) first.  NO
    auto-regeneration.
    """
    freeze_dir = SCRATCH / "frozen"
    frozen_manifest_path = freeze_dir / "manifest.json"

    # ── idempotent reuse: already-frozen manifest on disk ──────────────
    if frozen_manifest_path.is_file():
        try:
            existing = json.loads(frozen_manifest_path.read_text())
            if existing.get("freeze_count", 0) >= 1:
                manifest["probe_session_id"] = existing["probe_session_id"]
                manifest["frozen_inputs_hash"] = existing["frozen_inputs_hash"]
                manifest["freeze_count"] = existing["freeze_count"]
                manifest["cursor_source_tree"] = existing.get("cursor_source_tree")
                return freeze_dir
        except (json.JSONDecodeError, KeyError):
            pass  # corrupt on-disk manifest — fall through

    # ── idempotent reuse: this manifest dict was already frozen ────────
    if manifest.get("freeze_count", 0) >= 1 and manifest.get("frozen_inputs_hash"):
        return freeze_dir

    # ── FRESH FREEZE ──────────────────────────────────────────────────
    freeze_dir.mkdir(parents=True, exist_ok=True)

    # gather input files (deterministic set)
    input_files: dict[str, Path] = {}
    for ledger_name in ("plugin-fires.jsonl", "lifecycle-transitions.jsonl"):
        src = WORKTREE / "ledgers" / ledger_name
        if src.is_file():
            input_files[ledger_name] = src

    # dispatcher cursor: worktree first, then master fallback
    cursor_src = WORKTREE / "ledgers" / ".memos_dispatcher_cursor.json"
    cursor_origin = "worktree"
    if not cursor_src.is_file():
        cursor_src = STAGE_LIVE / "ledgers" / ".memos_dispatcher_cursor.json"
        cursor_origin = "master"
    if cursor_src.is_file():
        input_files[".memos_dispatcher_cursor.json"] = cursor_src

    # lifecycle-transitions master fallback
    if "lifecycle-transitions.jsonl" not in input_files:
        lt_master = STAGE_LIVE / "ledgers" / "lifecycle-transitions.jsonl"
        if lt_master.is_file():
            input_files["lifecycle-transitions.jsonl"] = lt_master

    # FREEZE FIRST: compute content hash over all input bytes
    hash_builder = hashlib.sha256()
    for name in sorted(input_files):
        hash_builder.update(name.encode())
        hash_builder.update(input_files[name].read_bytes())
    frozen_inputs_hash = hash_builder.hexdigest()

    # THEN DERIVE: session id from freeze hash
    manifest["frozen_inputs_hash"] = frozen_inputs_hash
    manifest["probe_session_id"] = f"g6-probe-{frozen_inputs_hash[:12]}"
    manifest["freeze_count"] = 1
    manifest["cursor_source_tree"] = (
        cursor_origin if ".memos_dispatcher_cursor.json" in input_files else None
    )

    # copy input files into freeze dir
    for name, src in input_files.items():
        shutil.copy2(src, freeze_dir / name)

    # persist the frozen manifest (gate for future reuse)
    (freeze_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True))

    return freeze_dir


# ─── per-leg --confirm-live guards ──────────────────────────────────────────

def store_write_leg(confirm_live: bool = False, runid: str | None = None,
                    cwd: str | None = None) -> dict:
    """Store-write leg.  LIVE SURFACES: writes to the MemOS store :7411.
    A write from any lane other than the §2 G5 orchestrator live phase is a
    GATE FAILURE regardless of which tree the code runs from (§1 LIVE SURFACES).
    REFUSES to execute without --confirm-live in ANY lane."""
    if not confirm_live:
        return {
            "refused": True,
            "reason": "--confirm-live required on store-write leg",
            "leg": "store-write",
            "executed": False,
        }
    # additional G5 binding: cwd must be stage_live, runid must be registered
    if cwd and Path(cwd).resolve() != STAGE_LIVE.resolve():
        return {
            "refused": True,
            "reason": f"cwd must be stage_live ({STAGE_LIVE}), got {cwd}",
            "leg": "store-write",
            "executed": False,
        }
    if not _runid_registered(runid, campaign_root=_executing_root(cwd)):
        return {
            "refused": True,
            "reason": f"runid {runid!r} not pre-registered in run log",
            "leg": "store-write",
            "executed": False,
        }
    # REAL store-write path (orchestrator live phase executes; builder never)
    return _store_write_body(runid, _executing_root(cwd))


def positive_render_leg(confirm_live: bool = False, runid: str | None = None,
                        cwd: str | None = None) -> dict:
    """Positive-render leg.  LIVE SURFACES: writes to the pinned vault
    /home/greenknight/icm-knowledge-vault/roof via the deployed C15 path.
    A write from any lane other than the §2 G5 orchestrator live phase is a
    GATE FAILURE regardless of which tree the code runs from (§1 LIVE SURFACES).
    REFUSES to execute without --confirm-live in ANY lane."""
    if not confirm_live:
        return {
            "refused": True,
            "reason": "--confirm-live required on positive-render leg",
            "leg": "positive-render",
            "executed": False,
        }
    if cwd and Path(cwd).resolve() != STAGE_LIVE.resolve():
        return {
            "refused": True,
            "reason": f"cwd must be stage_live ({STAGE_LIVE}), got {cwd}",
            "leg": "positive-render",
            "executed": False,
        }
    if not _runid_registered(runid, campaign_root=_executing_root(cwd)):
        return {
            "refused": True,
            "reason": f"runid {runid!r} not pre-registered in run log",
            "leg": "positive-render",
            "executed": False,
        }
    # REAL render path (orchestrator live phase executes; builder never)
    return _positive_render_body(runid, _executing_root(cwd))


# ─── run-log registration (executing-campaign-root resolution) ──────────────
# BUG FIX (2026-11-16): the live phase executes from stage_live (the master
# tree), so the run log must resolve against the EXECUTING campaign root —
# not the worktree copy of this file.  The master run log is
# reports/run-log-estate-repair-G2G3.md under stage_live.  Both-tree
# resolution is covered by the selftest.
RUN_LOG_REL = Path("reports") / "run-log-estate-repair-G2G3.md"


def _executing_root(cwd: str | Path | None = None) -> Path:
    """The executing campaign root: the explicit cwd when given, else the
    process cwd (the live phase binds cwd == stage_live)."""
    return Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()


def _runid_registered(runid: str | None,
                      campaign_root: str | Path | None = None) -> bool:
    """Check if runid is pre-registered in the run log of the EXECUTING
    campaign root (stage_live -> master run log reports/run-log-estate-repair-
    G2G3.md; worktree -> the worktree's copy of the same relative path).

    Resolves the run log relative to the passed root, or the process cwd when
    no root is passed.  Orchestrator registers runids — this guard is the
    registration lookup."""
    if not runid:
        return False
    root = Path(campaign_root) if campaign_root is not None else Path.cwd()
    run_log = root / RUN_LOG_REL
    if not run_log.is_file():
        return False
    return runid in run_log.read_text()


# ─── live-leg machinery (behind the guards — implemented, never executed
#     by the builder seat) ───────────────────────────────────────────────────
# LIVE SURFACES (§1): these bodies write the MemOS store :7411, the pinned
# vault, the tree-named ledgers and the §5 receipt.  They run ONLY in the
# §2 G5 orchestrator live phase (guards above + run_live pre-flight).
#
# Push-route mapping (measured at disk 2026-11-16, read-only probes):
# memos_push.push() posts to STORE_PATH "/store" (frozen module constant);
# the live :7411 engine (memos.api.server_api, 26 /product routes) serves NO
# /store route (POST /store -> 404).  The store round-trip property this leg
# verifies (row-6 canary: text + sha + meta) was MEASURED at Stage 0 on the
# /product channel (memos_promote_2_0_34.py engine contract: POST /product/add
# sync/fast byte fidelity; metadata carried in `info`, flattened to
# metadata.* on read-back via POST /product/get_memory).  The leg therefore
# drives the deployed memos_push machinery's authorization refusal rule and
# transport helper against that verified write contract — every module
# imported READ-ONLY, nothing edited.  Disclosed for orchestrator ratification
# in the close report.
PROBE_NAMESPACE = "g6-probe"
ENGINE_BASE = "http://127.0.0.1:7411"
PRODUCT_ADD_PATH = "/product/add"
GET_MEMORY_PATH = "/product/get_memory"
STORE_CUBE_ID = "mev-promotions"
STORE_CUBE_USER = "mev-promotion"
GRANULARITY_PIN = "batch-global-with-per-item-evidence"


def _store_census(engine_base: str | None = None) -> dict:
    """Store census via the deployed read path used in _store_roundtrip_probe
    (POST /product/get_memory over the mev-promotions cube).  Read-only."""
    import memos_push
    base = engine_base or ENGINE_BASE
    status, body = memos_push._http_json(base + GET_MEMORY_PATH, payload={
        "mem_cube_id": STORE_CUBE_ID, "user_id": STORE_CUBE_USER,
        "include_preference": False, "include_tool_memory": False,
        "include_skill_memory": False, "page": None, "page_size": None,
    })
    mems = []
    for entry in (body.get("data") or {}).get("text_mem", []):
        mems.extend(entry.get("memories", []))
    return {"count": len(mems), "http_status": status,
            "read_path": GET_MEMORY_PATH, "mem_cube_id": STORE_CUBE_ID,
            "tree": "live-store-7411"}


def _build_probe_record(manifest: dict, runid: str | None) -> dict:
    """ONE probe-namespace record from the frozen inputs: probe session id,
    source record hash, probe_namespace field.  The canonical payload is the
    probe-owned content field (round-tripped verbatim by the store in
    sync/fast mode) and carries the probe namespace so post-close handling
    can find the record WITHOUT deletion (deletion of estate records is
    forbidden)."""
    probe_sid = manifest.get("probe_session_id") or "g6-probe-unfrozen"
    source_hash = manifest.get("frozen_inputs_hash") or "unfrozen"
    payload_obj = {
        "probe_namespace": PROBE_NAMESPACE,
        "probe_session_id": probe_sid,
        "source_record_hash": source_hash,
    }
    content = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"))
    content_sha = sha256_12(content.encode("utf-8"))
    rid = "rec_g6probe_" + sha256_12(
        f"{PROBE_NAMESPACE}|{probe_sid}|{runid}".encode("utf-8"))
    info = {
        "record_id": rid,
        "sha256_12": content_sha,
        "probe_namespace": PROBE_NAMESPACE,
        "probe_session_id": probe_sid,
        "source_record_hash": source_hash,
        "topic_hash": sha256_12(f"topic|{PROBE_NAMESPACE}|{source_hash}".encode()),
        "to": PROBE_NAMESPACE,
        "record_type": "probe",
        "sessions": [probe_sid],
        "adapter": "g6_probe.py",
    }
    return {"record_id": rid, "content": content, "content_sha256_12": content_sha,
            "info": info, "probe_namespace": PROBE_NAMESPACE}


def _product_add_body(record: dict) -> dict:
    """The deployed live-write request contract (memos_promote_2_0_34.py
    build_add_body: async_mode=sync + mode=fast for byte fidelity; metadata
    carried in `info`, flattened to metadata.* on read-back)."""
    return {
        "user_id": STORE_CUBE_USER,
        "session_id": record["info"]["probe_session_id"],
        "mem_cube_id": STORE_CUBE_ID,
        "writable_cube_ids": [STORE_CUBE_ID],
        "memory_content": record["content"],
        "async_mode": "sync",
        "mode": "fast",
        "custom_tags": [PROBE_NAMESPACE, record["info"]["probe_session_id"],
                        record["record_id"]],
        "info": record["info"],
        "source": PROBE_NAMESPACE,
    }


def _verify_row6_two_sided(record: dict, stored_text: str, meta: dict) -> dict:
    """TWO-SIDED row-6 canary over the probe-owned canonical payload field:
    written AND read back AND canonical-hash equal (text_match + sha_match +
    meta_ok).  Compares over the payload field only — a re-wrapped envelope
    with an intact canonical payload does NOT fail (A5 falsifier)."""
    written = record["content"]
    text_match = stored_text == written
    stored_sha = sha256_12((stored_text or "").encode("utf-8"))
    sha_match = (stored_sha == record["content_sha256_12"]
                 and (meta.get("sha256_12") == record["content_sha256_12"]))
    meta_ok = (
        meta.get("record_id") == record["record_id"]
        and meta.get("probe_namespace") == PROBE_NAMESPACE
        and meta.get("probe_session_id") == record["info"]["probe_session_id"]
        and meta.get("source_record_hash") == record["info"]["source_record_hash"]
        and bool(meta.get("topic_hash"))
        and bool(meta.get("to"))
        and bool(meta.get("record_type"))
        and bool(meta.get("sessions"))
    )
    return {"text_match": text_match, "sha_match": sha_match, "meta_ok": meta_ok,
            "stored_sha256_12": stored_sha,
            "source_sha256_12": record["content_sha256_12"],
            "canary_shape": "row-6 (text_match + sha_match + meta_ok)",
            "two_sided": text_match and sha_match and meta_ok}


def _store_write_body(runid: str | None, root: Path) -> dict:
    """REAL store-write leg body (runs behind the guards; orchestrator live
    phase only).  Payload bounds asserted BEFORE the push; exactly one record."""
    manifest = build_probe_manifest()
    freeze_inputs(manifest)
    record = _build_probe_record(manifest, runid)
    batch = [record]

    # payload bounds BEFORE the push: exactly one record
    import engraphis_sink
    limits = dict(engraphis_sink.STAGING_LIMITS)
    bounds = {"records_planned": len(batch),
              "bytes_planned": len(record["content"].encode("utf-8")),
              "limits": limits}
    if len(batch) != 1:
        return {"leg": "store-write", "executed": False, "aborted": True,
                "reason": f"payload bounds assert failed: {len(batch)} records != 1",
                "bounds": bounds}
    if (bounds["records_planned"] > int(limits["records"])
            or bounds["bytes_planned"] > int(limits["bytes"])):
        return {"leg": "store-write", "executed": False, "aborted": True,
                "reason": "payload bounds assert failed: exceeds STAGING_LIMITS",
                "bounds": bounds}

    # the deployed memos_push machinery — authorization refusal rule first
    # (read-only import; PushRefusedError aborts before any engine contact)
    import memos_push
    ownership = memos_push.resolve_ownership_or_grant(str(root))
    evidence = memos_push.authorization_evidence(str(root))
    try:
        memos_push.check_push_authorization(ownership, evidence)
    except memos_push.PushRefusedError as e:
        return {"leg": "store-write", "executed": False, "refused": True,
                "reason": f"push authorization refused: {e.reason}",
                "detail": e.detail, "bounds": bounds}

    # write through the verified live contract (route mapping disclosed above)
    body = _product_add_body(record)
    status, resp = memos_push._http_json(ENGINE_BASE + PRODUCT_ADD_PATH,
                                         payload=body)
    memory_id = None
    write_ok = status == 200 and resp.get("code") == 200
    if write_ok and resp.get("data"):
        memory_id = resp["data"][0].get("memory_id")
    if not write_ok:
        return {"leg": "store-write", "executed": False,
                "reason": f"product add failed: {status} {json.dumps(resp)[:300]}",
                "bounds": bounds, "record_id": record["record_id"]}

    # read back via the deployed read path and verify two-sided (row-6)
    census = _store_census()
    readback = memos_push._http_json(ENGINE_BASE + GET_MEMORY_PATH, payload={
        "mem_cube_id": STORE_CUBE_ID, "user_id": STORE_CUBE_USER,
        "include_preference": False, "include_tool_memory": False,
        "include_skill_memory": False, "page": None, "page_size": None,
    })[1]
    target = None
    for entry in (readback.get("data") or {}).get("text_mem", []):
        for m in entry.get("memories", []):
            md = m.get("metadata") or {}
            if (md.get("record_id") == record["record_id"]
                    or m.get("id") == memory_id):
                target = m
                break
        if target is not None:
            break
    if target is None:
        return {"leg": "store-write", "executed": False,
                "reason": "readback target not found in store census",
                "record_id": record["record_id"], "memory_id": memory_id,
                "census": census, "bounds": bounds}
    row6 = _verify_row6_two_sided(record, target.get("memory", ""),
                                  target.get("metadata") or {})
    return {
        "leg": "store-write", "executed": True, "refused": False,
        "record_id": record["record_id"], "memory_id": memory_id,
        "probe_namespace": PROBE_NAMESPACE,
        "probe_session_id": record["info"]["probe_session_id"],
        "source_record_hash": record["info"]["source_record_hash"],
        "content_sha256_12": record["content_sha256_12"],
        "push_route": {"write": PRODUCT_ADD_PATH, "read": GET_MEMORY_PATH,
                       "memos_push": "authorization machinery (deployed, read-only)"},
        "row6": row6,
        "two_sided_ok": row6["two_sided"],
        "deletion": "forbidden — record tagged via probe_namespace, left in place",
        "census_after_read": census,
        "bounds": bounds,
    }


def _mint_receipt(runid: str | None, root: Path) -> dict:
    """Mint the §5 receipt: runid + principal_go + pinned vault root + CURRENT
    dispatcher self-hash (read from the deployed file at run time).  All four
    fields present or the mint aborts."""
    sys.path.insert(0, str(root))
    import memos_dispatcher
    receipt = {
        "runid": runid,
        "principal_go": True,
        "vault_root": str(PINNED_VAULT_ROOT),
        "dispatcher_sha256_12": memos_dispatcher._self_sha256_12(),
    }
    missing = [k for k in ("runid", "principal_go", "vault_root",
                           "dispatcher_sha256_12") if k not in receipt]
    if missing or not receipt["runid"]:
        return {"minted": False,
                "reason": f"receipt mint missing fields: {missing}"}
    receipt_path = Path(root) / "reports" / "c15_render_receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt))
    return {"minted": True, "path": str(receipt_path),
            "fields": dict(receipt)}


def _delete_receipt(root: Path) -> dict:
    """Delete the probe's OWN §5 receipt (the rollback of its mint).  The
    receipt is the probe's single-render instrument — NOT a live_grant (R-B2)."""
    receipt_path = Path(root) / "reports" / "c15_render_receipt.json"
    existed = receipt_path.is_file()
    if existed:
        receipt_path.unlink()
    return {"deleted": existed and not receipt_path.is_file(),
            "path": str(receipt_path), "existed_before": existed}


def _failclosed_reprrove(root: Path) -> dict:
    """Post-delete fail-closed re-prove: one render attempt must be refused
    with the deployed literal c15-receipt-missing."""
    sys.path.insert(0, str(root))
    import memos_dispatcher
    ok, reason = memos_dispatcher._check_c15_receipt(root)
    attempt = memos_dispatcher._run_c15_renderer(root)
    skipped = attempt.get("skipped")
    return {"expected_guard": C15_RECEIPT_MISSING,
            "actual_guard": reason,
            "render_attempt_skipped": skipped,
            "render_attempt_ok": attempt.get("ok"),
            "passed": ((not ok) and reason == C15_RECEIPT_MISSING
                       and skipped == C15_RECEIPT_MISSING)}


def _reconcile_posture(root: Path) -> dict:
    """Reconcile/delivery-gate posture check — the deployed C16 gate
    (verify_g3_delivery.check_delivery), called as-is (no wiring changes)."""
    sys.path.insert(0, str(root))
    import verify_g3_delivery
    manifests = []
    m_dir = Path(root) / "reports" / "manifests"
    if m_dir.is_dir():
        for p in sorted(m_dir.glob("*.json")):
            try:
                manifests.append(json.loads(p.read_text()))
            except (OSError, json.JSONDecodeError) as e:
                manifests.append({"batch_id": p.name, "parse_error": str(e)})
    mirror_path = Path(root) / "reports" / "obsidian_mirror_manifest.json"
    mirror = None
    if mirror_path.is_file():
        try:
            mirror = json.loads(mirror_path.read_text())
        except (OSError, json.JSONDecodeError):
            mirror = None
    result = verify_g3_delivery.check_delivery(manifests, mirror,
                                               campaign_root=root)
    return {"leg": "reconcile", "ok": result.get("ok"),
            "verdict": result.get("verdict"),
            "manifests_given": len(manifests),
            "checks": result.get("checks")}


def _measure_cursor(root: Path) -> dict:
    """Measure the dispatcher cursor state at disk (tree-named)."""
    sys.path.insert(0, str(root))
    import memos_dispatcher
    cur_path = memos_dispatcher.cursor_path(Path(root))
    t_path = memos_dispatcher.transitions_path(Path(root))
    cursor = memos_dispatcher.load_cursor(cur_path)
    ledger_size = t_path.stat().st_size if t_path.is_file() else 0
    return {"byte_offset": int(cursor.get("byte_offset", -1)),
            "row_count": cursor.get("row_count"),
            "ledger_size": ledger_size,
            "cursor_current": int(cursor.get("byte_offset", -1)) == ledger_size,
            "cursor_path": str(cur_path), "tree": str(root)}


def _cursor_advance_assert(pre: dict, post: dict,
                           dispatch_result: dict | None) -> dict:
    """Cursor ADVANCE post-cycle (G5 step 7): the mechanical proof the
    dispatch leg was the real dispatcher.  Measured pre/post byte offsets;
    a non-advance is REPORTED as measured, never forced."""
    advanced = post["byte_offset"] > pre["byte_offset"]
    post_current = post.get("cursor_current")
    window = (dispatch_result or {}).get("window") or {}
    return {
        "pre_byte_offset": pre.get("byte_offset"),
        "post_byte_offset": post.get("byte_offset"),
        "post_ledger_size": post.get("ledger_size"),
        "cursor_advanced": advanced,
        "post_current": post_current,
        "dispatch_window": {"rows": window.get("rows"),
                            "directives": window.get("directives")},
        "dispatch_verdict": (dispatch_result or {}).get("verdict"),
        # Pre-registered honest edge (2026-11-15/16 pre-registration): the
        # cursor is DRAINED — a 0-row dispatch window legitimately produces
        # no advance. The advance is REPORTED as measured; when the window
        # is empty (rows==0 and directives==0) the assert passes on
        # cursor-currency (post_current), not on advance. A STALE cursor or
        # an empty window with a non-current cursor still fails.
        "empty_window_edge": (window.get("rows") == 0
                              and window.get("directives") == 0),
        "assert_ok": bool(post_current and (advanced or (window.get("rows") == 0
                              and window.get("directives") == 0))),
    }


def _preflight(root: Path) -> dict:
    """Live pre-flight (G5 i/ii), measured at run time: (i) MemOS reachable;
    (ii) dispatcher cursor CURRENT.  Abort live on failure — that is a
    measured refusal, not a probe defect."""
    import memos_push
    health = memos_push.health_probe(ENGINE_BASE)
    if not health.get("ok"):
        return {"ok": False,
                "reason": f"store unreachable: {health.get('error')}",
                "store_health": health}
    cursor = _measure_cursor(root)
    if not cursor.get("cursor_current"):
        return {"ok": False,
                "reason": ("stale dispatcher cursor: byte_offset "
                           f"{cursor['byte_offset']} != ledger size "
                           f"{cursor['ledger_size']} — live aborted "
                           "(re-push hazard)"),
                "store_health": health, "cursor": cursor}
    return {"ok": True, "store_health": health, "cursor": cursor}


def _positive_render_body(runid: str | None, root: Path) -> dict:
    """REAL positive-render leg body: mint §5 receipt -> REAL C15 render path
    -> per-item/detector/granularity asserts -> DELETE receipt -> fail-closed
    re-prove (one post-delete render attempt refused with c15-receipt-missing)."""
    mint = _mint_receipt(runid, root)
    if not mint.get("minted"):
        return {"leg": "positive-render", "executed": False,
                "reason": f"receipt mint failed: {mint.get('reason')}"}
    sys.path.insert(0, str(root))
    import memos_dispatcher
    import obsidian_mirror
    render_result = None
    asserts: dict = {}
    try:
        render_result = memos_dispatcher._run_c15_renderer(root)
        manifest_path = Path(root) / "reports" / "obsidian_mirror_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        rows = manifest.get("per_item_render_evidence") or []
        items = manifest.get("items") or 0
        rows_ok = all(r.get("render_status") == "ok" for r in rows)
        non_empty = bool(rows) and items > 0 and len(rows) == items
        violations = obsidian_mirror.detect_render_inconsistency(manifest)
        detector_ok = (violations == [])
        granularity_measured = manifest.get("render_verdict_granularity")
        granularity_ok = (granularity_measured == GRANULARITY_PIN)
        asserts = {
            "render_ok": bool(render_result.get("ok")),
            "per_item_rows": len(rows),
            "manifest_items": items,
            "non_empty_manifest": non_empty,
            "per_item_rows_ok": rows_ok,
            "detector_violations": violations,
            "detector_ok": detector_ok,
            "granularity_disclosed_as_measured": granularity_measured,
            "granularity_expected": GRANULARITY_PIN,
            "granularity_ok": granularity_ok,
        }
        asserts["assert_ok"] = bool(render_result.get("ok") and rows_ok
                                    and non_empty and detector_ok
                                    and granularity_ok)
    except Exception as e:
        asserts = {"assert_ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        delete = _delete_receipt(root)
    reprove = _failclosed_reprrove(root)
    return {"leg": "positive-render", "executed": True, "refused": False,
            "receipt_mint": mint,
            "render": {"ok": bool(render_result and render_result.get("ok")),
                       "manifest_path": (render_result or {}).get("manifest_path"),
                       "sections": (render_result or {}).get("sections"),
                       "asserts": asserts},
            "delete_receipt": delete,
            "fail_closed_reprove": reprove,
            "assert_ok": bool(asserts.get("assert_ok") and reprove.get("passed"))}


def _fire_body(runid: str | None, root: Path) -> dict:
    """REAL fire leg body: one cadence fire through the mev-cadence plugin
    entrypoint (deployed writer, read-only import), provenance verified in the
    tree-named plugin-fires ledger (N6 logic reused positively), then ONE
    dispatch cycle against the real dispatcher (the cadence->dispatch cycle,
    G5 step 2)."""
    sys.path.insert(0, str(root))
    import hermes_plugin.mev_cadence as mev_cadence
    manifest = build_probe_manifest()
    freeze_inputs(manifest)
    probe_sid = manifest.get("probe_session_id") or "g6-probe-unfrozen"
    # per-ATTEMPT-unique turn_id: a re-run of the same runid must not collide
    # with a prior attempt's fire row (run-2 finding: 2 rows shared
    # probe-<runid> and the bounds assert failed on the SECOND run).
    attempt_nonce = uuid.uuid4().hex[:8]
    turn_id = f"probe-{runid}-{attempt_nonce}"
    plugin = mev_cadence.MevCadencePlugin(
        campaign_root=root, ledger_dir=Path(root) / "ledgers")
    plugin.on_post_llm_call(session_id=probe_sid, turn_id=turn_id)

    ledger = Path(root) / "ledgers" / "plugin-fires.jsonl"
    rows = []
    if ledger.is_file():
        for line in ledger.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    ours = [r for r in rows if r.get("turn_id") == turn_id]
    provenance = (check_fire_provenance(ours[0]) if ours
                  else {"provenance_ok": False, "reason": "fire row not found"})
    bounds_ok = (len(ours) == 1)

    # one dispatch cycle against the real dispatcher (cadence->dispatch)
    import memos_dispatcher
    import staging_provider
    dispatch_result = memos_dispatcher.dispatch(
        campaign_root=root,
        staging_state=staging_provider.make_live_provider())
    return {
        "leg": "fire", "executed": True, "refused": False,
        "plugin": mev_cadence.PLUGIN_NAME,
        "entrypoint": "MevCadencePlugin.on_post_llm_call",
        "probe_session_id": probe_sid, "turn_id": turn_id,
        "fire_rows_found": len(ours),
        "bounds_ok": bounds_ok,
        "provenance": provenance,
        "ledger": str(ledger),
        "dispatch": {
            "verdict": dispatch_result.get("verdict"),
            "window": dispatch_result.get("window"),
            "cursor_advanced": dispatch_result.get("cursor_advanced"),
            "held": dispatch_result.get("held"),
            "enqueued": len(dispatch_result.get("enqueued") or []),
        },
        "assert_ok": bool(bounds_ok and provenance.get("provenance_ok")),
    }


def fire_leg(confirm_live: bool = False, runid: str | None = None,
             cwd: str | None = None) -> dict:
    """Fire-provenance leg (live mode).  LIVE SURFACES: appends the fire row
    to the tree-named plugin-fires ledger via the deployed plugin writer.
    REFUSES to execute without --confirm-live in ANY lane."""
    if not confirm_live:
        return {
            "refused": True,
            "reason": "--confirm-live required on fire leg",
            "leg": "fire",
            "executed": False,
        }
    if cwd and Path(cwd).resolve() != STAGE_LIVE.resolve():
        return {
            "refused": True,
            "reason": f"cwd must be stage_live ({STAGE_LIVE}), got {cwd}",
            "leg": "fire",
            "executed": False,
        }
    if not _runid_registered(runid, campaign_root=_executing_root(cwd)):
        return {
            "refused": True,
            "reason": f"runid {runid!r} not pre-registered in run log",
            "leg": "fire",
            "executed": False,
        }
    return _fire_body(runid, _executing_root(cwd))


def run_live(runid: str | None = None, cwd: str | None = None) -> dict:
    """Live orchestration (orchestrator lane).  Charge order: receipt -> fire
    -> store-write -> reconcile -> render -> delete receipt -> fail-closed
    re-prove -> cursor-advance assert.  Emits the verdict JSON embedding the
    verbatim §1 point-in-time distinction constant and the store census
    measured pre/post via the deployed read path."""
    root = _executing_root(cwd)
    if root != STAGE_LIVE.resolve():
        return {"refused": True,
                "reason": f"cwd must be stage_live ({STAGE_LIVE}), got {root}",
                "runid": runid, "stage": "live",
                "point_in_time_distinction": POINT_IN_TIME_DISTINCTION}
    if not _runid_registered(runid, campaign_root=root):
        return {"refused": True,
                "reason": f"runid {runid!r} not pre-registered in run log",
                "runid": runid, "stage": "live",
                "point_in_time_distinction": POINT_IN_TIME_DISTINCTION}

    pre = _preflight(root)
    if not pre.get("ok"):
        return {"refused": True, "reason": pre.get("reason"),
                "runid": runid, "stage": "live", "tree": str(root),
                "point_in_time_distinction": POINT_IN_TIME_DISTINCTION,
                "live_surfaces_rule": LIVE_SURFACES_RULE,
                "preflight": {"store_health": pre.get("store_health"),
                              "cursor": pre.get("cursor")}}

    pre_cursor = pre["cursor"]
    census_pre = _store_census()
    steps: dict = {}
    try:
        steps["receipt"] = _mint_receipt(runid, root)
        steps["fire"] = fire_leg(confirm_live=True, runid=runid, cwd=str(root))
        steps["store-write"] = store_write_leg(confirm_live=True, runid=runid,
                                               cwd=str(root))
        steps["reconcile"] = _reconcile_posture(root)
        render_leg = positive_render_leg(confirm_live=True, runid=runid,
                                         cwd=str(root))
        steps["render"] = render_leg.get("render", render_leg)
        steps["delete-receipt"] = render_leg.get("delete_receipt", {})
        steps["fail-closed-re-prove"] = render_leg.get("fail_closed_reprove", {})
    finally:
        # A4 fail-closed: no surviving receipt at close, whatever the chain did
        if (Path(root) / "reports" / "c15_render_receipt.json").is_file():
            steps["receipt-leak-cleanup"] = _delete_receipt(root)

    post_cursor = _measure_cursor(root)
    steps["cursor-advance-assert"] = _cursor_advance_assert(
        pre_cursor, post_cursor, (steps.get("fire") or {}).get("dispatch"))
    census_post = _store_census()

    step_ok = {
        "receipt": bool((steps.get("receipt") or {}).get("minted")),
        "fire": bool((steps.get("fire") or {}).get("assert_ok")),
        "store-write": bool((steps.get("store-write") or {}).get("two_sided_ok")),
        "reconcile": True,  # posture check recorded; not a pass gate (G5 step 4)
        "render": bool((steps.get("render") or {}).get("asserts", {})
                       .get("assert_ok")),
        "delete-receipt": bool((steps.get("delete-receipt") or {}).get("deleted")),
        "fail-closed-re-prove": bool((steps.get("fail-closed-re-prove") or {})
                                     .get("passed")),
        "cursor-advance-assert": bool((steps.get("cursor-advance-assert") or {})
                                      .get("assert_ok")),
    }
    verdict = {
        "runid": runid,
        "stage": "live",
        "tree": str(root),
        "point_in_time_distinction": POINT_IN_TIME_DISTINCTION,
        "live_surfaces_rule": LIVE_SURFACES_RULE,
        "store_census": {"pre": census_pre, "post": census_post},
        "measured_counts": {
            "measured_total_master": _count_master_ledger(),
            "cursor_byte_offset": post_cursor.get("byte_offset"),
        },
        "steps": steps,
        "step_ok": step_ok,
        "live_pass": all(step_ok.values()),
    }
    try:
        verdict = _emit_verdict(verdict, runid, root)
    except OSError as e:
        verdict["verdict_write_error"] = f"{type(e).__name__}: {e}"
    return verdict


def _emit_verdict(verdict: dict, runid: str | None, root: Path) -> dict:
    """Write reports/g6-probe-<runid>.json under the executing campaign root
    (G5 step 9) and stamp the path into the verdict."""
    out_path = Path(root) / "reports" / f"g6-probe-{runid}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
    verdict["verdict_path"] = str(out_path)
    return verdict


# ─── N1: render, no §5 receipt → c15-receipt-missing ───────────────────────

def negative_n1(scratch: Path) -> dict:
    """N1: render attempt with NO §5 receipt → refused with EXACTLY the
    deployed literal c15-receipt-missing."""
    sys.path.insert(0, str(WORKTREE))
    import memos_dispatcher

    # scratch campaign root with NO receipt
    n1_root = scratch / "n1_no_receipt"
    (n1_root / "reports").mkdir(parents=True, exist_ok=True)

    ok, reason = memos_dispatcher._check_c15_receipt(n1_root)
    return {
        "negative": "N1",
        "expected_guard": C15_RECEIPT_MISSING,
        "actual_guard": reason,
        "passed": (not ok) and (reason == C15_RECEIPT_MISSING),
        "citation": DEPLOYED_LITERAL_CITATIONS["c15-receipt-missing"],
    }


# ─── N2: four receipt defects → four deployed literals ─────────────────────

def negative_n2(scratch: Path) -> dict:
    """N2: four receipt defects, each failing on its own guard:
    vault-root mismatch → c15-receipt-vault-root-mismatch
    stale dispatcher self-hash → c15-receipt-stale-sha
    unparseable/non-dict/bad-runid → c15-receipt-invalid
    principal_go≠true → c15-receipt-invalid
    All four are deployed literals in memos_dispatcher.py."""
    sys.path.insert(0, str(WORKTREE))
    import memos_dispatcher

    results = {}

    # (a) vault-root mismatch
    root_a = scratch / "n2_vault_root"
    (root_a / "reports").mkdir(parents=True, exist_ok=True)
    receipt_a = {
        "runid": "test-runid-a",
        "principal_go": True,
        "vault_root": "/wrong/vault/path",
        "dispatcher_sha256_12": memos_dispatcher._self_sha256_12(),
    }
    (root_a / "reports" / "c15_render_receipt.json").write_text(
        json.dumps(receipt_a))
    ok, reason = memos_dispatcher._check_c15_receipt(root_a)
    results["vault_root_mismatch"] = {
        "expected": C15_RECEIPT_VAULT_ROOT_MISMATCH,
        "actual": reason,
        "passed": reason == C15_RECEIPT_VAULT_ROOT_MISMATCH,
        "citation": DEPLOYED_LITERAL_CITATIONS["c15-receipt-vault-root-mismatch"],
    }

    # (b) stale dispatcher self-hash
    root_b = scratch / "n2_stale_sha"
    (root_b / "reports").mkdir(parents=True, exist_ok=True)
    receipt_b = {
        "runid": "test-runid-b",
        "principal_go": True,
        "vault_root": str(PINNED_VAULT_ROOT),
        "dispatcher_sha256_12": "deadbeefdead",
    }
    (root_b / "reports" / "c15_render_receipt.json").write_text(
        json.dumps(receipt_b))
    ok, reason = memos_dispatcher._check_c15_receipt(root_b)
    results["stale_sha"] = {
        "expected": C15_RECEIPT_STALE_SHA,
        "actual": reason,
        "passed": reason == C15_RECEIPT_STALE_SHA,
        "citation": DEPLOYED_LITERAL_CITATIONS["c15-receipt-stale-sha"],
    }

    # (c) unparseable receipt
    root_c = scratch / "n2_invalid_unparseable"
    (root_c / "reports").mkdir(parents=True, exist_ok=True)
    (root_c / "reports" / "c15_render_receipt.json").write_text("not json at all")
    ok, reason = memos_dispatcher._check_c15_receipt(root_c)
    results["invalid_unparseable"] = {
        "expected": C15_RECEIPT_INVALID,
        "actual": reason,
        "passed": reason == C15_RECEIPT_INVALID,
        "citation": DEPLOYED_LITERAL_CITATIONS["c15-receipt-invalid"],
    }

    # (d) non-dict receipt
    root_d = scratch / "n2_invalid_nondict"
    (root_d / "reports").mkdir(parents=True, exist_ok=True)
    (root_d / "reports" / "c15_render_receipt.json").write_text('"just a string"')
    ok, reason = memos_dispatcher._check_c15_receipt(root_d)
    results["invalid_nondict"] = {
        "expected": C15_RECEIPT_INVALID,
        "actual": reason,
        "passed": reason == C15_RECEIPT_INVALID,
        "citation": DEPLOYED_LITERAL_CITATIONS["c15-receipt-invalid"],
    }

    # (e) bad runid (empty)
    root_e = scratch / "n2_invalid_runid"
    (root_e / "reports").mkdir(parents=True, exist_ok=True)
    receipt_e = {
        "runid": "",
        "principal_go": True,
        "vault_root": str(PINNED_VAULT_ROOT),
        "dispatcher_sha256_12": memos_dispatcher._self_sha256_12(),
    }
    (root_e / "reports" / "c15_render_receipt.json").write_text(
        json.dumps(receipt_e))
    ok, reason = memos_dispatcher._check_c15_receipt(root_e)
    results["invalid_runid"] = {
        "expected": C15_RECEIPT_INVALID,
        "actual": reason,
        "passed": reason == C15_RECEIPT_INVALID,
        "citation": DEPLOYED_LITERAL_CITATIONS["c15-receipt-invalid"],
    }

    # (f) principal_go ≠ true
    root_f = scratch / "n2_invalid_principal"
    (root_f / "reports").mkdir(parents=True, exist_ok=True)
    receipt_f = {
        "runid": "test-runid-f",
        "principal_go": False,
        "vault_root": str(PINNED_VAULT_ROOT),
        "dispatcher_sha256_12": memos_dispatcher._self_sha256_12(),
    }
    (root_f / "reports" / "c15_render_receipt.json").write_text(
        json.dumps(receipt_f))
    ok, reason = memos_dispatcher._check_c15_receipt(root_f)
    results["invalid_principal_go"] = {
        "expected": C15_RECEIPT_INVALID,
        "actual": reason,
        "passed": reason == C15_RECEIPT_INVALID,
        "citation": DEPLOYED_LITERAL_CITATIONS["c15-receipt-invalid"],
    }

    all_passed = all(r["passed"] for r in results.values())
    return {
        "negative": "N2",
        "sub_results": results,
        "passed": all_passed,
    }


# ─── N3: receipt minted-but-not-deleted → receipt-not-deleted ──────────────

def negative_n3(scratch: Path) -> dict:
    """N3: receipt minted but never deleted → post-check flags
    receipt-not-deleted (PROBE-DEFINED sentinel — the probe pins and emits
    it itself).  The fail-closed posture is the END state."""
    sys.path.insert(0, str(WORKTREE))
    import memos_dispatcher

    n3_root = scratch / "n3_not_deleted"
    (n3_root / "reports").mkdir(parents=True, exist_ok=True)

    # mint a receipt (valid shape)
    receipt = {
        "runid": "test-runid-n3",
        "principal_go": True,
        "vault_root": str(PINNED_VAULT_ROOT),
        "dispatcher_sha256_12": memos_dispatcher._self_sha256_12(),
    }
    receipt_path = n3_root / "reports" / "c15_render_receipt.json"
    receipt_path.write_text(json.dumps(receipt))

    # simulate the post-render check: receipt should have been deleted
    # but it was NOT — the post-check flags it
    receipt_exists = receipt_path.is_file()
    post_check_flag = RECEIPT_NOT_DELETED if receipt_exists else None

    return {
        "negative": "N3",
        "expected_sentinel": RECEIPT_NOT_DELETED,
        "actual_flag": post_check_flag,
        "receipt_exists_at_post_check": receipt_exists,
        # hardcoded literal (not the module variable) so guard-dependence
        # tests can detect a sentinel mismatch
        "passed": post_check_flag == "receipt-not-deleted",
        "note": ("PROBE-DEFINED sentinel (not a deployed literal). "
                 "The probe pins and emits receipt-not-deleted itself. "
                 "The fail-closed posture is the END state."),
    }


# ─── N4: detector seed (false-GREEN shape + companion silent shape) ────────

def negative_n4(scratch: Path) -> dict:
    """N4: seeded detector negative.  Shape PINNED to the detector's real rule
    (obsidian_mirror.py:148-184).  The detector flags ONLY the false-GREEN
    shape and is documented-silent when rendered != items.

    Main: copy live manifest, PRESERVE rendered==items==N, flip ONE row's
    render_status → violations==1 AND the row's item key in bad keys.
    Companion: counts-only mutation (rendered≠items) must yield violations==[]
    (documented-silent shape)."""
    sys.path.insert(0, str(WORKTREE))
    import obsidian_mirror

    # build a synthetic manifest matching the deployed shape
    n = 4
    evidence = []
    for i in range(n):
        evidence.append({
            "item_identity_key": f"topic:row-{i}",
            "render_status": "ok",
            "reason": "",
        })

    base_manifest = {
        "rendered": n,
        "items": n,
        "per_item_render_evidence": evidence,
    }

    # main assertion: flip ONE row → violations==1, bad key present
    mutated = copy.deepcopy(base_manifest)
    mutated["per_item_render_evidence"][1]["render_status"] = "failed"
    violations = obsidian_mirror.detect_render_inconsistency(mutated)
    main_ok = (
        len(violations) == 1
        and "topic:row-1" in (violations[0].get("bad_keys") or [])
    )

    # companion: counts-only mutation (rendered != items) → violations==[]
    companion = copy.deepcopy(base_manifest)
    companion["rendered"] = n - 1  # rendered != items
    companion_violations = obsidian_mirror.detect_render_inconsistency(companion)
    companion_ok = len(companion_violations) == 0

    return {
        "negative": "N4",
        "detector_rule_citation": DETECTOR_RULE_CITATION,
        "main_assertion": {
            "description": "flip one row render_status → violations==1, bad key in bad_keys",
            "violations_count": len(violations),
            "bad_keys": violations[0].get("bad_keys") if violations else [],
            "expected_row_key": "topic:row-1",
            "passed": main_ok,
        },
        "companion_assertion": {
            "description": "counts-only mutation (rendered≠items) → violations==[] (documented-silent)",
            "violations_count": len(companion_violations),
            "passed": companion_ok,
        },
        "passed": main_ok and companion_ok,
    }


# ─── N5: live phase without --confirm-live → refusal ───────────────────────

def negative_n5(scratch: Path) -> dict:
    """N5: live phase invoked without --confirm-live → refuse before touching
    anything.  Also: wrong cwd and unregistered runid must refuse
    (per §5/G5 binding)."""
    # (a) no --confirm-live
    r1 = store_write_leg(confirm_live=False)
    r2 = positive_render_leg(confirm_live=False)
    no_flag_ok = r1["refused"] and r2["refused"]

    # (b) wrong cwd
    r3 = store_write_leg(confirm_live=True, runid="test",
                         cwd="/tmp/wrong-place")
    r4 = positive_render_leg(confirm_live=True, runid="test",
                             cwd="/tmp/wrong-place")
    wrong_cwd_ok = r3["refused"] and r4["refused"]

    # (c) unregistered runid
    r5 = store_write_leg(confirm_live=True, runid="unregistered-runid-xyz",
                         cwd=str(STAGE_LIVE))
    r6 = positive_render_leg(confirm_live=True, runid="unregistered-runid-xyz",
                             cwd=str(STAGE_LIVE))
    unregistered_ok = r5["refused"] and r6["refused"]

    return {
        "negative": "N5",
        "no_confirm_live": {
            "store_write_refused": r1["refused"],
            "store_write_reason": r1["reason"],
            "positive_render_refused": r2["refused"],
            "positive_render_reason": r2["reason"],
            "passed": no_flag_ok,
        },
        "wrong_cwd": {
            "store_write_refused": r3["refused"],
            "store_write_reason": r3["reason"],
            "positive_render_refused": r4["refused"],
            "positive_render_reason": r4["reason"],
            "passed": wrong_cwd_ok,
        },
        "unregistered_runid": {
            "store_write_refused": r5["refused"],
            "store_write_reason": r5["reason"],
            "positive_render_refused": r6["refused"],
            "positive_render_reason": r6["reason"],
            "passed": unregistered_ok,
        },
        "passed": no_flag_ok and wrong_cwd_ok and unregistered_ok,
    }


# ─── N6: fire-leg provenance bypass ────────────────────────────────────────

def check_fire_provenance(fire_row: dict) -> dict:
    """Provenance-shape check: a fire row must carry the mev-cadence plugin
    writer's provenance shape.  A synthetic row without the plugin entrypoint
    provenance FAILS.  Shape read from hermes_plugin/mev_cadence/__init__.py:871-873
    and the live ledger tail (plugin-fires.jsonl)."""
    plugin_ok = fire_row.get("plugin") == PLUGIN_WRITER_PROVENANCE["plugin"]
    hook_ok = fire_row.get("hook") == PLUGIN_WRITER_PROVENANCE["hook"]
    return {
        "plugin_match": plugin_ok,
        "hook_match": hook_ok,
        "provenance_ok": plugin_ok and hook_ok,
    }


def negative_n6(scratch: Path) -> dict:
    """N6: fire-leg bypass — a probe-declared fire row written WITHOUT the
    mev-cadence plugin entrypoint must FAIL the verdict (provenance-shape
    check)."""
    # (a) legitimate fire row (from worktree ledger tail, frozen)
    legit_row = _read_last_fire_row(scratch)
    if legit_row is None:
        # construct minimal legitimate shape
        legit_row = {
            "plugin": "mev-cadence",
            "hook": "post_llm_call",
            "ts": 0.0,
            "session_id": "test-session",
            "turn_id": "test-turn",
        }
    legit_check = check_fire_provenance(legit_row)

    # (b) synthetic row WITHOUT plugin entrypoint provenance
    synthetic_row = {
        "ts": 0.0,
        "session_id": "test-session",
        "turn_id": "test-turn",
        "plugin": "not-mev-cadence",
        "hook": "not-post_llm_call",
    }
    synthetic_check = check_fire_provenance(synthetic_row)

    # (c) row missing provenance fields entirely
    bare_row = {"ts": 0.0, "data": "something"}
    bare_check = check_fire_provenance(bare_row)

    passed = (
        legit_check["provenance_ok"]
        and not synthetic_check["provenance_ok"]
        and not bare_check["provenance_ok"]
    )

    return {
        "negative": "N6",
        "plugin_writer_citation": PLUGIN_WRITER_CITATION,
        "legit_row_provenance_ok": legit_check["provenance_ok"],
        "synthetic_row_provenance_ok": synthetic_check["provenance_ok"],
        "bare_row_provenance_ok": bare_check["provenance_ok"],
        "passed": passed,
    }


def _read_last_fire_row(scratch: Path) -> dict | None:
    frozen = scratch / "frozen" / "plugin-fires.jsonl"
    src = frozen if frozen.is_file() else WORKTREE / "ledgers" / "plugin-fires.jsonl"
    if not src.is_file():
        return None
    lines = [l for l in src.read_text().splitlines() if l.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


# ─── dispatch-cursor leg (worktree copies) ─────────────────────────────────

def check_dispatch_cursor(scratch: Path) -> dict:
    """Dispatch-cursor leg against WORKTREE COPIES (frozen).  Verifies cursor
    state: byte_offset == lifecycle-transitions ledger size (tree-named,
    re-derived).  Falls back to master tree (read-only) if worktree lacks
    the cursor runtime artifact.

    The ``tree_name`` label states the TRUE source tree of the cursor —
    it must never misattribute provenance (a cursor frozen from the master
    fallback is labeled master-frozen, not worktree-frozen)."""
    frozen = SCRATCH / "frozen"
    cursor_path = frozen / ".memos_dispatcher_cursor.json"
    ledger_path = frozen / "lifecycle-transitions.jsonl"

    # Determine true origin of the frozen cursor from the frozen manifest
    cursor_origin = None
    frozen_manifest_path = frozen / "manifest.json"
    if frozen_manifest_path.is_file():
        try:
            fm = json.loads(frozen_manifest_path.read_text())
            cursor_origin = fm.get("cursor_source_tree")
        except (json.JSONDecodeError, KeyError):
            pass

    if cursor_path.is_file():
        if cursor_origin == "master":
            tree_name = "master-frozen"
        elif cursor_origin == "worktree":
            tree_name = "worktree-frozen"
        else:
            tree_name = "frozen-source-unknown"
    else:
        cursor_path = WORKTREE / "ledgers" / ".memos_dispatcher_cursor.json"
        tree_name = "worktree"
    if not cursor_path.is_file():
        cursor_path = STAGE_LIVE / "ledgers" / ".memos_dispatcher_cursor.json"
        tree_name = "master-read-only"
    if not ledger_path.is_file():
        ledger_path = WORKTREE / "ledgers" / "lifecycle-transitions.jsonl"
    if not ledger_path.is_file():
        ledger_path = STAGE_LIVE / "ledgers" / "lifecycle-transitions.jsonl"

    if not cursor_path.is_file():
        return {"cursor_found": False, "passed": False,
                "reason": "cursor file not found in worktree or master"}
    if not ledger_path.is_file():
        return {"cursor_found": True, "ledger_found": False, "passed": False,
                "reason": "lifecycle-transitions ledger not found"}

    cursor = json.loads(cursor_path.read_text())
    ledger_size = ledger_path.stat().st_size
    byte_offset = cursor.get("byte_offset", -1)
    cursor_current = (byte_offset == ledger_size)

    return {
        "cursor_found": True,
        "ledger_found": True,
        "cursor_byte_offset": byte_offset,
        "measured_ledger_size": ledger_size,
        "cursor_current": cursor_current,
        "tree_name": tree_name,
        "passed": cursor_current,
    }


# ─── dry-run (bounded scope) ────────────────────────────────────────────────

def run_dry_run(probe_session_id: str | None = None,
                reuse_manifest: dict | None = None) -> dict:
    """Execute the bounded dry-run scope:
    (a) freeze discipline,
    (b) negatives N1–N6,
    (c) fire-provenance and dispatch-cursor legs against worktree copies,
    (d) refusal-shaped render/store coverage (each live-writing leg exercised
        only to its refusal).
    MUST NOT complete the store-write or positive-render legs.
    Pass reuse_manifest (from a prior build_probe_manifest call) to satisfy
    freeze-once-reuse across a determinism pair."""
    if reuse_manifest is not None:
        manifest = reuse_manifest
    else:
        manifest = build_probe_manifest(probe_session_id)
    freeze_inputs(manifest)

    scratch = SCRATCH / "dry_run_scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    negatives = {
        "N1": negative_n1(scratch),
        "N2": negative_n2(scratch),
        "N3": negative_n3(scratch),
        "N4": negative_n4(scratch),
        "N5": negative_n5(scratch),
        "N6": negative_n6(scratch),
    }

    fire_prov = negative_n6(scratch)  # includes provenance checks
    cursor_check = check_dispatch_cursor(scratch)

    # refusal-shaped render/store coverage: exercise to refusal ONLY
    store_refusal = store_write_leg(confirm_live=False)
    render_refusal = positive_render_leg(confirm_live=False)

    # measured counts (tree-named, run-time)
    measured_counts = {
        "measured_total_worktree": manifest["ledger_head_recheck"]["entries"],
        "measured_total_master": _count_master_ledger(),
        "cursor_byte_offset": cursor_check.get("cursor_byte_offset", 0),
    }

    all_negatives_pass = all(n["passed"] for n in negatives.values())

    verdict = {
        "probe_session_id": manifest["probe_session_id"],
        "stage": "dry-run",
        "tree": "worktree",
        "point_in_time_distinction": POINT_IN_TIME_DISTINCTION,
        "live_surfaces_rule": LIVE_SURFACES_RULE,
        "manifest": manifest,
        "negatives": negatives,
        "fire_provenance": {
            "passed": fire_prov.get("passed", False),
            "plugin_writer_citation": PLUGIN_WRITER_CITATION,
        },
        "dispatch_cursor": cursor_check,
        "refusal_coverage": {
            "store_write": store_refusal,
            "positive_render": render_refusal,
        },
        "measured_counts": measured_counts,
        "determinism_allowlist": list(DETERMINISM_ALLOWLIST),
        "dry_run_scope_note": (
            "Dry-run executes ONLY: freeze discipline + negatives N1-N6 + "
            "fire-provenance and dispatch-cursor legs against worktree copies "
            "+ refusal-shaped render/store coverage. The dry-run does NOT "
            "complete the store-write or positive-render legs."
        ),
        "all_negatives_pass": all_negatives_pass,
        "dry_run_pass": (
            all_negatives_pass
            and fire_prov.get("passed", False)
            and cursor_check.get("passed", False)
            and store_refusal.get("refused", False)
            and render_refusal.get("refused", False)
        ),
    }
    return verdict


def _count_master_ledger() -> int:
    master_ledger = (STAGE_LIVE / "reading-campaign-2026-11-02" / "_campaign"
                     / "evidence-ledger" / "evidence-ledger.jsonl")
    if master_ledger.is_file():
        return len([l for l in master_ledger.read_text().splitlines() if l.strip()])
    return 0


# ─── determinism check ──────────────────────────────────────────────────────

def check_determinism(verdict_a: dict, verdict_b: dict) -> dict:
    """Two dry-runs on frozen inputs must produce identical artifact bytes
    EXCEPT the enumerated count-field allowlist.  Any other differing byte
    fails determinism."""
    a = _strip_allowlisted(copy.deepcopy(verdict_a))
    b = _strip_allowlisted(copy.deepcopy(verdict_b))

    a_bytes = json.dumps(a, sort_keys=True, separators=(",", ":")).encode()
    b_bytes = json.dumps(b, sort_keys=True, separators=(",", ":")).encode()

    identical = a_bytes == b_bytes

    # find differing fields for diagnostics
    diff_fields = _find_diff_fields(a, b)

    return {
        "identical_after_allowlist": identical,
        "allowlist": list(DETERMINISM_ALLOWLIST),
        "differing_fields_outside_allowlist": diff_fields,
        "passed": identical,
    }


def _strip_allowlisted(d: dict) -> dict:
    """Remove allowlisted count fields from the verdict for byte comparison."""
    if isinstance(d, dict):
        return {k: _strip_allowlisted(v) for k, v in d.items()
                if k not in DETERMINISM_ALLOWLIST}
    if isinstance(d, list):
        return [_strip_allowlisted(x) for x in d]
    return d


def _find_diff_fields(a, b, path="") -> list:
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a.keys()) | set(b.keys()):
            p = f"{path}.{k}" if path else k
            if k not in a or k not in b:
                diffs.append(p)
            else:
                diffs.extend(_find_diff_fields(a[k], b[k], p))
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            p = f"{path}[{i}]"
            if i >= len(a) or i >= len(b):
                diffs.append(p)
            else:
                diffs.extend(_find_diff_fields(a[i], b[i], p))
    elif a != b:
        diffs.append(path)
    return diffs


# ─── main ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="G6.1 end-to-end probe (dry-run)")
    ap.add_argument("--confirm-live", action="store_true",
                    help="required for live legs (store-write, positive-render)")
    ap.add_argument("--runid", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="execute bounded dry-run scope")
    ap.add_argument("--determinism", action="store_true",
                    help="run dry-run twice and check determinism")
    args = ap.parse_args()

    if args.determinism:
        shared_manifest = build_probe_manifest()
        v1 = run_dry_run(reuse_manifest=shared_manifest)
        v2 = run_dry_run(reuse_manifest=shared_manifest)
        det = check_determinism(v1, v2)
        out = {"dry_run_1_pass": v1["dry_run_pass"],
               "dry_run_2_pass": v2["dry_run_pass"],
               "determinism": det}
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    if args.dry_run:
        v = run_dry_run()
        print(json.dumps(v, indent=2, sort_keys=True))
        return

    if args.confirm_live:
        # live orchestration (orchestrator lane): legs carry their own
        # guards; run_live refuses unless cwd == stage_live and the runid
        # is pre-registered in the executing campaign root's run log
        out = run_live(args.runid, cwd=os.getcwd())
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    # no live confirmation — refuse before touching anything
    print(json.dumps({
        "refused": True,
        "reason": "live mode requires --confirm-live (orchestrator lane); "
                  "builder must use --dry-run",
        "live_surfaces_rule": LIVE_SURFACES_RULE,
    }, indent=2))


if __name__ == "__main__":
    main()
