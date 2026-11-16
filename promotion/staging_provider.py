"""staging_provider — live, read-only recount of the REAL Engraphis staging workspace.

CHARGE-LIVE-STAGING-V1-2026-10-29, deliverable 1.

The provider is a zero-argument callable (memos_dispatcher.dispatch already
accepts a callable staging_state — memos_dispatcher.py:293-299) returning:

    {"records": N, "bytes": B, "basis": "live-recount", "ts": <epoch>,
     "store_sha256_12": "<first 12 hex of the store file sha256>",
     "store_path": "<path read>", "workspace": "mev-staging"}

Semantics are NOT forked: the record/byte derivation is
engraphis_sink.EngraphisSink.recount_staging() (engraphis_sink.py:205-217), the
same gate-check channel the sink uses, against the same STAGING_LIMITS single
source (engraphis_sink.py:32).

Read-only discipline: the store is opened with a sqlite read-only URI
(``file:///...?mode=ro``) plus ``PRAGMA query_only=ON``; this module never
issues a write and never opens the store writable. No writes to
~/.engraphis/memory.db happen here — the only sanctioned writer is the sink
ingest path, which lives in engraphis_sink, not in this provider.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_STORE = Path(
    os.environ.get("ENGRAPHIS_LIVE_DB", "~/.engraphis/memory.db")
).expanduser()
DEFAULT_CAMPAIGN_ROOT = Path(
    os.environ.get(
        "MEV_CAMPAIGN_ROOT",
        "/home/greenknight/Desktop/roof/campaigns/bench/micro-eviction",
    )
)

WORKSPACE = "mev-staging"
BASIS = "live-recount"

# Per-process sha cache: the real store is ~1.4 GB, hashing it is disk-bound
# (~17 s here). A loop runner must pay that once, not once per poll.
_SHA_CACHE: dict[str, dict] = {}


def _campaign_root(campaign_root=None) -> Path:
    return Path(campaign_root) if campaign_root else DEFAULT_CAMPAIGN_ROOT


def _import_sink(campaign_root=None):
    """Import the campaign's engraphis_sink (single source of STAGING_LIMITS)."""
    root = str(_campaign_root(campaign_root))
    if root not in sys.path:
        sys.path.insert(0, root)
    import engraphis_sink  # noqa: E402  (campaign-root import, lazy)

    return engraphis_sink


class _StoreShim:
    """Minimal stand-in for engraphis.core.store.Store (only `.conn` is used)."""

    __slots__ = ("conn",)

    def __init__(self, conn):
        self.conn = conn


class _ServiceShim:
    """Minimal stand-in for MemoryService (recount only touches `.store.conn`)."""

    __slots__ = ("store",)

    def __init__(self, conn):
        self.store = _StoreShim(conn)


def _resolve_store(store_path=None) -> Path:
    path = Path(store_path).expanduser() if store_path else DEFAULT_STORE
    if str(path) == ":memory:":
        raise ValueError("staging_provider requires a real store file (not :memory:)")
    if not path.is_file():
        raise FileNotFoundError(f"Engraphis store not found: {path}")
    return path


def open_readonly(store_path=None) -> sqlite3.Connection:
    """Open the store with a sqlite read-only URI; refuse anything less."""
    path = _resolve_store(store_path)
    uri = path.resolve().as_uri() + "?mode=ro"
    if "mode=ro" not in uri:
        raise AssertionError("read-only URI discipline violated")
    conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def store_sha256_12(store_path=None) -> str:
    """First 12 hex chars of the store file's sha256 (cached per process)."""
    path = _resolve_store(store_path)
    st = path.stat()
    key = {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    cached = _SHA_CACHE.get(str(path))
    if cached is not None and cached["key"] == key:
        return cached["sha12"]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    sha12 = h.hexdigest()[:12]
    _SHA_CACHE[str(path)] = {"key": key, "sha12": sha12}
    return sha12


def recount_state(store_path=None, *, workspace: str = WORKSPACE,
                  campaign_root=None) -> dict:
    """Live record/byte recount (no sha; cheap path for tests/diagnostics)."""
    sink_mod = _import_sink(campaign_root)
    conn = open_readonly(store_path)
    try:
        sink = sink_mod.EngraphisSink(
            _ServiceShim(conn), workspace=workspace, capture_sha="", ownership=None
        )
        state = sink.recount_staging()  # engraphis_sink.py:205-217 (reused verbatim)
    finally:
        conn.close()
    return {"records": int(state["records"]), "bytes": int(state["bytes"])}


def recount_live(store_path=None, *, workspace: str = WORKSPACE,
                 campaign_root=None) -> dict:
    """The declared live provider payload."""
    path = _resolve_store(store_path)
    state = recount_state(path, workspace=workspace, campaign_root=campaign_root)
    return {
        "records": state["records"],
        "bytes": state["bytes"],
        "basis": BASIS,
        "ts": time.time(),
        "store_sha256_12": store_sha256_12(path),
        "store_path": str(path),
        "workspace": workspace,
    }


def make_live_provider(store_path=None, *, workspace: str = WORKSPACE,
                       campaign_root=None):
    """Return the zero-argument callable dispatch() receives as staging_state."""

    def live_recount_provider() -> dict:
        return recount_live(
            store_path, workspace=workspace, campaign_root=campaign_root
        )

    return live_recount_provider


def staging_limits(campaign_root=None) -> dict:
    """The adopted caps, imported from engraphis_sink (never duplicated)."""
    return dict(_import_sink(campaign_root).STAGING_LIMITS)


__all__ = [
    "DEFAULT_STORE",
    "WORKSPACE",
    "BASIS",
    "open_readonly",
    "store_sha256_12",
    "recount_state",
    "recount_live",
    "make_live_provider",
    "staging_limits",
]
