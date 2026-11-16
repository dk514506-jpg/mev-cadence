"""Staging registry (G-B, CHARGE-G1-BUILD-ENGINE-V2 §2) — package id minting,
existence checks, and structured alarms.

C1 boundary: eviction of an episode is FORBIDDEN until its crystallization
package id exists in staging; violation = skip the eviction + alarm.
C6 boundary: the HOST does reporting, which says "staged", never "evicted".

Alarm schema (§7 signal definitions, binding): every alarm record appended to
`reports/engine-alarms.jsonl` carries the fields `{ts, kind, episode, reason}`.
"alarm" = an append to the alarms file AND a nonzero exit from the invoked
engine entrypoint.  "refuse" = raise `EvictionRefused(reason)` AND record the
report field `disposition: "refused"`.

The alarms file is a worktree test artifact: appends happen only from
test-generated calls to `alarm()`.
"""

import hashlib
import json
import os
import time

_ALARMS_RELPATH = os.path.join("reports", "engine-alarms.jsonl")

# In-process registry of minted package ids.  Populated by mint_package_id
# and checked by exists().  Reset via reset() for test isolation.
_registry: dict = {}


def reset():
    """Clear the in-process registry (test isolation)."""
    _registry.clear()


def mint_package_id(episode):
    """Deterministic, content-derived package id for an episode.

    The id is a 12-hex-char digest of the episode's structural identity:
    type, startSeq, endSeq, batch seqs, and sorted path sets.  Identical
    episode content always yields the same id; any content change yields a
    different id.
    """
    if not isinstance(episode, dict):
        raise TypeError("episode must be a dict")
    h = hashlib.sha256()
    h.update(str(episode.get("type", "")).encode("utf-8"))
    h.update(b"\x00")
    h.update(str(episode.get("startSeq", "")).encode("utf-8"))
    h.update(b"\x00")
    h.update(str(episode.get("endSeq", "")).encode("utf-8"))
    h.update(b"\x00")
    for b in episode.get("batches") or []:
        h.update(str(b.get("seq", "")).encode("utf-8"))
        h.update(b"\x01")
        h.update(json.dumps(b.get("names") or [], sort_keys=True).encode("utf-8"))
        h.update(b"\x01")
    h.update(b"\x00")
    h.update(json.dumps(sorted(episode.get("readPaths") or [])).encode("utf-8"))
    h.update(b"\x00")
    h.update(json.dumps(sorted(episode.get("touchedPaths") or [])).encode("utf-8"))
    pid = "pkg-" + h.hexdigest()[:12]
    _registry[pid] = True
    return pid


def register(id):
    """Register a pre-existing package id (e.g., from a fixture ledger)."""
    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string")
    _registry[id] = True
    return id


def exists(id):
    """True when the package id is present in the staging registry."""
    return bool(id) and id in _registry


def alarm(event, alarms_dir=None):
    """Append a structured alarm record to reports/engine-alarms.jsonl.

    Schema (§7 signal definitions): {ts, kind, episode, reason}.
    Returns the record that was appended.  The alarms_dir parameter exists
    for test isolation; it defaults to the campaign-root reports/ directory.
    """
    if not isinstance(event, dict):
        raise TypeError("event must be a dict")
    record = {
        "ts": event.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": event.get("kind", "alarm"),
        "episode": event.get("episode", ""),
        "reason": event.get("reason", ""),
    }
    if alarms_dir is None:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        alarms_dir = os.path.join(base, "reports")
    os.makedirs(alarms_dir, exist_ok=True)
    path = os.path.join(alarms_dir, _ALARMS_RELPATH.split(os.sep)[-1])
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    return record
