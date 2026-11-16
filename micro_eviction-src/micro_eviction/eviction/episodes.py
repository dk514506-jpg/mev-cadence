"""Mechanical episode boundary-proposer (G-D; CHARGE-G1-BUILD-ENGINE-V2 §2).
Mechanical deduction ONLY — no LLM, no annotation surface (Ruling 3, A-D4).
Proposal artifact lands BEFORE any downstream consumer."""

import hashlib, json, os, time

from ..dsh_cwl.lib_port import deriveEpisodes

PROPOSALS_DIR_DEFAULT = os.path.join("reports", "proposals")


def propose(events):
    """Return the proposal record dict: {"episodes": <deriveEpisodes output>,
    "proposal_ts": <unix float>, "content_sha256": <hex>} where content_sha256
    is sha256 over json.dumps({"episodes": ...}, sort_keys=True,
    separators=(",", ":")) — the canonical content ONLY (ts excluded)."""
    episodes = deriveEpisodes(events)
    canonical = json.dumps({"episodes": episodes}, sort_keys=True, separators=(",", ":"))
    return {
        "episodes": episodes,
        "proposal_ts": time.time(),
        "content_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def check_ordering(proposal_record, consumer_ts):
    """True iff consumer_ts >= proposal_record["proposal_ts"]; raise
    ValueError on missing/invalid fields."""
    ts = proposal_record.get("proposal_ts") if isinstance(proposal_record, dict) else None
    if not isinstance(ts, (int, float)):
        raise ValueError("proposal_record missing or invalid proposal_ts")
    return consumer_ts >= ts


def write_proposal(events, out_dir=PROPOSALS_DIR_DEFAULT):
    """propose() then land JSON at <out_dir>/proposal-<sha12>.json (mkdir -p).
    Return the record. Pure worktree writes only."""
    record = propose(events)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "proposal-%s.json" % record["content_sha256"][:12])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, sort_keys=True)
    return record
