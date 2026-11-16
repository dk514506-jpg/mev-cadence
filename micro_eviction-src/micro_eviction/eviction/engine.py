"""Eviction engine core (G-B, CHARGE-G1-BUILD-ENGINE-V2 §2) — deterministic,
LLM-free eviction per the §R1 policy of record over the ordered ledger.

C6 boundary (host dependency): the engine EMITS eviction DECISIONS over a
surface and mutates a surface IN-MEMORY ONLY.  The HOST does reporting, which
says "staged", never "evicted".  Every `EvictionDecision.disposition` is the
literal string "staged".  The post_llm_call-only observation boundary: this
module never calls a model, never writes to a provider store, and never
mutates the input surface.

Binding data model (§2 G-B, C5 provisional form): ledger row =
(turn, rowid, package_id).  The frozen §0 fixtures carry the ledger in their
`derived_ledger` field.  turn <-> event seq <-> episode mapping is derived via
`deriveEpisodes` from the structural port (lib_port).

Budget parameter τ (D2, frozen): TAU_DEFAULT = 80,000 tokens, overridable per
fixture call.  No τ economics booked (C11) — token estimation is
character-based and approximate.

§R1 policy of record (Ruling 2, Dallas verbatim: "ACT first please proceed"):
1. Candidates = CLOSED, non-prologue, non-active episodes whose dependents
   are all fully evicted.
2. EXPL evictable only when every ACT that depended on it is already fully
   evicted.
3. On equal eligibility, evict the oldest ACT first
   (EVICTION_PREFERENCE = "oldest-act"); EXPL after its dependent ACTs.
4. Loop: smallest eviction that might help; re-check τ after each; repeat
   until under budget or no candidate remains (then alarm).
5. Graduated levels: strip reasoning (disabled, C2) -> bulk outputs ->
   intermediate artifacts -> full episode removal.
6. Prologue protection: USER TURNS AND GOAL STATEMENTS ARE NEVER EVICTION
   CANDIDATES (CWL Principle 3).
7. 1:1 turn FIFO is SUPERSEDED and MUST NOT be implemented.

Signal definitions (§7, binding):
  "alarm" = append to reports/engine-alarms.jsonl with fields
            {ts, kind, episode, reason} AND a nonzero exit from the invoked
            engine entrypoint.
  "refuse" = raise EvictionRefused(reason) AND record
             disposition: "refused".

Source of record: the GitHub reference implementation dsh-cwl
(https://github.com/kalifun/dsh-cwl).  Cite the GitHub reference, never the
arXiv id (C14).
"""

import json
from dataclasses import dataclass, field

from ..dsh_cwl.lib_port import (
    EVICTION_PREFERENCE,
    NEVER_EVICT_TYPES,
    SURFACE_THRESHOLD_CHARS,
    deriveEpisodes,
    largeResultSeqs,
)
from .pins import (
    PIN_CEILING_DEFAULT,
    PIN_OVERFLOW_CONDITION,
    PIN_OVERFLOW_KIND,
    derive_pins,
    protected_seqs,
)
from .staging_registry import alarm as _alarm_record
from .staging_registry import exists as _pkg_exists

# --- policy constants (frozen) -------------------------------------------

TAU_DEFAULT = 80_000

# Graduated stripping levels (§R1 item 5).  Level 1 ("reasoning") is
# disabled per C2 and is not represented here.
STRIP_LEVEL_BULK = "strip_bulk"
STRIP_LEVEL_INTERMEDIATE = "strip_intermediate"
STRIP_LEVEL_FULL = "stage_episode"


class EvictionRefused(Exception):
    """Refusal raised by the engine (e.g., C5 mutated ledger rowid).

    Per the §7 "refuse" definition: raise EvictionRefused(reason) AND
    record the report field disposition: "refused".
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
        self.disposition = "refused"


@dataclass(frozen=True)
class EvictionDecision:
    """A single eviction decision.

    C6 vocabulary: disposition is always "staged" — never "evicted".
    """

    action: str
    episode: str
    seqs: tuple
    package_id: str
    disposition: str = "staged"
    detail: str = ""

    def to_dict(self):
        return {
            "action": self.action,
            "episode": self.episode,
            "seqs": list(self.seqs),
            "package_id": self.package_id,
            "disposition": self.disposition,
            "detail": self.detail,
        }


def canonical_decisions(decisions):
    """Deterministic serialization of a decision list (byte-stable)."""
    return json.dumps(
        [d.to_dict() for d in decisions],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class EvictionEngine:
    """Deterministic, LLM-free eviction engine per §R1.

    Usage::

        engine = EvictionEngine(events, ledger, tau=TAU_DEFAULT)
        decisions, new_surface, alarms = engine.run(surface)

    The input `surface` is NEVER mutated (C6 in-memory-only).  A new list
    is returned.

    Pin awareness (AMB-1 / A-C8): the engine derives the never-evictable
    pin set via `pins.derive_pins(events)` merged with any caller-seeded
    pin records (`pin_seed`), bounded by `pin_ceiling` (default
    `PIN_CEILING_DEFAULT`).  When the pin count exceeds the ceiling the
    engine itself emits the structured `pin_overflow` alarm (via
    `staging_registry.alarm`) as part of `run()`.  The pin set is
    load-bearing: `pins.protected_seqs(...)` is consulted in
    `_is_protected`, so pinned seqs are never eviction candidates.
    """

    def __init__(self, events, ledger, tau=TAU_DEFAULT,
                 pin_ceiling=PIN_CEILING_DEFAULT, pin_seed=()):
        self.events = list(events)
        self.ledger = [tuple(row) for row in ledger]
        self.tau = tau
        self._by_seq = {e["seq"]: e for e in self.events}
        self._episodes = deriveEpisodes(self.events)
        self._turn_of_seq = self._build_turn_map()
        self._ledger_by_turn = {row[0]: row for row in self.ledger}
        self._without_stream = not any(
            isinstance(b, dict) and b.get("type") == "tool-call"
            for ev in self.events
            if ev.get("type") == "assistant/message"
            for b in ((ev.get("data") or {}).get("message") or {}).get("content") or []
        )
        self._pin_ceiling = pin_ceiling
        self._pins = derive_pins(self.events)
        seen = {p.get("seq") for p in self._pins}
        for p in (pin_seed or ()):
            if isinstance(p, dict) and p.get("seq") not in seen:
                self._pins.append(dict(p))
                seen.add(p.get("seq"))
        self._pin_protected = protected_seqs(self._pins)

    # --- turn mapping ----------------------------------------------------

    def _build_turn_map(self):
        """Map every event seq to its 1-based turn ordinal.

        Turn = 1-based ordinal of user/message events (C5 ledger rule).
        """
        turn = 0
        m = {}
        for ev in sorted(self.events, key=lambda e: e["seq"]):
            if ev.get("type") == "user/message":
                turn += 1
            m[ev["seq"]] = turn
        return m

    def _turn_of_episode(self, ep):
        return self._turn_of_seq.get(ep.get("startSeq"), 0)

    # --- token estimation (approximate, C11 no economics booked) ---------

    def _estimate_tokens(self, surface):
        """Rough token estimate: JSON-dumped data chars / 4, floor+1."""
        total = 0
        for seq in surface:
            ev = self._by_seq.get(seq)
            if ev is not None:
                total += len(json.dumps(ev.get("data") or {}, sort_keys=True))
        return total // 4 + 1

    # --- §R1 candidate selection ------------------------------------------

    def _is_protected(self, ep):
        """§R1 item 6: prologue / never-evict class.  C7 fail-closed:
        without-stream inputs mark ALL ambiguous content conservative-preserve.
        Pin awareness (AMB-1): the pin set is load-bearing — an episode
        holding any pinned seq is never an eviction candidate."""
        if bool(ep.get("neverEvict")) or ep.get("type") in NEVER_EVICT_TYPES:
            return True
        if self._without_stream and ep.get("type") == "single":
            return True
        if self._ep_member_seqs(ep) & self._pin_protected:
            return True
        return False

    def _ep_member_seqs(self, ep):
        """Seqs an episode owns (membership rule), regardless of surface."""
        seqs = set()
        for b in ep.get("batches") or []:
            seqs.add(b.get("seq"))
        for s in ep.get("resultSeqs") or []:
            seqs.add(s)
        seqs.add(ep.get("startSeq"))
        seqs.add(ep.get("endSeq"))
        return seqs

    def _has_live_dependent_act(self, ep, surface_seqs, fully_staged):
        """§R1 item 2: EXPL is evictable only after every dependent ACT is
        fully staged (evicted).  Returns True when a live dependent exists."""
        if ep.get("type") != "expl":
            return False
        surface_set = set(surface_seqs)
        for other in self._episodes:
            if other.get("type") != "act":
                continue
            if other.get("name") in fully_staged:
                continue
            if other.get("startSeq") not in surface_set:
                continue
            if ep.get("name") in (other.get("deps") or []):
                return True
        return False

    def _select_candidate(self, surface, fully_staged, processed):
        """Select the next eviction target per §R1 items 1-3.

        Returns the full episode dict or None.
        """
        surface_set = set(surface)
        best = None
        best_score = None
        for ep in self._episodes:
            if ep.get("name") in processed:
                continue
            if ep.get("startSeq") not in surface_set:
                continue
            if not ep.get("completed"):
                continue
            if self._is_protected(ep):
                continue
            if self._has_live_dependent_act(ep, surface, fully_staged):
                continue
            # §R1 item 3 / Ruling 2: oldest ACT first; EXPL after ACTs.
            if EVICTION_PREFERENCE == "oldest-act":
                base = 0 if ep.get("type") == "act" else 10
            else:
                base = 0 if ep.get("type") == "expl" else 10
            score = base + ep.get("startSeq", 0) / 1e9
            if best is None or score < best_score:
                best = ep
                best_score = score
        return best

    # --- graduated stripping helpers (§R1 item 5) -------------------------

    def _bulk_seqs(self, ep, surface):
        """Oversized tool-result seqs on the surface (level 2)."""
        results = largeResultSeqs(self.events, ep, surface)
        return frozenset(r["seq"] for r in results)

    def _intermediate_seqs(self, ep, surface):
        """All tool-result seqs of the episode on the surface (level 3)."""
        surface_set = set(surface)
        return frozenset(s for s in (ep.get("resultSeqs") or []) if s in surface_set)

    def _episode_seqs(self, ep, surface):
        """All episode member seqs on the surface (level 4)."""
        surface_set = set(surface)
        seqs = set()
        for b in ep.get("batches") or []:
            seqs.add(b.get("seq"))
        for s in ep.get("resultSeqs") or []:
            seqs.add(s)
        seqs.add(ep.get("startSeq"))
        seqs.add(ep.get("endSeq"))
        return frozenset(s for s in seqs if s in surface_set)

    # --- C1 / C5 gates ----------------------------------------------------

    def _check_c1(self, ep):
        """C1: package id must exist in staging.

        Returns (ok, package_id, alarm_kind, alarm_reason).
        """
        turn = self._turn_of_episode(ep)
        row = self._ledger_by_turn.get(turn)
        if row is None:
            return False, None, "no_ledger_row", f"no ledger row for turn {turn}"
        pkg = row[2]
        if pkg is None:
            return False, None, "missing_package_id", (
                f"C1: episode {ep.get('name')} turn {turn} has no package id"
            )
        if not _pkg_exists(pkg):
            return False, None, "package_not_staged", (
                f"C1: package {pkg} not in staging for episode {ep.get('name')}"
            )
        return True, pkg, None, None

    def _check_c5(self, ep):
        """C5: ledger rowid must match the derived rowid; else refuse."""
        turn = self._turn_of_episode(ep)
        row = self._ledger_by_turn.get(turn)
        if row is None:
            raise EvictionRefused(
                f"no ledger row for turn {turn} (episode {ep.get('name')})"
            )
        expected = "row-%04d" % turn
        if row[1] != expected:
            raise EvictionRefused(
                "ledger rowid mismatch for turn %d: expected %s, got %s"
                % (turn, expected, row[1])
            )

    # --- main loop --------------------------------------------------------

    def run(self, surface, alarms_dir=None):
        """Execute the eviction loop over `surface`.

        Returns (decisions, new_surface, alarms).
        The input `surface` is NEVER mutated (C6 in-memory-only).
        `alarms_dir` redirects the alarms sink (run-6 F11); default None =
        today's behavior (module-anchored campaign reports/).
        """
        work = list(surface)
        decisions = []
        alarms = []
        fully_staged = set()
        processed = set()

        # AMB-1 / A-C8: engine-side pin overflow emission.  The pin count
        # over its ceiling is surfaced as the structured pin_overflow alarm
        # (the §7 sliver) — never a silent drop.
        if len(self._pins) > self._pin_ceiling:
            alarms.append(_alarm_record({
                "kind": PIN_OVERFLOW_KIND,
                "episode": "",
                "reason": PIN_OVERFLOW_CONDITION,
            }, alarms_dir=alarms_dir))

        while self._estimate_tokens(work) > self.tau:
            candidate = self._select_candidate(work, fully_staged, processed)
            if candidate is None:
                rec = _alarm_record({
                    "kind": "no_candidates",
                    "episode": "",
                    "reason": "over budget (%d > %d) with no eviction candidates"
                    % (self._estimate_tokens(work), self.tau),
                }, alarms_dir=alarms_dir)
                alarms.append(rec)
                break

            # C1 gate
            ok, pkg, alarm_kind, alarm_reason = self._check_c1(candidate)
            if not ok:
                rec = _alarm_record({
                    "kind": alarm_kind,
                    "episode": candidate.get("name", ""),
                    "reason": alarm_reason,
                }, alarms_dir=alarms_dir)
                alarms.append(rec)
                processed.add(candidate.get("name"))
                continue

            # C5 gate — may raise EvictionRefused
            self._check_c5(candidate)

            name = candidate.get("name", "")
            seqs_done = False

            # Level 2: bulk outputs
            bulk = self._bulk_seqs(candidate, work)
            if bulk:
                work = [s for s in work if s not in bulk]
                decisions.append(EvictionDecision(
                    action=STRIP_LEVEL_BULK,
                    episode=name,
                    seqs=tuple(sorted(bulk)),
                    package_id=pkg,
                    detail="stripped bulk outputs from %s" % name,
                ))
                seqs_done = True
                if self._estimate_tokens(work) <= self.tau:
                    continue

            # Level 3: intermediate artifacts
            inter = self._intermediate_seqs(candidate, work)
            if inter:
                work = [s for s in work if s not in inter]
                decisions.append(EvictionDecision(
                    action=STRIP_LEVEL_INTERMEDIATE,
                    episode=name,
                    seqs=tuple(sorted(inter)),
                    package_id=pkg,
                    detail="stripped intermediate artifacts from %s" % name,
                ))
                seqs_done = True
                if self._estimate_tokens(work) <= self.tau:
                    continue

            # Level 4: full episode removal
            ep_seqs = self._episode_seqs(candidate, work)
            if ep_seqs:
                work = [s for s in work if s not in ep_seqs]
                decisions.append(EvictionDecision(
                    action=STRIP_LEVEL_FULL,
                    episode=name,
                    seqs=tuple(sorted(ep_seqs)),
                    package_id=pkg,
                    detail="staged full episode %s" % name,
                ))
                seqs_done = True

            fully_staged.add(name)
            processed.add(name)

        return decisions, work, alarms
