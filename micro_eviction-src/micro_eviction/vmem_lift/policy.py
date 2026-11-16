# SPDX-License-Identifier: MIT
#
# policy.py — eviction-policy core lifted from vMem.
#
# Attribution: derived from vMem (https://github.com/soolaugust/vMem),
# MIT license, upstream commit 1d57f12 (1d57f12fbb46742a14ffe623fba474bf554ff954).
# Upstream locators for the semantics ported here:
#   * retention scoring      — memory_os/core/scorer.py:693-701
#                              (+ recency_score :107, access_frequency :422)
#   * eviction selection     — memory_os/store/vfs.py:6866-6960 (evict_lowest_retention)
#   * pin / shield semantics — memory_os/store/vfs.py:6971-7151 (chunk_pins, iter104),
#                              shield threshold at vfs.py:6866-6960 (iter1889)
#   * pin decay              — memory_os/runtime/write_feedback.py:141-196
#                              (decay_stale_pins: hard->soft 14d, soft->unpin 28d)
#   * pin cap                — memory_os/store/vfs.py:7092-7151 (enforce_pin_cap, iter356)
#   * clock second-chance    — memory_os/runtime/agent_working_set.py:279-309
#   * MGLRU generation bookkeeping — memory_os/store/mm.py:1983-2160
#                              (mglru_aging / mglru_promote / mglru_stats, iter44)
# This file is a SEMANTIC port rewritten as pure functions over mev data
# shapes (plain dicts); it does not import vMem and performs no I/O.

"""mev.vmem_lift.policy — retention scoring, pin/shield, MGLRU generations.

Pure functions only. Every public function takes plain data (dicts / lists)
and returns data; no filesystem, network, clock, or global-state access —
callers pass ``now`` explicitly wherever a timestamp is needed.

Record shape (plain dict; missing keys use the defaults below)::

    id            str                    identity (select_evictable returns records)
    type          str                    matched against protect_types
    importance    float = 0.5            >= IMPORTANCE_SHIELD_THRESHOLD shields
    uniqueness    float = 0.5            upstream constant (vfs.py evict path)
    access_count  int = 0                drives the access_freq factor
    apply_count   int = 0                > 0 exempts a pin from pin_decay
    last_accessed float | None           unix seconds; recency factor input
    created_at    float | None           unix seconds; grace-window input
    pinned_at     float | None           unix seconds; pin_decay / pin-cap order
    pin_type      "hard"|"soft"|None     mlock semantics (iter104)
    generation    int = 0                MGLRU-style rank (see below)

Generation encoding: ours is a recency RANK — 0 = coldest (evicted first),
``max_generation`` = hottest (default 4, upstream ``mglru.max_gen``).
``touch_generation`` promotes one rank per call, clamped at ``max_generation``;
eviction walks ranks ascending. Upstream ``lru_gen`` encodes the mirror
(0 = youngest, ``mglru_aging`` increments toward max, ``mglru_promote``
resets to 0, eviction scans gen DESC): same observable policy — access
protects, inactivity endangers, ranks clamp at max, coldest rank evicted
first — only the integer direction is this port's own.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

# ── retention scoring (scorer.py:693-701) ────────────────────────────────────
# retention = importance*0.4 + recency*0.15 + uniqueness*0.25 + access_freq*0.2
RETENTION_WEIGHTS: dict[str, float] = {
    "importance": 0.4,
    "recency": 0.15,
    "uniqueness": 0.25,
    "access_freq": 0.2,
}

# upstream _age_days fallback on a missing/unparseable timestamp (scorer.py:717)
_FALLBACK_AGE_DAYS = 30.0
_DAY_S = 86400.0


def recency_factor(last_accessed: float | None, now: float) -> float:
    """1 / (1 + age_days) (scorer.py:107). Missing/bad timestamp -> 30-day age."""
    if last_accessed is None:
        age_days = _FALLBACK_AGE_DAYS
    else:
        age_days = max(0.0, (now - float(last_accessed)) / _DAY_S)
    return 1.0 / (1.0 + age_days)


def access_frequency_factor(access_count: int) -> float:
    """log2(1 + count) / 4, capped at 1.0 (scorer.py:422)."""
    if access_count <= 0:
        return 0.0
    return min(1.0, math.log2(1.0 + access_count) / 4.0)


def retention_score(
    importance: float,
    recency: float,
    uniqueness: float,
    access_freq: float,
) -> float:
    """Weighted retention score (scorer.py:693-701 semantics over factors)."""
    return (
        importance * RETENTION_WEIGHTS["importance"]
        + recency * RETENTION_WEIGHTS["recency"]
        + uniqueness * RETENTION_WEIGHTS["uniqueness"]
        + access_freq * RETENTION_WEIGHTS["access_freq"]
    )


def record_retention_score(record: Mapping[str, Any], now: float) -> float:
    """retention_score over a record's fields (factors derived, defaults above)."""
    return retention_score(
        importance=float(record.get("importance", 0.5)),
        recency=recency_factor(record.get("last_accessed"), now),
        uniqueness=float(record.get("uniqueness", 0.5)),
        access_freq=access_frequency_factor(int(record.get("access_count", 0))),
    )


# ── pin / shield semantics (vfs.py:6971-7151; write_feedback.py:141-196) ────
PIN_HARD = "hard"
PIN_SOFT = "soft"
PIN_NONE = "none"

# iter1889: importance >= 0.9 is shielded from eviction (high_importance_eviction_shield)
IMPORTANCE_SHIELD_THRESHOLD = 0.9

# write_feedback.py decay_stale_pins defaults (mechanism 3)
PIN_DECAY_HARD_TO_SOFT_DAYS = 14.0
PIN_DECAY_SOFT_TO_UNPIN_DAYS = 28.0


def pin_state(record: Mapping[str, Any]) -> str:
    """Return PIN_HARD / PIN_SOFT / PIN_NONE for a record (mlock semantics)."""
    p = record.get("pin_type")
    if p == PIN_HARD:
        return PIN_HARD
    if p == PIN_SOFT:
        return PIN_SOFT
    return PIN_NONE


def is_hard_pinned(record: Mapping[str, Any]) -> bool:
    """Hard pin = skipped on ALL eviction paths (vfs.py iter104, test_chunk_pins T6)."""
    return pin_state(record) == PIN_HARD


def is_shielded(record: Mapping[str, Any]) -> bool:
    """importance >= IMPORTANCE_SHIELD_THRESHOLD => shielded from eviction (iter1889)."""
    return float(record.get("importance", 0.5)) >= IMPORTANCE_SHIELD_THRESHOLD


def pin_decay(
    records: Sequence[Mapping[str, Any]],
    now: float,
    *,
    hard_to_soft_days: float = PIN_DECAY_HARD_TO_SOFT_DAYS,
    soft_to_unpin_days: float = PIN_DECAY_SOFT_TO_UNPIN_DAYS,
) -> list[dict[str, Any]]:
    """Apply pin decay (write_feedback.py:141-196): hard->soft, soft->unpin.

    One step per call, per upstream: a pin at or past ``hard_to_soft_days``
    demotes hard->soft (it does not continue to unpin in the same call).
    Pins with ``apply_count > 0`` never decay; pins without ``pinned_at``
    never decay. Returns new record dicts (inputs untouched), same order.
    """
    out: list[dict[str, Any]] = []
    for rec in records:
        new = dict(rec)
        if pin_state(rec) == PIN_NONE or int(rec.get("apply_count", 0)) > 0:
            out.append(new)
            continue
        pinned_at = rec.get("pinned_at")
        if pinned_at is None:
            out.append(new)
            continue
        age_days = (now - float(pinned_at)) / _DAY_S
        if pin_state(rec) == PIN_HARD and age_days >= hard_to_soft_days:
            new["pin_type"] = PIN_SOFT
        elif pin_state(rec) == PIN_SOFT and age_days >= soft_to_unpin_days:
            new["pin_type"] = None
        out.append(new)
    return out


def enforce_pin_cap(
    records: Sequence[Mapping[str, Any]],
    cap: int,
) -> dict[str, Any]:
    """RLIMIT_MEMLOCK analog (vfs.py:7092-7151, iter356): cap the resident pin set.

    While the pinned count (hard + soft) exceeds ``cap``, the OLDEST soft pin
    (lowest ``pinned_at``) is released. Hard pins are never released.
    Returns {"records", "evicted_pin_ids", "pin_count"}.
    """
    pinned = [r for r in records if pin_state(r) != PIN_NONE]
    softs = sorted(
        (r for r in pinned if pin_state(r) == PIN_SOFT),
        key=lambda r: float(r.get("pinned_at") or 0.0),
    )
    evict_ids: list[Any] = []
    remaining = len(pinned)
    for rec in softs:
        if remaining <= cap:
            break
        evict_ids.append(rec.get("id"))
        remaining -= 1
    evict_set = set(evict_ids)
    out = [
        ({**r, "pin_type": None} if r.get("id") in evict_set else dict(r))
        for r in records
    ]
    return {"records": out, "evicted_pin_ids": evict_ids, "pin_count": remaining}


# ── MGLRU-style generation accounting (mm.py:1983-2160, iter44) ──────────────
DEFAULT_MAX_GENERATION = 4  # upstream mglru.max_gen default (test_mglru)


def generation_of(record: Mapping[str, Any]) -> int:
    """Generation rank a record currently sits in (0 = coldest = evict first)."""
    return int(record.get("generation", 0))


def touch_generation(
    record: Mapping[str, Any],
    *,
    max_generation: int = DEFAULT_MAX_GENERATION,
) -> dict[str, Any]:
    """Promote a record's generation on access, clamped to max_generation.

    Pure: returns a new dict. Upstream analog is mglru_promote (mm.py:2106);
    this port promotes one rank per call (clock second-chance lineage,
    agent_working_set.py:279-309) rather than resetting to the young end.
    """
    new = dict(record)
    new["generation"] = min(generation_of(record) + 1, max_generation)
    return new


def order_by_generation(
    records: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Stable ascending generation order (oldest generation first)."""
    return sorted(records, key=generation_of)


# ── eviction selection (vfs.py:6866-6960, pure) ──────────────────────────────
GRACE_SECONDS = 600.0  # iter_multiagent P1: 10-minute cross-agent grace window


def select_evictable(
    records: Sequence[Mapping[str, Any]],
    *,
    budget: int,
    protect_types: Iterable[str] = (),
    now: float,
    grace_seconds: float = GRACE_SECONDS,
) -> list[Mapping[str, Any]]:
    """Pick up to ``budget`` eviction candidates: MGLRU generation then
    retention_score ascending, skipping hard pins, shields, protected types
    and records still inside the 10-minute grace window.

    Soft pins are NOT skipped — upstream kswapd hard eviction respects only
    hard pins (test_chunk_pins T6).
    """
    if budget <= 0:
        return []
    protect = frozenset(protect_types)
    cutoff = now - grace_seconds
    candidates: list[Mapping[str, Any]] = []
    for rec in records:
        if is_hard_pinned(rec) or is_shielded(rec):
            continue
        if rec.get("type") in protect:
            continue
        created_at = rec.get("created_at")
        if created_at is not None and float(created_at) > cutoff:
            continue
        candidates.append(rec)
    candidates.sort(key=lambda r: (generation_of(r), record_retention_score(r, now)))
    return candidates[:budget]


# ── E-A9 residual lifecycle closure (engineer-checks-v3 §2 CHECK E-A9) ──────
# ADDITIVE residual shape (package atoms): each residual carries
#   lifecycle = {"decay": {layer, strength, eps_prune, t_max} | None,
#                "interval": {t_start, t_end} | None,
#                "stale_flag": bool}
# Closure rule (increment 2): every residual write through the lifted policy
# carries decay XOR validity-interval + stale_flag. The "XOR" of the charge is
# the spec's OR-with-both-allowed ("decay present XOR interval present both
# checked, both-allowed" — engineer-checks-v3 §2): at least one of decay /
# interval must be present (bare residual FAILS), both may coexist. Staleness
# is warning-bearing only: a stale residual is demoted to trusted=False, never
# refused. NO decay dynamics here — strength/eps_prune/t_max are carried data.


def compute_stale_flag(
    decay: Mapping[str, Any] | None,
    interval: Mapping[str, Any] | None,
    query_time: float,
) -> bool:
    """Recompute staleness (engineer-checks-v3 §2 E-A9):

    stale(r) = (decay != null AND strength < eps_prune)
            OR (interval != null AND NOT (t_start <= query_time <= t_end))

    Both halves evaluated when both present (both-allowed). Requires complete
    decay/interval state — validator handles malformed input separately.
    """
    stale = False
    if decay is not None and decay["strength"] < decay["eps_prune"]:
        stale = True
    if interval is not None and not (
        interval["t_start"] <= query_time <= interval["t_end"]
    ):
        stale = True
    return stale


def write_residual(
    *,
    residual_id: str,
    query_time: float,
    value: Any = None,
    decay: Mapping[str, Any] | None = None,
    interval: Mapping[str, Any] | None = None,
    trusted: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Sanctioned residual write through the lifted policy (E-A9 closure).

    Every write carries decay XOR validity-interval (both allowed) plus the
    recomputed ``stale_flag`` — bare writes are refused (fail-closed). Stale
    is warning-only: the write goes through but the residual is demoted to
    warning-bearing (``trusted=False``), never trusted context (v2 C4/E4).
    Pure: returns a new residual dict; no I/O, no decay dynamics.
    """
    if decay is None and interval is None:
        raise ValueError(
            f"residual {residual_id}: bare residual (both decay and interval null)"
        )
    if decay is not None and ("strength" not in decay or "eps_prune" not in decay):
        raise ValueError(
            f"residual {residual_id}: decay state incomplete (need strength, eps_prune)"
        )
    if interval is not None and ("t_start" not in interval or "t_end" not in interval):
        raise ValueError(
            f"residual {residual_id}: interval incomplete (need t_start, t_end)"
        )
    stale = compute_stale_flag(decay, interval, query_time)
    rec: dict[str, Any] = {
        "id": residual_id,
        "value": value,
        "trusted": False if stale else bool(trusted),
        "lifecycle": {
            "decay": dict(decay) if decay is not None else None,
            "interval": dict(interval) if interval is not None else None,
            "stale_flag": stale,
        },
    }
    rec.update(extra)
    return rec


def validate_e_a9(
    package: Mapping[str, Any],
    query_time: float | None = None,
) -> list[str]:
    """E-A9 residual_lifecycle_closure predicate (engineer-checks-v3 §2).

    For every residual: decay != null OR interval != null (both allowed; bare
    FAILS naming the residual id, B1 gap form); recorded stale_flag must match
    the recomputation; stale requires trusted == False (warning-bearing only).
    Fail-closed on missing fields. Returns violations; empty == PASS.
    """
    errors: list[str] = []
    residuals = package.get("residuals")
    if not isinstance(residuals, list):
        return ["package missing residuals[]"]
    if query_time is None:
        if "query_time" not in package:
            return ["package missing query_time"]
        query_time = package["query_time"]
    if isinstance(query_time, bool) or not isinstance(query_time, (int, float)):
        return [f"query_time not numeric: {query_time!r}"]

    for r in residuals:
        if not isinstance(r, Mapping):
            errors.append("residual entry not an object")
            continue
        rid = r.get("id", "<missing-id>")
        lc = r.get("lifecycle")
        if not isinstance(lc, Mapping):
            errors.append(f"residual {rid}: lifecycle missing (bare residual)")
            continue
        decay = lc.get("decay")
        interval = lc.get("interval")
        if decay is None and interval is None:
            errors.append(
                f"residual {rid}: bare residual (both decay and interval null)"
            )
            continue

        stale = False
        computable = True
        if decay is not None:
            if not isinstance(decay, Mapping):
                errors.append(f"residual {rid}: decay state malformed")
                computable = False
            elif "strength" not in decay or "eps_prune" not in decay:
                errors.append(
                    f"residual {rid}: decay state incomplete (need strength, eps_prune)"
                )
                computable = False
            elif decay["strength"] < decay["eps_prune"]:
                stale = True
        if interval is not None:
            if not isinstance(interval, Mapping):
                errors.append(f"residual {rid}: interval malformed")
                computable = False
            elif "t_start" not in interval or "t_end" not in interval:
                errors.append(
                    f"residual {rid}: interval incomplete (need t_start, t_end)"
                )
                computable = False
            elif not (interval["t_start"] <= query_time <= interval["t_end"]):
                stale = True
        if not computable:
            continue

        if "stale_flag" not in lc:
            errors.append(
                f"residual {rid}: stale_flag missing (staleness not computable)"
            )
            continue
        recorded = bool(lc["stale_flag"])
        if recorded != stale:
            errors.append(
                f"residual {rid}: stale_flag mismatch "
                f"(recorded={recorded}, recomputed={stale})"
            )
            continue
        if stale:
            trusted = r.get("trusted")
            if trusted is not False:
                errors.append(
                    f"residual {rid}: stale residual marked trusted "
                    f"(trusted={trusted!r}; must be warning-bearing, trusted=false)"
                )
    return errors


def e_a9_exit_code(
    package: Mapping[str, Any],
    query_time: float | None = None,
) -> int:
    """Exit-code contract for E-A9 / F-A9 fixtures: 0 = PASS, 1 = FAIL."""
    return 1 if validate_e_a9(package, query_time) else 0


# ── E-A8 signpost provenance (engineer-checks-v3 §2 CHECK E-A8) ─────────────
# ADDITIVE boundary shape (package annotations): each boundary carries
#   {id, signal_name, threshold_value, candidate_id,
#    confirmation_record: {reviewer_id, date, candidate_ref, ...},
#    relied_upon_for_eviction: bool}
# Closure rule (increment 3): every boundary annotation produced via the
# lifted policy emits the (signal, threshold, confirmation -> candidate)
# triple — signal_name non-empty, threshold_value present (numeric cutoff
# or named policy version), candidate_id present, confirmation_record with
# reviewer id + date, and the confirmation referencing the candidate
# (candidate_ref == candidate_id). Agent-only filings (no mechanical
# candidate) and mechanical-only filings (no agent confirmation) are
# refused at the write path and FAIL the validator naming the boundary id.
# A package claiming boundary-based eviction with zero recorded boundaries
# FAILS unconditionally. No scorer integration, no threshold calibration —
# threshold_value is carried data only (numeric cutoff or named policy).


def write_boundary(
    *,
    boundary_id: str,
    signal_name: str,
    threshold_value: Any,
    candidate_id: str,
    confirmation_record: Mapping[str, Any],
    relied_upon_for_eviction: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Sanctioned boundary annotation through the lifted policy (E-A8 closure).

    Every annotation emits the (signal, threshold, confirmation -> candidate)
    triple. Incomplete provenance is refused (fail-closed), naming the
    boundary id: empty signal, missing/invalid threshold (must be a numeric
    cutoff or a named policy version), missing candidate (agent-only
    boundary), missing/incomplete confirmation (mechanical-only filing), or
    a confirmation whose ``candidate_ref`` contradicts ``candidate_id``.
    The write emits ``confirmation_record.candidate_ref = candidate_id`` —
    the policy guarantees the confirmation -> candidate link. Pure: returns
    a new boundary dict; no I/O, no scorer, no threshold calibration.
    """
    if not isinstance(signal_name, str) or not signal_name.strip():
        raise ValueError(f"boundary {boundary_id}: signal_name missing/empty")
    numeric_ok = isinstance(threshold_value, (int, float)) and not isinstance(
        threshold_value, bool
    )
    named_ok = isinstance(threshold_value, str) and bool(threshold_value.strip())
    if threshold_value is None:
        raise ValueError(f"boundary {boundary_id}: threshold_value missing")
    if not numeric_ok and not named_ok:
        raise ValueError(
            f"boundary {boundary_id}: threshold_value not numeric cutoff "
            f"or named policy"
        )
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError(
            f"boundary {boundary_id}: candidate_id missing (agent-only boundary)"
        )
    if not isinstance(confirmation_record, Mapping):
        raise ValueError(
            f"boundary {boundary_id}: confirmation_record missing "
            f"(mechanical-only filing)"
        )
    if not confirmation_record.get("reviewer_id"):
        raise ValueError(
            f"boundary {boundary_id}: confirmation_record missing reviewer id"
        )
    if not confirmation_record.get("date"):
        raise ValueError(
            f"boundary {boundary_id}: confirmation_record missing date"
        )
    ref = confirmation_record.get("candidate_ref")
    if ref is not None and ref != candidate_id:
        raise ValueError(
            f"boundary {boundary_id}: confirmation_record.candidate_ref "
            f"({ref!r}) does not reference candidate_id ({candidate_id!r})"
        )
    conf = dict(confirmation_record)
    conf["candidate_ref"] = candidate_id
    rec: dict[str, Any] = {
        "id": boundary_id,
        "signal_name": signal_name,
        "threshold_value": threshold_value,
        "candidate_id": candidate_id,
        "confirmation_record": conf,
        "relied_upon_for_eviction": bool(relied_upon_for_eviction),
    }
    rec.update(extra)
    return rec


def validate_e_a8(package: Mapping[str, Any]) -> list[str]:
    """E-A8 signpost_provenance predicate (engineer-checks-v3 §2).

    For every boundary in ``boundaries[]`` with ``relied_upon_for_eviction``,
    assert the (signal-name, threshold-value, confirmation-record) triple is
    present and the confirmation references the candidate_id: signal_name
    non-empty, threshold_value present (numeric cutoff or named policy
    version), candidate_id present, confirmation_record with reviewer id +
    date, ``confirmation_record.candidate_ref == candidate_id``. Any missing
    element FAILS naming the boundary id; a package claiming boundary-based
    eviction with zero recorded boundaries FAILS unconditionally.
    Boundaries not relied upon for eviction are not checked. Fail-closed on
    missing fields. Returns violations; empty == PASS.
    """
    errors: list[str] = []
    boundaries = package.get("boundaries")
    if not isinstance(boundaries, list):
        return ["package missing boundaries[]"]
    if package.get("boundary_based_eviction") and not boundaries:
        return [
            "package claims boundary-based eviction with zero recorded boundaries"
        ]

    for b in boundaries:
        if not isinstance(b, Mapping):
            errors.append("boundary entry not an object")
            continue
        bid = b.get("id", "<missing-id>")
        if not b.get("relied_upon_for_eviction"):
            continue

        sig = b.get("signal_name")
        if not isinstance(sig, str) or not sig.strip():
            errors.append(f"boundary {bid}: signal_name missing/empty")

        if "threshold_value" not in b or b["threshold_value"] is None:
            errors.append(f"boundary {bid}: threshold_value missing")
        else:
            tv = b["threshold_value"]
            numeric_ok = isinstance(tv, (int, float)) and not isinstance(tv, bool)
            named_ok = isinstance(tv, str) and bool(tv.strip())
            if not numeric_ok and not named_ok:
                errors.append(
                    f"boundary {bid}: threshold_value not numeric cutoff "
                    f"or named policy"
                )

        cid = b.get("candidate_id")
        cid_present = isinstance(cid, str) and bool(cid.strip())
        if not cid_present:
            errors.append(
                f"boundary {bid}: candidate_id missing (agent-only boundary)"
            )

        conf = b.get("confirmation_record")
        if not isinstance(conf, Mapping):
            errors.append(
                f"boundary {bid}: confirmation_record missing "
                f"(mechanical-only filing)"
            )
        else:
            if not conf.get("reviewer_id"):
                errors.append(
                    f"boundary {bid}: confirmation_record missing reviewer id"
                )
            if not conf.get("date"):
                errors.append(f"boundary {bid}: confirmation_record missing date")
            if cid_present and conf.get("candidate_ref") != cid:
                errors.append(
                    f"boundary {bid}: confirmation_record.candidate_ref "
                    f"({conf.get('candidate_ref')!r}) does not reference "
                    f"candidate_id ({cid!r})"
                )
    return errors


def e_a8_exit_code(package: Mapping[str, Any]) -> int:
    """Exit-code contract for E-A8 / F-A8 fixtures: 0 = PASS, 1 = FAIL."""
    return 1 if validate_e_a8(package) else 0


# ── increment-0 spec obligations (CRYSTALLIZATION-PACKAGE-V1.6 §1) ───────────
PREDICATE_KINDS: frozenset[str] = frozenset(
    {
        "BYTE_PRESENT",
        "TAG_CLOSED_SET",
        "POINTER_RESOLVES",
        "HASH_EQ",
        "LIFECYCLE_STALE",
        "BOUNDARY_TRIPLE",
        "SANDBOX_RULE",
        "SEMANTIC_REVIEW",
    }
)

CHECK_CLASSES: frozenset[str] = frozenset({"9a", "9b", "9c"})

# Obligation 1 — explicit Check.class <-> predicate.kind binding.
# 9a/9c always name executable code (never SEMANTIC_REVIEW); SANDBOX_RULE is
# the 9c meta-invariant token; 9b is the only home of SEMANTIC_REVIEW.
CHECK_CLASS_KIND_BINDING: dict[str, frozenset[str]] = {
    "9a": frozenset(PREDICATE_KINDS - {"SEMANTIC_REVIEW", "SANDBOX_RULE"}),
    "9b": frozenset(PREDICATE_KINDS - {"SANDBOX_RULE"}),
    "9c": frozenset(PREDICATE_KINDS - {"SEMANTIC_REVIEW"}),
}

RESIDUAL_KINDS: frozenset[str] = frozenset(
    {"quantities", "dates", "selectors", "named_items"}
)

# Obligation 4 — formal_id registry (predicates frozen in engineer-checks-v2/v3).
FORMAL_ID_REGISTRY: frozenset[str] = frozenset(
    {
        "E-A8",
        "E-A9",
        "F-A8",
        "F-A9",
        "C2",
        "C3",
        "C6",
        "C8",
        "C9",
        "E9",
    }
)


def validate_check_class_binding(
    checks: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Obligation 1: every Check.class must map to a declared predicate.kind."""
    errs: list[str] = []
    for chk in checks:
        cid = chk.get("check_id", "<no check_id>")
        cls = chk.get("class")
        pred = chk.get("predicate")
        kind = pred.get("kind") if isinstance(pred, Mapping) else None
        if cls not in CHECK_CLASSES:
            errs.append(f"{cid}: undeclared Check.class {cls!r}")
            continue
        if kind not in PREDICATE_KINDS:
            errs.append(f"{cid}: undeclared predicate.kind {kind!r}")
            continue
        if kind not in CHECK_CLASS_KIND_BINDING[cls]:
            errs.append(f"{cid}: unbound Check.class {cls!r} <-> kind {kind!r}")
    return errs


def validate_residual_index(
    residuals: Sequence[Mapping[str, Any]],
    residual_index: Mapping[str, Any],
) -> list[str]:
    """Obligation 2: residual_index membership matches Residual.kind and is
    complete over residuals[]."""
    errs: list[str] = []
    by_id = {r.get("residual_id"): r for r in residuals}
    for cat in residual_index:
        if cat not in RESIDUAL_KINDS:
            errs.append(f"residual_index: unknown category {cat!r}")
    for r in residuals:
        rid = r.get("residual_id")
        kind = r.get("kind")
        if kind not in RESIDUAL_KINDS:
            errs.append(f"{rid}: undeclared Residual.kind {kind!r}")
            continue
        filed = residual_index.get(kind) or []
        if rid not in filed:
            errs.append(f"{rid}: missing from residual_index[{kind!r}]")
    for cat, ids in residual_index.items():
        for rid in ids or ():
            res = by_id.get(rid)
            if res is None:
                errs.append(f"residual_index[{cat!r}]: dangling residual_id {rid!r}")
            elif cat in RESIDUAL_KINDS and res.get("kind") != cat:
                errs.append(
                    f"residual_index[{cat!r}]: {rid!r} filed under wrong kind "
                    f"(Residual.kind = {res.get('kind')!r})"
                )
    return errs


def validate_required_one_of(
    checks: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Obligation 3: the required_one_of [code_ref, formal_id] group for
    kind == SEMANTIC_REVIEW must be satisfied (emission-validator rule);
    every other kind must carry code_ref (absent_iff kind == SEMANTIC_REVIEW)."""
    errs: list[str] = []
    for chk in checks:
        cid = chk.get("check_id", "<no check_id>")
        pred = chk.get("predicate")
        if not isinstance(pred, Mapping):
            errs.append(f"{cid}: missing predicate object")
            continue
        kind = pred.get("kind")
        has_code = bool(pred.get("code_ref"))
        has_formal = bool(pred.get("formal_id"))
        if kind == "SEMANTIC_REVIEW":
            if not (has_code or has_formal):
                errs.append(
                    f"{cid}: required_one_of [code_ref, formal_id] unsatisfied "
                    f"for kind SEMANTIC_REVIEW"
                )
        elif not has_code:
            errs.append(f"{cid}: code_ref required when kind != SEMANTIC_REVIEW")
    return errs


def validate_formal_id_resolution(
    checks: Sequence[Mapping[str, Any]],
    registry: Iterable[str] = FORMAL_ID_REGISTRY,
) -> list[str]:
    """Obligation 4: every formal_id present must resolve against the
    registry — no dangling ids."""
    known = frozenset(registry)
    errs: list[str] = []
    for chk in checks:
        cid = chk.get("check_id", "<no check_id>")
        pred = chk.get("predicate")
        if not isinstance(pred, Mapping):
            continue
        fid = pred.get("formal_id")
        if fid is None:
            continue
        if fid not in known:
            errs.append(f"{cid}: dangling formal_id {fid!r}")
    return errs


def validate_package_obligations(
    package: Mapping[str, Any],
    registry: Iterable[str] = FORMAL_ID_REGISTRY,
) -> list[str]:
    """Run obligations 1-4 over a package-shaped mapping
    ({"checks", "residuals", "residual_index"}); errors accumulate."""
    checks = package.get("checks") or ()
    residuals = package.get("residuals") or ()
    residual_index = package.get("residual_index") or {}
    errs: list[str] = []
    errs.extend(validate_check_class_binding(checks))
    errs.extend(validate_residual_index(residuals, residual_index))
    errs.extend(validate_required_one_of(checks))
    errs.extend(validate_formal_id_resolution(checks, registry))
    return errs
