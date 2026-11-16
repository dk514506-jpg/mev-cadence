#!/usr/bin/env python3
"""memos_dispatcher — R1 promotion seam: lifecycle-ledger watch -> memos_push batches.

Seat: BUILDER Ling (inclusionai/ling-3.1-flash, command-code cmd harness) — sandbox
s4-ling2. Stage: V3 S4. Built: 2026-10-29.
Plan of record: V3-ARCHITECT-PLAN §2 R1 (mechanism + fixtures), §5 (manifest
discipline via R12), §7.3 (refusal layering), §8 (forms: MANIFEST per-batch by
memos_dispatcher; CALENDAR/JOURNAL event discipline), §10 stage ladder S4 row.
Charge: MICRO-EVICTION-VAULT-V3.1-2026-10-28 R1 ("polls lifecycle ledger
transitions at disk and enqueues to memos_push. NO edit to lifecycle.py").

MECHANISM (plan §2 R1, verbatim scope):
  * Polls the lifecycle ledger at disk — ledgers/lifecycle-transitions.jsonl,
    the append-only stream written by lifecycle.append_promotions() — through
    a durable cursor ledgers/.memos_dispatcher_cursor.json (byte-offset +
    row-count + seen directive ids; atomic io.safe_write).
  * For each unseen row with kind == "promotion" and to in {pattern, rule,
    calcified}: builds ONE BATCH PER UNSEEN ROW and enqueues it to memos_push
    IN-PROCESS. Unseen retraction directives (directives ledger derived via
    directives.derive_directives_path -> load_directives, deduped on
    directive_id) batch the same way.
  * Per-batch MANIFEST at reports/manifests/batch_<ts>_<seq>.json carrying
    items + named omissions (R12 completeness discipline applied to dispatch:
    across a run, items + refused + errors + omissions == the whole window).
  * R8 staging hold: staging state at cap (or UNKNOWN — fail closed) -> the
    run HOLDS: zero enqueues, zero cursor movement, drain review demanded,
    RUN-LOG + CALENDAR events recorded. Never spills.
  * Refusal layering: memos_push enforces the I3/R14 ownership rule + the R9
    fixture gate + the authorization rule. A PushRefusedError is LOUD: the
    batch is journaled, named in the manifest's refused list, and the cursor
    does NOT advance past it (the promotion stays reachable — when the
    registry resolves or a grant lands, the next poll delivers it). Any other
    engine error also preserves the cursor (at-least-once retry) and fails
    the run loud. Classified-out rows (wrong kind/to) are consumed, named as
    omissions with the classification reason — never enqueued, never crashed.

NO lifecycle.py edit: the design §7 line ("promotions now ALSO enqueue to
memos_push") is superseded by DESIGN-NOTE-2026-10-28-R1-promotion-seam.md;
the supersession is recorded in RUN-LOG (append-only).

  * A10 (CHARGE-S7-ENTROPY-CADENCE-V1 §6): the dispatch run acquires the SHARED
    advisory driver lock (``micro_eviction.cadence.driver_lock`` — one lock file,
    ``ledgers/.driver.lock``) around the cursor read-modify-write. Cursor writes
    stay atomic temp+``os.replace`` writes and now happen entirely UNDER that
    lock. A busy lock is an explicit HOLD payload (no cursor movement, no
    enqueue, journaled), so cadence and the dispatcher can never be concurrent
    drivers.

INERT ON IMPORT. All writes go through micro_eviction.io containment
(safe_write/append_jsonl). Stdlib + campaign modules only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── seat identity (dispatch rule: every new S4 file carries it) ──────────────
SEAT_ID = "BUILDER-Ling-s4-ling2"
SEAT_STAGE = "V3-S4"
SEAT_SANDBOX = "/tmp/seat-logs/s4-ling2/campaigns/bench/micro-eviction"

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from micro_eviction import io as mev_io          # noqa: E402
from micro_eviction import directives as dirmod  # noqa: E402
import engraphis_sink                            # noqa: E402  (STAGING_LIMITS single source, R8)
import memos_push                                # noqa: E402

PROMOTION_KIND = "promotion"
PROMOTION_TO = frozenset({"pattern", "rule", "calcified"})
CURSOR_NAME = ".memos_dispatcher_cursor.json"
MANIFEST_DIR = ("reports", "manifests")
MANIFEST_PREFIX = "batch_"
CALENDAR_HOLD_KIND = "staging_cap_hold"
CALENDAR_DRAIN_KIND = "drain_review_demanded"

# staging state basis labels (honesty: declared substrate vs unknown)
BASIS_UNKNOWN = "unknown"
BASIS_DECLARED = "declared"

# G-F (R-B3 / A-ROOT): sanctioned C15 vault root pinned in the charge body.
# Candidates digital_brain/ops_command (plugin default) and campaign reports/
# containment are recorded considered-and-rejected — never used here.
PINNED_VAULT_ROOT = Path.home() / "icm-knowledge-vault" / "roof"


class LedgerShrunkError(RuntimeError):
    """The append-only transitions ledger shrank under the cursor — corruption."""


class AppendOnlyViolation(RuntimeError):
    """The ledger was edited in place (blank line / non-boundary cursor)."""


# ── paths ────────────────────────────────────────────────────────────────────

def transitions_path(campaign_root) -> Path:
    return Path(campaign_root) / "ledgers" / "lifecycle-transitions.jsonl"


def cursor_path(campaign_root) -> Path:
    return Path(campaign_root) / "ledgers" / CURSOR_NAME


def calcified_path(campaign_root) -> Path:
    return Path(campaign_root) / "calcified.jsonl"


def manifest_dir(campaign_root) -> Path:
    return Path(campaign_root).joinpath(*MANIFEST_DIR)


# ── durable cursor (byte-offset / row-count / seen directive ids) ────────────

def zero_cursor() -> dict:
    return {"byte_offset": 0, "row_count": 0, "seen_directive_ids": [],
            "last_run_ts": None, "last_run": None}


def load_cursor(path) -> dict:
    p = Path(path)
    if not p.is_file():
        return zero_cursor()
    cur = json.loads(p.read_text(encoding="utf-8"))
    out = zero_cursor()
    out.update({k: cur[k] for k in out if k in cur})
    out["seen_directive_ids"] = list(cur.get("seen_directive_ids") or [])
    return out


def save_cursor(path, cursor: dict) -> None:
    mev_io.safe_write(path, json.dumps(cursor, ensure_ascii=False, sort_keys=True,
                                       indent=1) + "\n")


def save_cursor_if_changed(path, cursor: dict) -> bool:
    """Byte-identical discipline: write only when durable fields change."""
    try:
        existing = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    durable = ("byte_offset", "row_count", "seen_directive_ids")
    if existing is not None and all(existing.get(k) == cursor.get(k) for k in durable):
        return False
    save_cursor(path, cursor)
    return True


# ── window reader (byte-offset anchored, append-only defended) ───────────────

def read_unseen(path, offset: int) -> tuple[list, list, int]:
    """Return (rows, spans, size) for the unread window from `offset`.

    spans[i] is the byte length of row i's line (incl. its newline) so the
    cursor can advance per-row. Fail closed: a shrunk ledger or a cursor that
    is not on a line boundary is an append-only violation, never a silent
    re-read."""
    p = Path(path)
    size = p.stat().st_size if p.is_file() else 0
    if offset > size:
        raise LedgerShrunkError(f"{path} shrank under the cursor ({offset} > {size})")
    if offset == size:
        return [], [], size
    with open(p, "rb") as f:
        if offset > 0:
            f.seek(offset - 1)
            if f.read(1) != b"\n":
                raise AppendOnlyViolation(
                    f"cursor offset {offset} is not a line boundary in {path}")
        f.seek(offset)
        data = f.read()
    rows, spans = [], []
    for raw in data.splitlines(keepends=True):
        if not raw.strip():
            raise AppendOnlyViolation(f"blank line in append-only ledger window {p}")
        rows.append(json.loads(raw.decode("utf-8")))
        spans.append(len(raw))
    return rows, spans, size


# ── classification (right reason, never a crash) ─────────────────────────────

def classify_row(row: dict) -> tuple[str, str]:
    """Return ("enqueue", "") or ("skip", reason). The negative fixture asserts
    the reason is a classification, not an exception.

    Fail-closed hardening: a promotion row without a topic_hash or without a
    non-empty members list cannot build an honest item (no referent to push) —
    it classifies OUT with the reason named, never enqueues a referent-less
    item, never crashes the run."""
    kind = row.get("kind")
    to = row.get("to")
    if kind != PROMOTION_KIND:
        return "skip", f"classification: kind={kind!r} != {PROMOTION_KIND!r}"
    if to not in PROMOTION_TO:
        return "skip", (f"classification: to={to!r} not in "
                        f"{sorted(PROMOTION_TO)}")
    if not row.get("topic_hash"):
        return "skip", ("classification: promotion row missing topic_hash "
                        "(fail closed — no referent to push)")
    members = row.get("members")
    if not isinstance(members, list) or not members:
        return "skip", ("classification: promotion row missing/empty members "
                        "(fail closed — no referent to push)")
    return "enqueue", ""


# ── content enrichment (disk-local only: campaign ledgers + calcified store) ─

def build_records_index(campaign_root) -> tuple[dict, dict]:
    """{record_id: row} from session ledgers + {topic_hash: row} from the
    calcified store. Same exclusions as lifecycle.load_all_records."""
    root = Path(campaign_root)
    idx: dict[str, dict] = {}
    led = root / "ledgers"
    if led.is_dir():
        for p in sorted(led.glob("*.jsonl")):
            if p.name in ("lifecycle-transitions.jsonl", "conflict-queue.jsonl"):
                continue
            if p.name.endswith(".spine.jsonl") or p.name.endswith("transitions.jsonl"):
                continue
            for row in mev_io.load_jsonl(p):
                if "content" in row and "record_id" in row:
                    idx.setdefault(row["record_id"], row)
    calc: dict[str, dict] = {}
    cp = root / "calcified.jsonl"
    if cp.is_file():
        for row in mev_io.load_jsonl(cp):
            if row.get("topic_hash"):
                calc.setdefault(row["topic_hash"], row)
    return idx, calc


def build_promotion_item(row: dict, rec_index: dict, calc_index: dict) -> dict:
    th = row.get("topic_hash")
    to = row.get("to")
    content, source, extra = None, "row-only", {}
    if to == "calcified":
        c = calc_index.get(th)
        if c:
            content = c.get("rule_content")
            source = "calcified-store"
            extra = {"consensus_core": c.get("consensus_core"),
                     "member_quotes": c.get("member_quotes"),
                     "calcified_via": c.get("via")}
    if content is None:
        members = [m for m in (row.get("members") or []) if m in rec_index]
        if members:
            content = "\n\n".join(rec_index[m]["content"] for m in members)
            source = "ledger-join"
    if not isinstance(content, str) or not content.strip():
        content = (f"promotion to={to} topic_hash={th} "
                   f"members={len(row.get('members') or [])} "
                   f"sessions={len(row.get('sessions') or [])}")
        source = "row-only"
    item = {
        "item_kind": "promotion",
        "topic_hash": th,
        "to": to,
        "type": row.get("type"),
        "record_ids": list(row.get("members") or []),
        "sessions": list(row.get("sessions") or []),
        "counts": row.get("counts"),
        "evidence": row.get("evidence"),
        "content": content,
        "content_source": source,
        "source_row": {"kind": PROMOTION_KIND, "to": to, "topic_hash": th},
    }
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


def build_directive_item(d: dict) -> dict:
    did = d.get("directive_id")
    dtype = d.get("type")
    th = d.get("target_topic_hash")
    reason = d.get("reason")
    return {
        "item_kind": "retraction-directive",
        "directive_id": did,
        "directive_type": dtype,
        "target_topic_hash": th,
        "target_record_ids": list(d.get("target_record_ids") or []),
        "reason": reason,
        "source": d.get("source"),
        "issued_at": d.get("issued_at"),
        "replacement_topic_hash": d.get("replacement_topic_hash"),
        "content": (f"retraction directive {did} ({dtype}): retract topic "
                    f"{th} — {reason}"),
        "content_source": "directive-ledger",
        "source_row": {"kind": "retraction-directive", "directive_id": did},
    }


# ── R8 staging cap (fail closed on unknown) ──────────────────────────────────

def staging_at_cap(staging_state) -> tuple[bool, str, dict]:
    """Return (at_cap, basis, state). UNKNOWN state (None) HOLDS — fail closed;
    a filled or unreadable staging workspace never spills (R8)."""
    if staging_state is None:
        return True, BASIS_UNKNOWN, {"records": None, "bytes": None}
    if callable(staging_state):
        staging_state = staging_state()
    state = {"records": int((staging_state or {}).get("records", 0) or 0),
             "bytes": int((staging_state or {}).get("bytes", 0) or 0)}
    limits = engraphis_sink.STAGING_LIMITS
    at_cap = (state["records"] >= int(limits["records"])
              or state["bytes"] >= int(limits["bytes"]))
    return at_cap, BASIS_DECLARED, state


# ── event appenders (JOURNAL = RUN-LOG.md; CALENDAR = longtail/calendar.jsonl) ─

def _append_runlog(runlog_path, text: str) -> None:
    if runlog_path:
        mev_io.safe_write(runlog_path, text, append=True)


def _append_calendar(calendar_path, row: dict) -> None:
    if calendar_path:
        mev_io.append_jsonl(calendar_path, row)


def _hold_events(state: dict, limits: dict, basis: str, unseen_rows: int,
                 unseen_directives: int) -> list[dict]:
    ts = time.time()
    return [
        {"kind": CALENDAR_HOLD_KIND, "ts": ts, "workspace": "mev-staging",
         "state": state, "limits": limits, "basis": basis,
         "unseen_rows_held": unseen_rows,
         "unseen_directives_held": unseen_directives,
         "via": "memos_dispatcher R8"},
        {"kind": CALENDAR_DRAIN_KIND, "ts": ts, "workspace": "mev-staging",
         "via": "r8_cap", "demand": "drain review — staging drains ONLY via "
         "gate review (R8); dispatcher holds, never spills"},
    ]


def _hold_runlog_lines(state: dict, limits: dict, basis: str) -> list[str]:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        f"## [{ts}] memos_dispatcher (S4) — STAGING CAP HOLD (R8)",
        f"- staging_state: records={state['records']} bytes={state['bytes']}"
        f" limits={limits['records']} records / {limits['bytes']} bytes"
        f" basis={basis}",
        "- drain review DEMANDED — staging drains ONLY via gate review; the"
        " dispatcher HOLDS (no enqueue, no cursor movement, never spills)",
    ]


def _refusal_lines(batch, item, exc) -> list[str]:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ident = item.get("topic_hash") or item.get("directive_id")
    return [
        f"## [{ts}] memos_dispatcher (S4) — BATCH REFUSED (loud, journaled)",
        f"- batch: {batch['batch_id']} item: {item.get('item_kind')} ref: {ident}",
        f"- reason: {exc.reason} — {exc.detail}",
        "- cursor NOT advanced past this row: the promotion/directive stays"
        " reachable; the next poll retries after the registry resolves or a"
        " Dallas grant lands (R14/G1)",
    ]


def _error_lines(batch, item, exc) -> list[str]:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ident = item.get("topic_hash") or item.get("directive_id")
    return [
        f"## [{ts}] memos_dispatcher (S4) — BATCH ENGINE ERROR (loud)",
        f"- batch: {batch['batch_id']} item: {item.get('item_kind')} ref: {ident}",
        f"- error: {type(exc).__name__}: {exc}",
        "- cursor preserved for at-least-once retry",
    ]


# ── the dispatch run ─────────────────────────────────────────────────────────

def _lock_refusal(exc, runlog_path) -> dict:
    """A concurrent driver holds the shared lock — refuse, journal, no cursor move.

    A10 (CHARGE-S7-ENTROPY-CADENCE-V1 §6): cadence and the dispatcher share ONE
    advisory lock. Two concurrent drivers over the same cursors are forbidden, so
    a busy lock is a HOLD (nothing read-modify-written), never a race.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"## [{ts}] memos_dispatcher (S4) — DRIVER LOCK BUSY (A10)",
        f"- lock: `{getattr(exc, 'path', None)}` holder: {getattr(exc, 'holder', {})}",
        f"- stale: {getattr(exc, 'stale', False)} (a stale holder is reported,"
        " never auto-broken)",
        "- no cursor read/write, no enqueue: cadence and the dispatcher are never"
        " concurrent drivers (A1)",
    ]
    try:
        mev_io.allow(Path(runlog_path).parent)
        mev_io.safe_write(runlog_path, "\n".join(lines) + "\n", append=True)
    except OSError:
        pass
    return {
        "held": True, "hold_reason": "driver_lock_busy",
        "staging_state_basis": BASIS_UNKNOWN,
        "staging_state": {"records": None, "bytes": None},
        "limits": dict(engraphis_sink.STAGING_LIMITS),
        "window": {"rows": None, "directives": None, "byte_offset": None},
        "batches": [], "manifests": [], "enqueued": [], "refused": [],
        "errors": [], "skipped": [], "cursor": None, "cursor_advanced": False,
        "runlog_events": lines, "calendar_events": [],
        "driver_lock": {"path": str(getattr(exc, "path", "")),
                        "holder": getattr(exc, "holder", {}),
                        "stale": bool(getattr(exc, "stale", False))},
        "verdict": "held",
    }


def dispatch(*, campaign_root, transitions=None, directives=None,
             calcified=None, manifests=None, runlog=None, calendar=None,
             staging_state=None, push_fn=None, now_fn=None, seq: int = 0,
             use_lock: bool = True, lock_timeout_s: float = 0.0,
             sleep_fn=None) -> dict:
    """One poll under the SHARED driver lock (A10).

    The lock is the same file-lock cadence acquires (``micro_eviction.cadence``
    ``driver_lock``); cursor writes stay atomic ``os.replace`` writes (via
    ``mev_io.safe_write``) and now happen entirely UNDER the lock. A busy lock
    returns an explicit hold payload instead of racing. ``use_lock=False`` is
    only for a caller that already holds the lock (never a second driver).
    """
    if not use_lock:
        return _dispatch_unlocked(
            campaign_root=campaign_root, transitions=transitions,
            directives=directives, calcified=calcified, manifests=manifests,
            runlog=runlog, calendar=calendar, staging_state=staging_state,
            push_fn=push_fn, now_fn=now_fn, seq=seq)
    from micro_eviction import cadence as _cadence  # lazy: no import cycle

    root = Path(campaign_root)
    runlog_path = Path(runlog) if runlog else root / "RUN-LOG.md"
    try:
        with _cadence.driver_lock(root, timeout_s=lock_timeout_s,
                                  sleep_fn=sleep_fn or time.sleep,
                                  seat=SEAT_ID):
            return _dispatch_unlocked(
                campaign_root=campaign_root, transitions=transitions,
                directives=directives, calcified=calcified, manifests=manifests,
                runlog=runlog, calendar=calendar, staging_state=staging_state,
                push_fn=push_fn, now_fn=now_fn, seq=seq)
    except _cadence.DriverLockBusy as exc:
        return _lock_refusal(exc, runlog_path)


# ── G-F: C15 renderer binding (post-promotion phase) ───────────────────────
# The C15 renderer (obsidian_mirror) is bound into the promotion poll loop's
# POST-PROMOTION phase (eng m2: one phase, reusing that pass's cursor). The
# batch manifests gain rendered/not_rendered (AUTHORIZED extension) with the
# reconciliation invariant rendered + not_rendered == items.

# Run-6 F6: item identity key — uniform across promotions and retraction
# directives.  Same key used by verify_g3_delivery for set-identity reconciliation.
# Run-7 N4: deterministic fallback (content-hash) for identity-less items —
# IDENTICAL implementation in verify_g3_delivery.py (gf_proof).
def _item_identity_key(item) -> str:
    """Stable identity key for one item record (run-6 F6; run-7 N4;
    run-8 NEW-3/NEW-4ii).

    Promotions carry ``topic_hash``; retraction-directives carry
    ``directive_id``.  The key is ``item_kind:topic_hash`` or
    ``item_kind:directive_id``.  An item lacking both — or a non-dict item —
    gets a deterministic fallback: ``content:`` + sha256_12 of the item's
    canonical JSON (sort_keys, compact), never ``id(item)``/memory addresses.
    NEW-3: dispatcher-stamped fields (``item_identity_key``,
    ``render_verdict_granularity``) are excluded from the fallback hash so
    the key is stable across the dispatcher's stamping round-trip.
    NEW-4ii: non-serializable items fall back to ``repr:`` + sha256_12 of
    ``repr(item)`` instead of raising TypeError.
    Identical implementation in memos_dispatcher.py and verify_g3_delivery.py
    (run-7 N4; run-8 NEW-3/NEW-4ii).
    """
    if isinstance(item, dict):
        kind = item.get("item_kind") or "unknown"
        ident = item.get("topic_hash") or item.get("directive_id") or ""
        if ident:
            return f"{kind}:{ident}"
    payload_item = item
    if isinstance(payload_item, dict):
        payload_item = {k: v for k, v in payload_item.items()
                        if k not in ("item_identity_key", "render_verdict_granularity")}
    try:
        payload = json.dumps(payload_item, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "repr:" + hashlib.sha256(repr(item).encode("utf-8")).hexdigest()[:12]
    return "content:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _self_sha256_12() -> str:
    """sha256_12 of THIS file's bytes — the §5 receipt binds the grant to the
    exact dispatcher code version (run-7 N1/N2)."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def _check_c15_receipt(campaign_root) -> tuple:
    """Run-6 F2 + run-7 N1/N2: §5 receipt guard.  Returns (ok, reason).

    The receipt at reports/c15_render_receipt.json (campaign_root-relative)
    must carry:
      runid                — non-empty str,
      principal_go         — true,
      vault_root           — str(PINNED_VAULT_ROOT),
      dispatcher_sha256_12 — sha256_12 of THIS memos_dispatcher.py file
                             (self-hash at runtime, read at check time).
    A receipt written for older code (stale sha) is INVALID -> render skipped
    -> every item reconciles not_rendered -> C16 RED.  The §5 orchestrator
    creates the receipt (rows 6-7) and DELETES it at §5 close (rollback
    removes the grant).  This is a NEW mechanism — NOT a resurrection of
    live_grant (R-B2 stands)."""
    receipt_path = Path(campaign_root) / "reports" / "c15_render_receipt.json"
    if not receipt_path.is_file():
        return False, "c15-receipt-missing"
    try:
        data = json.loads(receipt_path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return False, "c15-receipt-invalid"
    if not isinstance(data, dict):
        return False, "c15-receipt-invalid"
    if not isinstance(data.get("runid"), str) or not data["runid"]:
        return False, "c15-receipt-invalid"
    if data.get("principal_go") is not True:
        return False, "c15-receipt-invalid"
    if data.get("vault_root") != str(PINNED_VAULT_ROOT):
        return False, "c15-receipt-vault-root-mismatch"
    if data.get("dispatcher_sha256_12") != _self_sha256_12():
        return False, "c15-receipt-stale-sha"
    return True, ""


def _run_c15_renderer(campaign_root) -> dict:
    """Run the C15 renderer (obsidian_mirror) once against the pinned vault
    root (R-B3).  Run-6 F2: BEFORE calling render, require the §5 receipt
    (reports/c15_render_receipt.json).  Absent/invalid receipt -> render is
    SKIPPED (fail-closed: all items reconcile not_rendered -> C16 gate RED).
    Fail-closed: a render error is reported (ok=False, sections empty) so
    every item reconciles as not_rendered and the D-2 gate trips."""
    receipt_ok, receipt_reason = _check_c15_receipt(campaign_root)
    if not receipt_ok:
        return {"ok": False, "skipped": receipt_reason, "sections": {}, "notes": []}
    import obsidian_mirror  # lazy: C15 renderer, no import cycle
    try:
        return obsidian_mirror.render(vault_root=PINNED_VAULT_ROOT,
                                      campaign_root=Path(campaign_root))
    except Exception as e:  # loud fail-closed; delivery never claims success
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "sections": {}, "notes": []}


def _item_rendered(item, render_report) -> bool:
    """Whether the C15 render delivered this item to the vault mirror.

    Promotion items (captured memories) land iff the promotions section
    rendered. Retraction directives are NOT rendered by C15 (not captured
    memories) — the honest accounting keeps them not_rendered."""
    if isinstance(item, dict) and item.get("item_kind") == "retraction-directive":
        return False
    sections = (render_report or {}).get("sections") or {}
    return sections.get("promotions") == "ok"


def _reconcile_batch_manifests(manifest_paths, render_report) -> list:
    """Add rendered/not_rendered to each batch manifest (AUTHORIZED extension).
    Invariant: rendered + not_rendered == items.  Run-6 F6: each manifest gains
    ``render_verdict_granularity: "batch-global"`` (the sections verdict is
    batch-global — obsidian_mirror exposes no per-item evidence; 1-of-10
    delivered marks 10 rendered) and each item record carries its identity key.
    Fail-closed: an unparseable manifest is reported and left un-reconciled
    (the gate reads it RED)."""
    reconciled = []
    for m_path in manifest_paths:
        p = Path(m_path)
        try:
            manifest = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            reconciled.append({"manifest": str(p),
                               "error": f"{type(e).__name__}: {e}"})
            continue
        items = manifest.get("items") or []
        # Stamp identity keys on every item record (run-6 F6).
        for it in items:
            if isinstance(it, dict):
                it["item_identity_key"] = _item_identity_key(it)
        rendered = [it for it in items if _item_rendered(it, render_report)]
        not_rendered = [it for it in items if not _item_rendered(it, render_report)]
        manifest["rendered"] = rendered
        manifest["not_rendered"] = not_rendered
        # Run-6 F6: honest granularity disclosure (obsidian_mirror's sections
        # verdict is batch-global — no per-item evidence is exposed).
        manifest["render_verdict_granularity"] = "batch-global"
        try:
            mev_io.safe_write(p, json.dumps(manifest, ensure_ascii=False,
                                           sort_keys=True, indent=1) + "\n")
            reconciled.append({"manifest": str(p), "items": len(items),
                               "rendered": len(rendered),
                               "not_rendered": len(not_rendered),
                               "reconciles": (len(rendered) + len(not_rendered)
                                              == len(items)),
                               "render_verdict_granularity": "batch-global"})
        except OSError as e:
            reconciled.append({"manifest": str(p),
                               "error": f"{type(e).__name__}: {e}"})
    return reconciled


def _dispatch_unlocked(*, campaign_root, transitions=None, directives=None,
                       calcified=None, manifests=None, runlog=None,
                       calendar=None, staging_state=None, push_fn=None,
                       now_fn=None, seq: int = 0) -> dict:
    """One poll: classify the unseen window, batch per qualifying item, enqueue
    in-process to memos_push, emit per-batch manifests, advance the durable
    cursor only past rows with a recorded terminal outcome (enqueued or
    classified-out). Refused/errored rows stay reachable (cursor preserved).

    Path params default to the campaign-root locations of record:
    transitions=ledgers/lifecycle-transitions.jsonl,
    directives=ledgers/retraction-directives.jsonl (derived from calcified),
    calcified=calcified.jsonl, manifests=reports/manifests/,
    runlog=RUN-LOG.md, calendar=longtail/calendar.jsonl.

    staging_state: dict {"records","bytes"} | callable | None (None = unknown
    -> HOLD, fail closed). push_fn: callable(batch)->report (default:
    memos_push.push in-process with this campaign_root)."""
    root = Path(campaign_root)
    t_path = Path(transitions) if transitions else transitions_path(root)
    calc_p = Path(calcified) if calcified else calcified_path(root)
    d_path = Path(directives) if directives else dirmod.derive_directives_path(calc_p)
    m_dir = Path(manifests) if manifests else manifest_dir(root)
    runlog_path = Path(runlog) if runlog else None
    calendar_path = Path(calendar) if calendar else None
    cur_path = cursor_path(root)

    cursor = load_cursor(cur_path)
    start_offset = int(cursor["byte_offset"])

    # ── R8 staging hold (fail closed on unknown) ──
    at_cap, basis, state = staging_at_cap(staging_state)
    if at_cap:
        rows_win, _, _ = read_unseen(t_path, start_offset)
        dirs_win = [d for d in dirmod.load_directives(d_path)
                    if d.get("directive_id") not in set(cursor.get("seen_directive_ids") or [])]
        limits = dict(engraphis_sink.STAGING_LIMITS)
        events = _hold_events(state, limits, basis, len(rows_win), len(dirs_win))
        lines = _hold_runlog_lines(state, limits, basis)
        _append_runlog(runlog_path, "\n".join(lines) + "\n")
        for ev in events:
            _append_calendar(calendar_path, ev)
        return {"held": True, "hold_reason": "staging_cap",
                "staging_state_basis": basis, "staging_state": state,
                "limits": limits,
                "window": {"rows": len(rows_win), "directives": len(dirs_win),
                           "byte_offset": start_offset},
                "batches": [], "manifests": [], "enqueued": [], "refused": [],
                "errors": [], "skipped": [], "cursor": cursor,
                "cursor_advanced": False, "runlog_events": lines,
                "calendar_events": events, "verdict": "held"}

    # ── unseen window: promotion rows + retraction directives ──
    rows, spans, size = read_unseen(t_path, start_offset)
    directives = dirmod.load_directives(d_path)
    seen_ids = set(cursor.get("seen_directive_ids") or [])
    unseen_directives = [d for d in directives
                         if d.get("directive_id") not in seen_ids]
    rec_index, calc_index = build_records_index(root)

    # classify the whole window first (right reason per row)
    promotion_items, skipped = [], []
    for row, span in zip(rows, spans):
        verdict, reason = classify_row(row)
        if verdict == "enqueue":
            promotion_items.append(
                (row, span, build_promotion_item(row, rec_index, calc_index)))
        else:
            skipped.append({"row_ref": {"kind": row.get("kind"),
                                        "to": row.get("to"),
                                        "topic_hash": row.get("topic_hash")},
                            "reason": reason})
            cursor["byte_offset"] += span  # classified-out = consumed
            cursor["row_count"] += 1

    directive_items = [(d, build_directive_item(d)) for d in unseen_directives]

    # ── per-item batches -> manifest -> in-process enqueue ──
    if push_fn is None:
        _charge = root.parent.parent.parent / "charges" / "MICRO-EVICTION-VAULT-V3-2026-10-28.md"
        def push_fn(batch, _root=root, _charge=_charge):
            return memos_push.push(batch["items"], campaign_root=str(_root),
                                   charge_path=str(_charge))

    batches, manifests, refused, errors, enqueued = [], [], [], [], []
    new_seen_ids = list(cursor.get("seen_directive_ids") or [])
    run_verdict = "ok"
    ts_file = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _emit(seq_no: int, item: dict, outcome: dict) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        batch_id = f"mev-batch-{ts_file}-{seq_no:04d}"
        manifest = {
            "batch_id": batch_id,
            "ts": ts,
            "seq": seq_no,
            "items": [item] if outcome["status"] == "enqueued" else [],
            "refused": ([{"item": item, "reason": outcome.get("reason"),
                          "detail": outcome.get("detail")}]
                        if outcome["status"] == "refused" else []),
            "errors": ([{"item": item, "error": outcome.get("error")}]
                       if outcome["status"] == "error" else []),
            "omissions": skipped,
            "staging_state_basis": basis,
            "engine": "memos_push.push (in-process, R1)",
            "cursor_after": dict(cursor),
        }
        m_path = m_dir / f"{MANIFEST_PREFIX}{ts_file}_{seq_no:04d}.json"
        mev_io.safe_write(m_path, json.dumps(manifest, ensure_ascii=False,
                                             sort_keys=True, indent=1) + "\n")
        manifests.append(str(m_path))
        batches.append(batch_id)
        return str(m_path)

    def _consume_promotion(row, span, item, seq_no: int) -> None:
        nonlocal run_verdict
        batch = {"batch_id": f"mev-batch-{ts_file}-{seq_no:04d}",
                 "ts": ts_file, "seq": seq_no, "items": [item],
                 "staging_state_basis": basis, "cursor_before": dict(cursor)}
        try:
            report = push_fn(batch)
        except memos_push.PushRefusedError as e:
            refused.append({"item": item, "reason": e.reason, "detail": e.detail})
            _append_runlog(runlog_path, "\n".join(_refusal_lines(batch, item, e)) + "\n")
            _emit(seq_no, item, {"status": "refused", "reason": e.reason,
                                 "detail": e.detail})
            return  # cursor NOT advanced: the promotion stays reachable
        except Exception as e:  # loud fail; cursor preserved for retry
            errors.append({"item": item, "error": f"{type(e).__name__}: {e}"})
            _append_runlog(runlog_path, "\n".join(_error_lines(batch, item, e)) + "\n")
            _emit(seq_no, item, {"status": "error",
                                 "error": f"{type(e).__name__}: {e}"})
            run_verdict = "error"
            raise RuntimeError(
                f"memos_push engine error on batch {batch['batch_id']} "
                f"(cursor preserved for retry): {type(e).__name__}: {e}") from e
        enqueued.append({"item": item, "push": report})
        _emit(seq_no, item, {"status": "enqueued", "push": report})
        cursor["byte_offset"] += span
        cursor["row_count"] += 1

    def _consume_directive(d, item, seq_no: int) -> None:
        nonlocal run_verdict
        batch = {"batch_id": f"mev-batch-{ts_file}-{seq_no:04d}",
                 "ts": ts_file, "seq": seq_no, "items": [item],
                 "staging_state_basis": basis, "cursor_before": dict(cursor)}
        try:
            report = push_fn(batch)
        except memos_push.PushRefusedError as e:
            refused.append({"item": item, "reason": e.reason, "detail": e.detail})
            _append_runlog(runlog_path, "\n".join(_refusal_lines(batch, item, e)) + "\n")
            _emit(seq_no, item, {"status": "refused", "reason": e.reason,
                                 "detail": e.detail})
            return
        except Exception as e:
            errors.append({"item": item, "error": f"{type(e).__name__}: {e}"})
            _append_runlog(runlog_path, "\n".join(_error_lines(batch, item, e)) + "\n")
            _emit(seq_no, item, {"status": "error",
                                 "error": f"{type(e).__name__}: {e}"})
            run_verdict = "error"
            raise RuntimeError(
                f"memos_push engine error on batch {batch['batch_id']} "
                f"(cursor preserved for retry)") from e
        enqueued.append({"item": item, "push": report})
        _emit(seq_no, item, {"status": "enqueued", "push": report})
        new_seen_ids.append(item["directive_id"])

    try:
        seq_no = seq
        for row, span, item in promotion_items:
            seq_no += 1
            _consume_promotion(row, span, item, seq_no)
        for d, item in directive_items:
            seq_no += 1
            _consume_directive(d, item, seq_no)
    finally:
        cursor["seen_directive_ids"] = sorted(
            set(new_seen_ids) | set(cursor.get("seen_directive_ids") or []))
        cursor["last_run_ts"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        cursor["last_run"] = {"verdict": run_verdict, "enqueued": len(enqueued),
                              "refused": len(refused), "errors": len(errors),
                              "skipped": len(skipped)}
        save_cursor_if_changed(cur_path, cursor)

    # ── G-F post-promotion phase: C15 renderer binding + manifest reconciliation
    # One phase, reusing this pass's cursor (ledgers/.memos_dispatcher_cursor.json).
    # The C15 renderer (obsidian_mirror) delivers the estate to the pinned ICM
    # vault root (R-B3); each batch manifest then gains rendered/not_rendered
    # (reconciliation invariant rendered + not_rendered == items) — the C16
    # delivery gate (verify_g3_delivery.py) reads that, NOT the fire report.
    render_report = None
    reconciled = []
    if promotion_items or directive_items:
        render_report = _run_c15_renderer(root)
        reconciled = _reconcile_batch_manifests(manifests, render_report)

    return {"held": False, "hold_reason": None, "staging_state_basis": basis,
            "staging_state": state,
            "window": {"rows": len(rows), "directives": len(unseen_directives),
                       "byte_offset": start_offset, "ledger_size": size},
            "batches": batches, "manifests": manifests,
            "enqueued": enqueued, "refused": refused, "errors": errors,
            "skipped": skipped, "cursor": cursor,
            "cursor_advanced": cursor["byte_offset"] != start_offset,
            "runlog_events": [], "calendar_events": [], "verdict": run_verdict,
            "render": render_report, "reconciled": reconciled}


__all__ = [
    "SEAT_ID", "PROMOTION_KIND", "PROMOTION_TO", "CURSOR_NAME",
    "BASIS_UNKNOWN", "BASIS_DECLARED", "PINNED_VAULT_ROOT",
    "LedgerShrunkError", "AppendOnlyViolation",
    "transitions_path", "cursor_path", "calcified_path", "manifest_dir",
    "zero_cursor", "load_cursor", "save_cursor", "save_cursor_if_changed",
    "read_unseen", "classify_row", "build_records_index", "build_promotion_item",
    "build_directive_item", "staging_at_cap", "dispatch",
]
