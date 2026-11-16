"""engraphis_sink — R15/R3/R8 Engraphis ingest wrapper (S1 deliverable).

Wraps a direct MemoryService per workspace:
  * R15 idempotency — query-then-skip on metadata `record_id` match per
    workspace. `resolve_conflicts` is pinned False on the no-op path
    (NOOP_RESOLVE_CONFLICTS); the first ingest keeps the skill's working
    default (True).
  * R3 / charge-law correction (a) — TRUST_FLIP is an IN-PLACE UPDATE of
    provenance/metadata via the direct-store UPDATE path (the trust-gate-approval
    recipe), never a re-ingest: a re-ingest mints a new store row id and can
    never flip trust on the same record. `record_id` is preserved. The flip is
    journaled; if the update path is unavailable the failure is LOUD and
    journaled (no silent drop). Post-condition on read-back: the row's
    provenance passes `prompt_eligible`.
  * R8 staging cap — 5,000 records / 50 MB hard cap; on cap the sink HOLDS
    (no ingest), demands a drain review, and journals both events. Never spills.
  * I3/R14 ownership gate — mutating operations refuse when the induction
    ownership is ambiguous (not exactly one owner engine with a boolean
    effective flag).

Provenance carriers travel in `metadata=`, never as ingest kwargs. The private
`_ingress` kwarg stays unused (`source='agent'`).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Adopted numbers (V3.1 / plan §11): mev-staging hard caps.
STAGING_LIMITS = {"records": 5000, "bytes": 52428800}

# R15: resolve_conflicts pinned False on the no-op path; first ingest keeps the
# skill's working default (True).
NOOP_RESOLVE_CONFLICTS = False
FIRST_INGEST_RESOLVE_CONFLICTS = True

SOURCE = "agent"  # skill rule; _ingress stays unused

METADATA_CARRIERS = (
    "record_id", "session_id", "bounds", "type", "topic_hash",
    "capture_sha", "authoring_tier", "supersedes",
)

# ── spark mode (CHARGE-REVERIE-V1 §2/§3; ADDITIVE — capture carriers untouched) ─
#
# A spark is a `reverie` record minted into the PROVISIONAL `reverie-staging`
# workspace and promoted (dream pass only) into the `reverie-permanent` tier.
# Spark provenance travels in metadata exactly the way capture provenance does;
# the carrier tuple is a superset so `derived_from`/`form`/`spark_id` survive
# the ingest round-trip.
SPARK_FORM = "reverie"
SPARK_TIERS = {
    "PROVISIONAL": "reverie-staging",
    "PERMANENT": "reverie-permanent",
}
SPARK_METADATA_CARRIERS = METADATA_CARRIERS + (
    "form", "spark_id", "derived_from", "sparks_from", "provocation",
    "synthesis", "provenance", "status", "tier", "weight", "is_immune",
    "corroboration", "context", "promotion", "marking",
)

TRUST_FLIP_EVENT = "trust_flip"


class SinkError(RuntimeError):
    """Base class for loud sink refusals."""


class ProvenanceBindingError(SinkError):
    """Capture sha mismatch — provenance binding refused (R15)."""


class OwnershipAmbiguousError(SinkError):
    """Induction ownership is not exactly one engine (I3/R14)."""


class StagingCapHeld(SinkError):
    """Staging workspace at cap — ingest held, drain review demanded (R8)."""


class TrustFlipUnavailable(SinkError):
    """The direct-store trust-update path is unavailable — loud failure (G4)."""


class RecordNotFoundError(SinkError):
    """No stored record with the given record_id in the workspace."""


class SparkProvenanceError(SinkError):
    """A spark record failed its provenance binding (form/spark_id/derived_from)."""


def resolve_ownership(ownership: Any) -> str:
    """Return the single owner engine, or raise OwnershipAmbiguousError (I3).

    Exactly one named engine AND a boolean `effective` flag is required — the
    same rule memos_push enforces (R14). `unresolved-gap` (G1's live state),
    zero engines, or two engines all refuse.
    """
    if not isinstance(ownership, dict):
        raise OwnershipAmbiguousError("ownership flag missing (I3: exactly one engine)")
    owner = ownership.get("owner_engine")
    effective = ownership.get("effective")
    if isinstance(owner, (list, tuple, set)):
        engines = [e for e in owner if e]
        if len(engines) != 1:
            raise OwnershipAmbiguousError(
                f"ownership ambiguous: {len(engines)} engines (I3: exactly one)"
            )
        owner = engines[0]
    if not owner or not isinstance(owner, str):
        raise OwnershipAmbiguousError("ownership ambiguous: no single named engine")
    if owner == "unresolved-gap":
        raise OwnershipAmbiguousError(
            "ownership ambiguous: unresolved-gap (I3 requires a named engine)")
    if not isinstance(effective, bool):
        raise OwnershipAmbiguousError(
            "ownership ambiguous: effective flag is not boolean "
            f"({effective!r}; I3 requires live-resolved true/false)"
        )
    return owner


class EngraphisSink:
    """Direct-MemoryService ingest wrapper for one workspace + one capture."""

    def __init__(self, service, *, workspace: str, capture_sha: str,
                 ownership: Any, staging_limits: Optional[dict] = None,
                 staging_state: Optional[dict] = None,
                 journal: Optional[list] = None,
                 store_path: Optional[Any] = None):
        self.svc = service
        self.workspace = workspace
        self.capture_sha = capture_sha
        self.ownership = ownership
        if store_path is None:
            store_path = self._resolve_store_path(service)
        self.store_path = Path(store_path).expanduser() if store_path else None
        self._read_conn_handle: Optional[Any] = None
        self.staging_limits = (
            dict(STAGING_LIMITS) if staging_limits is None else dict(staging_limits)
        )
        self.staging_state = {
            "records": int((staging_state or {}).get("records", 0)),
            "bytes": int((staging_state or {}).get("bytes", 0)),
        }
        self.events: list = journal if journal is not None else []
        self.ingest_calls: list = []

    # ── journal (interim registry delta; registry home is Stage 6, F13) ──────

    def journal_event(self, event: dict) -> dict:
        event = dict(event)
        event.setdefault("ts", time.time())
        self.events.append(event)
        return event

    def take_delta(self) -> bytes:
        """Byte delta of journal events since the last call (empty on a no-op)."""
        out = b"".join(
            (json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            for e in self.events
        )
        self.events.clear()
        return out

    # ── store-path resolution (G-E fix: bind from service or env) ────────────

    @staticmethod
    def _resolve_store_path(service: Any) -> Optional[str]:
        """Resolve the store path from the service or environment.

        The plugin binds store_path from ENGRAPHIS_LIVE_DB, but the worker's
        actual db path lives in ENGRAPHIS_DB_PATH (config.yaml).  When the
        constructor arg is None, try the service's own path attribute first,
        then the environment — so the sink can bind even when the caller
        passed no explicit path.
        """
        for attr in ("db_path", "store_path", "_db_path"):
            p = getattr(service, attr, None)
            if p:
                return str(p)
        for var in ("ENGRAPHIS_DB_PATH", "ENGRAPHIS_LIVE_DB"):
            p = os.environ.get(var)
            if p:
                return p
        return None

    # ── workspace / row lookups (direct store reads) ─────────────────────────

    @property
    def _conn(self):
        store = getattr(self.svc, "store", None)
        conn = getattr(store, "conn", None)
        if conn is None:
            raise TrustFlipUnavailable("direct-store connection unavailable")
        return conn

    @property
    def _read_conn(self):
        """Read-only connection for lookup/dedup/recount (2026-11-02 R-1 fix).

        Reads never write and never bypass the writer seam: the worker
        subprocess remains the ONLY ingest/update path. When the direct
        in-process store connection is unavailable (e.g. the mapped
        worker-subprocess service), lookups fall back to a mode=ro sqlite
        connection bound to the configured store path. No store_path (or an
        unreadable store) -> TrustFlipUnavailable, fail closed.
        """
        store = getattr(self.svc, "store", None)
        conn = getattr(store, "conn", None)
        if conn is not None:
            return conn
        if self._read_conn_handle is not None:
            return self._read_conn_handle
        if self.store_path is None or not self.store_path.is_file():
            tried = {
                "svc.store.conn": store is not None and getattr(store, "conn", None) is not None,
                "store_path": str(self.store_path) if self.store_path else None,
                "ENGRAPHIS_DB_PATH": os.environ.get("ENGRAPHIS_DB_PATH"),
                "ENGRAPHIS_LIVE_DB": os.environ.get("ENGRAPHIS_LIVE_DB"),
            }
            raise TrustFlipUnavailable(
                f"direct-store connection unavailable (tried: {tried})")
        try:
            ro = sqlite3.connect(
                f"file:{self.store_path}?mode=ro", uri=True, timeout=2.0)
            ro.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise TrustFlipUnavailable(
                f"read-only store connection unavailable: {exc}") from exc
        self._read_conn_handle = ro
        self.journal_event({
            "kind": "read_fallback_ro", "store_path": str(self.store_path),
            "via": "mode=ro", "ts": time.time(),
        })
        return ro

    def _workspace_id(self) -> Optional[str]:
        row = self._read_conn.execute(
            "SELECT id FROM workspaces WHERE name=?", (self.workspace,)
        ).fetchone()
        return row["id"] if row else None

    def _find_by_record_id(self, record_id: str) -> Optional[dict]:
        wid = self._workspace_id()
        if wid is None:
            return None
        rows = self._read_conn.execute(
            "SELECT id, provenance, metadata FROM memories WHERE workspace_id=?", (wid,)
        ).fetchall()
        for row in rows:
            try:
                meta = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                continue
            if meta.get("record_id") == record_id:
                prov = {}
                try:
                    prov = json.loads(row["provenance"] or "{}")
                except (TypeError, ValueError):
                    prov = {}
                return {
                    "id": row["id"],
                    "provenance": prov,
                    "metadata": meta,
                }
        return None

    def _service_ingest(self, record: dict, *, trusted: bool,
                        idempotent_check: bool, spark: bool = False) -> dict:
        """The single svc.ingest invocation point (spy target for fixtures).

        In idempotent-check mode any invocation pins resolve_conflicts=False
        (R15 no-op path); the fresh first ingest keeps the skill's working
        default (True).

        ``spark=False`` is the unchanged capture path; ``spark=True`` widens
        only the metadata carrier set so a spark's provenance survives.
        """
        resolve = NOOP_RESOLVE_CONFLICTS if idempotent_check else FIRST_INGEST_RESOLVE_CONFLICTS
        carriers = SPARK_METADATA_CARRIERS if spark else METADATA_CARRIERS
        metadata = {
            k: record[k] for k in carriers if k in record
        }
        self.ingest_calls.append({"record_id": record.get("record_id"),
                                  "trusted": trusted, "resolve_conflicts": resolve})
        return self.svc.ingest(
            record["content"],
            workspace=self.workspace,
            metadata=metadata,
            source=SOURCE,
            trusted=bool(trusted),
            resolve_conflicts=resolve,
        )

    # ── staging cap (R8) ─────────────────────────────────────────────────────

    def recount_staging(self) -> dict:
        """Re-derive the staging load from disk (gate-check channel)."""
        wid = self._workspace_id()
        if wid is None:
            self.staging_state = {"records": 0, "bytes": 0}
            return dict(self.staging_state)
        row = self._read_conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(COALESCE(content, ''))), 0) AS b "
            "FROM memories WHERE workspace_id=?",
            (wid,),
        ).fetchone()
        self.staging_state = {"records": int(row["n"]), "bytes": int(row["b"])}
        return dict(self.staging_state)

    def _staging_hold(self, record_id: str) -> dict:
        self.journal_event({
            "kind": "staging_cap_hold", "record_id": record_id,
            "workspace": self.workspace, "state": dict(self.staging_state),
            "limits": dict(self.staging_limits),
        })
        self.journal_event({
            "kind": "drain_review_demanded", "record_id": record_id,
            "workspace": self.workspace, "via": "r8_cap",
        })
        return {
            "op": "hold", "reason": "staging_cap",
            "record_id": record_id, "workspace": self.workspace,
            "state": dict(self.staging_state), "limits": dict(self.staging_limits),
            "drain_review_demanded": True,
        }

    def _staging_guard(self, record: dict) -> Optional[dict]:
        if self.staging_limits is None:
            return None
        limits = self.staging_limits
        state = self.staging_state
        content_bytes = len(record.get("content", "").encode("utf-8"))
        at_cap = (
            state["records"] >= int(limits["records"])
            or state["bytes"] + content_bytes > int(limits["bytes"])
        )
        if at_cap:
            return self._staging_hold(record.get("record_id", ""))
        return None

    # ── I5 / R15 ingest with query-then-skip ─────────────────────────────────

    def ingest_record(self, record: dict, *, trusted: bool = False) -> dict:
        """Ingest one record; re-ingest of the same record_id is query-then-skip
        unless it is the typed TRUST_FLIP exemption (trusted False->True)."""
        if not isinstance(record, dict) or "record_id" not in record or "content" not in record:
            raise SinkError("record must carry record_id and content")
        record_id = record["record_id"]

        # I3 ownership gate: refuse ambiguous induction ownership (R14 mirror).
        resolve_ownership(self.ownership)

        # R15 provenance binding: capture sha must match the sink's capture.
        if record.get("capture_sha") != self.capture_sha:
            raise ProvenanceBindingError(
                f"capture sha mismatch for {record_id}: "
                f"record={record.get('capture_sha')!r} sink={self.capture_sha!r}"
            )

        hold = self._staging_guard(record)
        if hold is not None:
            return hold

        existing = self._find_by_record_id(record_id)
        if existing is not None:
            stored_trusted = existing["provenance"].get("trusted") is True
            if (not stored_trusted) and trusted:
                # I5 typed exemption: TRUST_FLIP is NOT a no-op (R3).
                out = self.reclassify(record_id, trusted=True)
                return {"op": "trust_flip", "record_id": record_id, **out}
            # Non-flip re-ingest: query-then-skip, zero ingest calls.
            return {
                "op": "skip", "reason": "idempotent", "record_id": record_id,
                "resolve_conflicts": NOOP_RESOLVE_CONFLICTS,
                "ingest_calls": 0,
            }

        result = self._service_ingest(
            record, trusted=trusted, idempotent_check=False)
        self.staging_state["records"] += 1
        self.staging_state["bytes"] += len(record.get("content", "").encode("utf-8"))
        self.journal_event({
            "kind": "ingest", "record_id": record_id, "trusted": bool(trusted),
            "workspace": self.workspace,
        })
        return {"op": "ingest", "record_id": record_id, "result": result,
                "resolve_conflicts": FIRST_INGEST_RESOLVE_CONFLICTS}

    # ── spark mode (CHARGE-REVERIE-V1 §2/§3; ADDITIVE) ───────────────────────

    @staticmethod
    def _check_spark_binding(record: dict) -> None:
        """Spark provenance binding: form/spark_id/derived_from must agree.

        Capture records bind by capture_sha; a spark is not a capture product,
        so it binds by its own provenance instead (stricter, not looser).
        """
        if record.get("form") != SPARK_FORM:
            raise SparkProvenanceError(
                f"spark form must be {SPARK_FORM!r}, got {record.get('form')!r}")
        if record.get("provenance") != "derived":
            raise SparkProvenanceError(
                f"spark provenance must be 'derived', got {record.get('provenance')!r}")
        spark_id = record.get("spark_id")
        if not spark_id or spark_id != record.get("record_id"):
            raise SparkProvenanceError(
                f"spark_id must equal record_id ({spark_id!r} != "
                f"{record.get('record_id')!r})")
        sparks_from = record.get("sparks_from")
        if not isinstance(sparks_from, list) or len(sparks_from) < 2:
            raise SparkProvenanceError(
                "a spark encapsulates a connection: sparks_from needs >=2 episodes")
        if record.get("derived_from") != sparks_from:
            raise SparkProvenanceError(
                "derived_from must be the sparks_from citation list (verbatim)")

    def ingest_spark(self, record: dict, *, tier: str = "PROVISIONAL") -> dict:
        """Ingest one spark into its tier through the sanctioned sink path.

        ADDITIVE: capture ingest (`ingest_record`) is untouched. The tier ->
        workspace mapping is enforced structurally, so a PROVISIONAL mint can
        never land in the PERMANENT tier and vice versa. Re-ingest is the same
        query-then-skip as capture (promotion is idempotent).
        """
        if tier not in SPARK_TIERS:
            raise SinkError(f"unknown spark tier {tier!r}; expected one of "
                            f"{sorted(SPARK_TIERS)}")
        expected_ws = SPARK_TIERS[tier]
        if self.workspace != expected_ws:
            raise SinkError(
                f"tier {tier} lands in {expected_ws!r}; this sink writes "
                f"{self.workspace!r} (tier/workspace mismatch refused)")
        if not isinstance(record, dict) or "record_id" not in record or "content" not in record:
            raise SinkError("spark record must carry record_id and content")

        self._check_spark_binding(record)
        record_id = record["record_id"]

        # I3 ownership gate (same as capture): no ambiguous induction owner.
        resolve_ownership(self.ownership)

        hold = self._staging_guard(record)
        if hold is not None:
            return hold

        existing = self._find_by_record_id(record_id)
        if existing is not None:
            return {
                "op": "skip", "reason": "idempotent", "record_id": record_id,
                "spark_id": record_id, "tier": tier,
                "resolve_conflicts": NOOP_RESOLVE_CONFLICTS,
                "ingest_calls": 0,
            }

        result = self._service_ingest(
            record, trusted=False, idempotent_check=False, spark=True)
        self.staging_state["records"] += 1
        self.staging_state["bytes"] += len(record.get("content", "").encode("utf-8"))
        self.journal_event({
            "kind": "spark_ingest", "record_id": record_id,
            "spark_id": record_id, "tier": tier, "workspace": self.workspace,
            "derived_from": list(record.get("derived_from") or []),
        })
        return {"op": "ingest", "mode": "spark", "record_id": record_id,
                "spark_id": record_id, "tier": tier, "result": result,
                "resolve_conflicts": FIRST_INGEST_RESOLVE_CONFLICTS}

    # ── R3 / correction (a): in-place trust flip ─────────────────────────────

    def _apply_trust_update(self, row_id: str, provenance: dict, metadata: dict) -> None:
        """The direct-store UPDATE path (trust-gate-approval recipe).

        Python json round-trip only — NEVER SQL json_set (the boolean trap:
        json_set true becomes int 1 and fails the `is True` identity check).
        """
        conn = self._conn
        conn.execute(
            "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
            (json.dumps(provenance, ensure_ascii=False, sort_keys=True),
             json.dumps(metadata, ensure_ascii=False, sort_keys=True),
             row_id),
        )

    def reclassify(self, record_id: str, *, trusted: bool = True,
                   reviewer: str = "mev-gate") -> dict:
        """Flip trusted False->True IN PLACE on the existing record (R3/G4-corr).

        record_id is preserved (metadata + store row id). Loud failure with a
        journaled event when the update path is unavailable — never a silent
        drop, never a re-ingest.
        """
        if trusted is not True:
            raise SinkError("reclassify supports only trusted=False->True")
        resolve_ownership(self.ownership)

        existing = self._find_by_record_id(record_id)
        if existing is None:
            self.journal_event({
                "kind": "trust_flip_failed", "record_id": record_id,
                "from": False, "to": True, "via": "registry_op",
                "reason": "record_not_found", "ts": time.time(),
            })
            raise RecordNotFoundError(f"no stored record_id {record_id} in {self.workspace}")

        row_id = existing["id"]
        prov = dict(existing["provenance"])
        meta = dict(existing["metadata"])
        if prov.get("trusted") is True:
            return {"op": "noop", "reason": "already_trusted", "record_id": record_id}

        # Recipe fields (G4 finding "fix path B"), Python bools via json round-trip.
        prov["trusted"] = True
        prov["review_state"] = "approved"
        prov["trust_downgraded"] = False
        prov["trust_origin"] = "local_owner_approval"
        meta_prov = dict(meta.get("provenance") or {})
        meta_prov["trusted"] = True
        meta_prov["review_state"] = "approved"
        meta_prov["trust_downgraded"] = False
        meta_prov["trust_origin"] = "local_owner_approval"
        meta["provenance"] = meta_prov

        try:
            self._apply_trust_update(row_id, prov, meta)
            store = getattr(self.svc, "store", None)
            audit = getattr(store, "audit", None)
            if callable(audit):
                audit(reviewer, "trust_flip", row_id,
                      json.dumps({"record_id": record_id, "from": False,
                                  "to": True, "via": "registry_op"},
                                 sort_keys=True))
            conn = self._conn
            conn.commit()
        except SinkError:
            raise
        except Exception as exc:
            self.journal_event({
                "kind": "trust_flip_failed", "record_id": record_id,
                "from": False, "to": True, "via": "registry_op",
                "reason": f"update_path_unavailable:{type(exc).__name__}",
                "ts": time.time(),
            })
            raise TrustFlipUnavailable(
                f"trust update path unavailable for {record_id}: {exc}"
            ) from exc

        # Read-back post-condition: record_id preserved + prompt_eligible is True.
        row = self._conn.execute(
            "SELECT id, provenance, metadata FROM memories WHERE id=?", (row_id,)
        ).fetchone()
        if row is None:
            self.journal_event({
                "kind": "trust_flip_failed", "record_id": record_id,
                "from": False, "to": True, "via": "registry_op",
                "reason": "row_vanished_after_update", "ts": time.time(),
            })
            raise TrustFlipUnavailable(f"row {row_id} vanished after trust update")
        back_prov = json.loads(row["provenance"] or "{}")
        back_meta = json.loads(row["metadata"] or "{}")
        if back_meta.get("record_id") != record_id:
            raise TrustFlipUnavailable("record_id not preserved by trust update")
        if back_prov.get("trusted") is not True:
            raise TrustFlipUnavailable("trusted did not read True after flip")
        try:
            from engraphis.core.poisoning import prompt_eligible
            eligible = prompt_eligible(back_prov, back_meta)
        except Exception as exc:
            raise TrustFlipUnavailable(f"prompt_eligible read-back failed: {exc}") from exc
        if eligible is not True:
            raise TrustFlipUnavailable("prompt_eligible is not True after flip")

        self.journal_event({
            "kind": TRUST_FLIP_EVENT, "record_id": record_id,
            "from": False, "to": True, "ts": time.time(), "via": "registry_op",
        })
        return {
            "op": "trust_flip", "record_id": record_id, "row_id": row_id,
            "trusted": True, "prompt_eligible": True,
        }
