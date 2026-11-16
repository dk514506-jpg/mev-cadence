r"""Structural port of dsh-cwl `lib.js` (G-A1) — episode derivation + eviction
selection primitives.

Source of record: the GitHub reference implementation dsh-cwl `lib.js`
(https://github.com/kalifun/dsh-cwl), frozen byte-identically at
tests/g1/fixtures/reference_frozen/lib.js (sha256_12 7318f403ded1, 17,809
bytes). `index.js` is EXCLUDED by path (no HTTP/recall surface here).

SUPERSESSION NOTE (2026-11-07, CHARGE-G1-BUILD-ENGINE-V2-2026-11-06 §R1,
Ruling 2 — Dallas, verbatim: "ACT first please proceed"): `lib.js`'s eviction
scoring `expl=0 < act=10` (evict pure-context EXPL episodes before ACT
episodes) is SUPERSEDED and MUST NOT be the order of the port. This port is
structural; the scoring is overridden to the §R1 policy of record:
`EVICTION_PREFERENCE = "oldest-act"` — on equal eligibility evict the oldest
ACT episode first (effects persisted in environment), EXPL only after every
ACT that depended on it is fully evicted (§R1 item 2: a live-dependent EXPL is
not a candidate at all). 1:1 turn FIFO is likewise SUPERSEDED (§R1 item 7) and
is not implemented. The eviction target selection is asserted against the §R1
policy, not against `lib.js`'s order.

Prologue protection (§R1 item 6, approved ruling 2026-11-02): user turns and
goal statements are NEVER eviction candidates (CWL Principle 3) — they are
members of the C8 pin set / never-evict class (`NEVER_EVICT_TYPES`) and
`deriveEpisodes` never promotes a `user/message` event to an episode.

Port surface (binding, charge §2 G-A1): deriveEpisodes, pickEvictionTarget,
newestEvictableBoundary, largeResultSeqs, pairingBreaks, episodeBlock,
mergeBlocksByPos, mergeRanges, shadowedNodes, stubToolResultData,
toolResultText, plus constants EXPL_TOOLS, BASH_WRITE_RE, MAX_BATCHES=6,
threshold=1500 (`SURFACE_THRESHOLD_CHARS`), order (`ORDER_DEFAULT`),
preserveRecent (`PRESERVE_RECENT`). Export names are carried EXACTLY from
`lib.js` so the JS-vs-Python equivalence contract (tests/g1/fixtures/equivalence/,
canonical bytes = sorted-key compact JSON, UTF-8, no ASCII escaping) can
byte-compare one fixture per ported export.

Recorded adaptations (each carried with a reason; everything else is exact):
1. Scoring override in pickEvictionTarget per Ruling 2 / §R1 (above).
2. BASH_WRITE_RE: JS regex `\w`/`\b` are ASCII-only while Python's are
   Unicode-aware by default; the pattern is rewritten with explicit ASCII
   classes/lookarounds (`[A-Za-z0-9_]`, `(?<![A-Za-z0-9_])`/`(?![A-Za-z0-9_])`)
   for semantic identity. Residual divergence: JS `\s` also matches U+FEFF,
   Python's does not.
3. pairingBreaks: lib.js's dead pre-scan maps (`callInWin`/`resultInWin`) and
   its empty comment-only loop are omitted — they have no effect on any
   output; the reliable full-surface scan is carried exactly.
4. String lengths (`largeResultSeqs` `chars` and its threshold compare) are
   UTF-16 code-unit lengths, matching JS `String.prototype.length`.
5. Constants that live as function locals / parameter defaults in `lib.js`
   (MAX_BATCHES, threshold=1500, order, preserveRecent) are elevated to
   named, frozen module-level constants per §R1 "Policy constants (frozen)"
   and are used as the exact defaults of the same-named parameters/opts keys.
6. lib.js comment blocks are translated/summarized, not carried verbatim;
   behavior is carried exactly (subject to item 1).
"""

import json
import re

# --- policy constants (frozen) -------------------------------------------

EVICTION_PREFERENCE = "oldest-act"
MAX_BATCHES = 6
SURFACE_THRESHOLD_CHARS = 1500
ORDER_DEFAULT = "oldest"
PRESERVE_RECENT = 2

# §R1 item 6: user turns and goal statements are never eviction candidates.
NEVER_EVICT_TYPES = frozenset({"user", "goal", "prologue"})

# --- classification substrate (carried from lib.js) -----------------------

EXPL_TOOLS = frozenset(
    ["read", "search", "list", "ls", "glob", "grep", "find", "web_search"]
)

# lib.js: />>|>|\bsed\s+-i\b|.../ — see recorded adaptation (2) in the
# module docstring for the ASCII-safe rewrite of \w and \b.
BASH_WRITE_RE = re.compile(
    r">>|>"
    r"|(?<![A-Za-z0-9_])sed\s+-i(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])rm(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])mv(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])cp(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])touch(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])mkdir(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])chmod(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])chown(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])ln(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])tee(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])install(?![A-Za-z0-9_])"
    r"|git\s+(add|commit|push|rm|mv)(?![A-Za-z0-9_])"
    r"|(npm|pnpm|yarn)\s+(i|install|add)(?![A-Za-z0-9_])"
    r"|curl\s+-[a-z]*o"
    r"|wget\s+-O"
)

_PATH_IN_CMD_RE = re.compile(r"[A-Za-z0-9_./-]+\.[a-z]+")


def _js_str_len(s):
    """UTF-16 code-unit length (JS `String.prototype.length`)."""
    return len(s.encode("utf-16-le")) // 2


def _parse_args(args_raw):
    if isinstance(args_raw, str):
        return json.loads(args_raw)
    return args_raw


def _args_get(args, *keys):
    """JS `args?.k1 ?? args?.k2 ?? ...` — nullish-only fallback."""
    for k in keys:
        if isinstance(args, dict):
            v = args.get(k)
        else:
            v = None
        if v is not None:
            return v
    return None


def collectReadPaths(toolName, argsRaw):
    """lib.js collectReadPaths — paths a read/search call touched."""
    if toolName != "read" and toolName != "search":
        return []
    try:
        args = _parse_args(argsRaw)
        p = _args_get(args, "file_path", "path", "pattern")
        return [p] if isinstance(p, str) else []
    except Exception:
        return []


def collectTouchedPaths(toolName, argsRaw):
    """lib.js collectTouchedPaths — paths an act call touched."""
    try:
        args = _parse_args(argsRaw)
        p = _args_get(args, "file_path", "path")
        if isinstance(p, str):
            return [p]
        if isinstance(args, dict) and isinstance(args.get("command"), str):
            return _command_paths(args["command"])
        return []
    except Exception:
        return []


def _command_paths(cmd):
    matches = _PATH_IN_CMD_RE.findall(cmd)
    seen = []
    for m in matches:
        if "/" in m and m not in seen:
            seen.append(m)
    return seen[:5]


def isReadOnlyCall(name, argsRaw):
    """lib.js isReadOnlyCall — true when the call has no side effects."""
    if name in EXPL_TOOLS:
        return True
    if name != "bash":
        return False
    try:
        args = _parse_args(argsRaw)
        cmd = _args_get(args, "command")
        if cmd is None:
            cmd = ""
        if not isinstance(cmd, str):
            cmd = str(cmd)
        return BASH_WRITE_RE.search(cmd) is None
    except Exception:
        return False


def collectCommandPaths(argsRaw):
    """lib.js collectCommandPaths — best-effort read-only bash path refs."""
    try:
        args = _parse_args(argsRaw)
        cmd = _args_get(args, "command")
        if not isinstance(cmd, str):
            return []
        return _command_paths(cmd)
    except Exception:
        return []


def deriveEpisodes(events, opts=None):
    """lib.js deriveEpisodes — episode graph (stage merge + deps) from events.

    Unit: each assistant/message carrying tool-calls is one tool-batch;
    consecutive same-type batches merge into expl/act segments (capped at
    MAX_BATCHES per segment); deps: an act that touches a file an earlier
    expl read gets a dep edge (protects that expl at eviction time).
    """
    if opts is None:
        opts = {}
    max_batches = opts.get("maxBatches")
    if max_batches is None:
        max_batches = MAX_BATCHES

    episodes = []
    expl_count = 0
    act_count = 0
    single_count = 0
    cur = None

    def flush():
        nonlocal cur
        if cur is not None:
            cur["completed"] = True
            episodes.append(cur)
            cur = None

    by_seq = {}
    for e in events:
        by_seq[e["seq"]] = e

    has_pos = isinstance(opts.get("surface"), list)
    if has_pos:
        stream = [by_seq[s] for s in opts["surface"] if s in by_seq]
    else:
        stream = events
    pos = -1

    for ev in stream:
        if has_pos:
            pos += 1
        etype = ev.get("type") if isinstance(ev, dict) else None
        data = ev.get("data") if isinstance(ev, dict) else None
        if data is None:
            data = {}
        if etype == "user/message":
            flush()
            continue
        if etype == "assistant/message":
            message = data.get("message") if isinstance(data, dict) else None
            content = (message or {}).get("content") if isinstance(message, dict) else None
            if content is None:
                content = []
            tool_calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool-call"]
            if not tool_calls:
                # C4: a tool-less assistant turn is EXACTLY ONE single-turn
                # episode.  It NEVER merges with neighbours into a multi-turn
                # episode (not stored in ``cur``).  For without-stream event
                # shapes (no tool-call stream anywhere) this yields the
                # turn-granularity episode set required by C7.
                single_count += 1
                ep = {}
                ep["name"] = "single-%d" % single_count
                ep["type"] = "single"
                ep["batches"] = [{"seq": ev["seq"], "names": [], "readPaths": [], "touchedPaths": []}]
                ep["startSeq"] = ev["seq"]
                ep["endSeq"] = ev["seq"]
                if has_pos:
                    ep["posStart"] = pos
                    ep["posEnd"] = pos
                ep["toolNames"] = []
                ep["readPaths"] = []
                ep["touchedPaths"] = []
                ep["deps"] = []
                ep["resultSeqs"] = []
                ep["completed"] = True
                episodes.append(ep)
                continue
            is_expl = all(isReadOnlyCall(b.get("name"), b.get("arguments")) for b in tool_calls)
            read_paths = []
            for b in tool_calls:
                bname = b.get("name")
                if bname in EXPL_TOOLS:
                    read_paths.extend(collectReadPaths(bname, b.get("arguments")))
                elif isReadOnlyCall(bname, b.get("arguments")):
                    read_paths.extend(collectCommandPaths(b.get("arguments")))
            names = [b.get("name") for b in tool_calls]
            if is_expl:
                touched_paths = []
            else:
                touched_paths = []
                for b in tool_calls:
                    touched_paths.extend(collectTouchedPaths(b.get("name"), b.get("arguments")))
            in_cap = cur is not None and len(cur["batches"]) < max_batches
            if cur is not None and cur["type"] == ("expl" if is_expl else "act") and in_cap:
                cur["batches"].append({"seq": ev["seq"], "names": names, "readPaths": read_paths, "touchedPaths": touched_paths})
                cur["endSeq"] = ev["seq"]
                if has_pos:
                    cur["posEnd"] = pos
                cur["toolNames"].extend(names)
                cur["readPaths"].extend(read_paths)
                if not is_expl:
                    cur["touchedPaths"].extend(touched_paths)
            else:
                flush()
                ep = {}
                if is_expl:
                    expl_count += 1
                    ep["name"] = "expl-%d" % expl_count
                    ep["type"] = "expl"
                    ep["batches"] = [{"seq": ev["seq"], "names": names, "readPaths": read_paths, "touchedPaths": []}]
                    ep["touchedPaths"] = []
                else:
                    act_count += 1
                    ep["name"] = "act-%d" % act_count
                    ep["type"] = "act"
                    ep["batches"] = [{"seq": ev["seq"], "names": names, "readPaths": read_paths, "touchedPaths": touched_paths}]
                    ep["touchedPaths"] = list(touched_paths)
                ep["startSeq"] = ev["seq"]
                ep["endSeq"] = ev["seq"]
                if has_pos:
                    ep["posStart"] = pos
                    ep["posEnd"] = pos
                ep["toolNames"] = list(names)
                ep["readPaths"] = list(read_paths)
                ep["deps"] = []
                ep["resultSeqs"] = []
                ep["completed"] = False
                cur = ep
        elif etype == "tool/result":
            if cur is not None:
                cur["endSeq"] = ev["seq"]
                cur["resultSeqs"].append(ev["seq"])
                if has_pos:
                    cur["posEnd"] = pos
    flush()

    expls = [e for e in episodes if e["type"] == "expl"]
    read_sets = [set(e["readPaths"]) for e in expls]
    for ep in episodes:
        if ep["type"] != "act":
            continue
        touched = set(ep["readPaths"])
        touched.update(ep["touchedPaths"] or [])
        for i in range(len(expls)):
            if expls[i]["endSeq"] < ep["startSeq"] and any(p in read_sets[i] for p in touched):
                ep["deps"].append(expls[i]["name"])
    return episodes


def pickEvictionTarget(events, surface, newestAllowed, opts=None):
    """lib.js pickEvictionTarget — select the next evictable episode.

    Structural fidelity with the scoring substitution of Ruling 2 (§R1):
    `EVICTION_PREFERENCE = "oldest-act"` — ACT episodes rank before EXPL
    (lib.js ranked expl first: expl=0 < act=10); an EXPL whose dependent ACTs
    are still on the surface is NOT a candidate at all (lib.js scored it 20 —
    soft; §R1 item 2 makes it hard). opts (carried from lib.js):
      order      ORDER_DEFAULT ('oldest') | 'tail'
      tailWindow only consider episodes ending within the last N surface nodes
      exclude    set of startSeqs to skip
    """
    if opts is None:
        opts = {}
    order = opts.get("order", ORDER_DEFAULT)
    tail_window = opts.get("tailWindow", 0)
    if tail_window is None:
        tail_window = 0
    exclude = opts.get("exclude")

    episodes = deriveEpisodes(events, {"surface": surface})
    surface_set = set(surface)

    # Effective deps (3.3 time-window protection): count only expls whose
    # referencing act is still on the surface. (Carried exactly.)
    depended_active = set()
    for ep in episodes:
        if ep["type"] != "act" or ep["startSeq"] not in surface_set:
            continue
        for d in ep["deps"] or []:
            depended_active.add(d)

    sorted_surface = sorted(surface)
    if tail_window > 0 and len(sorted_surface) > tail_window:
        tail_floor = sorted_surface[len(sorted_surface) - 1 - tail_window]
    else:
        tail_floor = -1

    best = None
    for ep in episodes:
        if not ep.get("completed"):
            continue
        if exclude is not None and ep["startSeq"] in exclude:
            continue
        if ep["startSeq"] not in surface_set:
            continue
        if ep["endSeq"] > newestAllowed:
            continue
        if tail_floor >= 0 and ep["endSeq"] < tail_floor:
            continue
        # §R1 item 6: prologue / never-evict class is never a candidate.
        if ep.get("type") in NEVER_EVICT_TYPES or ep.get("neverEvict"):
            continue
        # §R1 item 2: EXPL is evictable only after every ACT that depended on
        # it is fully evicted (lib.js's soft score 20 is superseded to a hard
        # exclusion — A-POLICY: "an EXPL with a live dependent ACT is never
        # selected").
        if ep["type"] == "expl" and ep["name"] in depended_active:
            continue
        # §R1 item 3 (Ruling 2): oldest ACT first on equal eligibility;
        # EXPL after its dependent ACTs. lib.js base (expl=0 < act=10) is
        # overridden to act=0 < expl=10.
        if EVICTION_PREFERENCE == "oldest-act":
            base = 0 if ep["type"] == "act" else 10
        else:
            base = 0 if ep["type"] == "expl" else 10
        if order == "tail":
            score = base - ep["endSeq"] / 1e9
        else:
            score = base + ep["startSeq"] / 1e9
        if best is None or score < best["score"]:
            best = dict(ep)
            best["score"] = score
    if best is None:
        return None
    return {
        "start": best["startSeq"],
        "end": best["endSeq"],
        "label": best["name"],
        "type": best["type"],
        "readPaths": best["readPaths"] or [],
    }


def newestEvictableBoundary(surface, episodes, preserveRecent=PRESERVE_RECENT):
    """lib.js newestEvictableBoundary — boundary protecting the active chain.

    Returns the boundary seq (completed segments <= it are evictable); -1 when
    the protected tail covers everything or the surface is empty.
    """
    if not surface:
        return -1
    active = None
    for e in reversed(episodes):
        if not e.get("completed"):
            active = e
            break
    if active is not None and active.get("posStart") is not None:
        protect_from = max(0, active["posStart"] - preserveRecent)
    else:
        protect_from = max(0, len(surface) - preserveRecent)
    if protect_from <= 0:
        return -1
    idx = protect_from - 1
    if 0 <= idx < len(surface):
        v = surface[idx]
        return -1 if v is None else v
    return -1


def largeResultSeqs(events, episode, surface, threshold=SURFACE_THRESHOLD_CHARS):
    """lib.js largeResultSeqs — oversized tool/result nodes in an episode.

    Returns [{seq, chars}] for on-surface tool/result seqs whose text length
    (UTF-16 code units) reaches `threshold` (default SURFACE_THRESHOLD_CHARS).
    """
    in_surface = set(surface)
    by_seq = {}
    for e in events:
        if e.get("type") == "tool/result":
            by_seq[e["seq"]] = e
    out = []
    for seq in episode.get("resultSeqs") or []:
        if seq not in in_surface:
            continue
        ev = by_seq.get(seq)
        text = toolResultText(ev)
        if text and _js_str_len(text) >= threshold:
            out.append({"seq": seq, "chars": _js_str_len(text)})
    return out


def toolResultText(ev):
    """lib.js toolResultText — text content of a tool/result node."""
    try:
        data = ev.get("data") if isinstance(ev, dict) else None
        message = data.get("message") if isinstance(data, dict) else None
        content = (message or {}).get("content") if isinstance(message, dict) else None
        if content is None:
            content = []
        block = content[0] if content else None
        inner = block.get("content") if isinstance(block, dict) else None
        if inner is None:
            inner = []
        parts = []
        for b in inner:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                parts.append("" if t is None else t)
        return "".join(parts)
    except Exception:
        return ""


def stubToolResultData(originalEvent, stubText):
    """lib.js stubToolResultData — replace message.content[0].content only.

    Returns a same-shape data object; every other field is deep-equal to the
    original (harness assertToolResultRewrite contract).
    """
    d = originalEvent["data"]
    block = d["message"]["content"][0]
    new_block = dict(block)
    new_block["content"] = [{"type": "text", "text": stubText}]
    new_message = dict(d["message"])
    new_message["content"] = [new_block]
    out = dict(d)
    out["message"] = new_message
    return out


def episodeBlock(surface, ep):
    """lib.js episodeBlock — full position block of an episode on the surface."""
    if not ep:
        return []
    pos_start = ep.get("posStart")
    pos_end = ep.get("posEnd")
    if pos_start is None or pos_end is None:
        return []
    if pos_start < 0 or pos_end >= len(surface) or pos_start > pos_end:
        return []
    return list(surface[pos_start : pos_end + 1])


def mergeBlocksByPos(blocks):
    """lib.js mergeBlocksByPos — merge position-adjacent eviction windows."""
    sorted_blocks = sorted(blocks, key=lambda b: b["posStart"])
    merged = []
    for b in sorted_blocks:
        if merged and merged[-1]["posEnd"] + 1 == b["posStart"]:
            merged[-1]["posEnd"] = b["posEnd"]
            merged[-1]["labels"].append(b["label"])
        else:
            merged.append({"posStart": b["posStart"], "posEnd": b["posEnd"], "labels": [b["label"]]})
    return merged


def pairingBreaks(events, surface, start, end):
    """lib.js pairingBreaks — tool-call/result pairs split by [start, end].

    Returns [{callId, calls, results}] for pairs on both sides of the window;
    empty list = the window is pair-complete and evictable.
    """
    win = shadowedNodes(surface, start, end)
    if not win:
        return []
    win_set = set(win)
    by_seq = {}
    for e in events:
        if e.get("type") in ("assistant/message", "tool/result"):
            by_seq[e["seq"]] = e

    # lib.js's callInWin/resultInWin pre-scan maps and its comment-only loop
    # are omitted (recorded adaptation 3) — output-equivalent.
    all_ids = {}
    for s in surface:
        ev = by_seq.get(s)
        if not ev:
            continue
        if ev.get("type") == "assistant/message":
            data = ev.get("data") or {}
            message = data.get("message") or {}
            for b in message.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool-call" and b.get("id"):
                    rec = all_ids.setdefault(b["id"], {"calls": [], "results": []})
                    rec["calls"].append(s)
        elif ev.get("type") == "tool/result":
            data = ev.get("data") or {}
            message = data.get("message") or {}
            content = message.get("content") or []
            block = content[0] if content else None
            id_ = block.get("toolCallId") if isinstance(block, dict) else None
            if id_:
                rec = all_ids.setdefault(id_, {"calls": [], "results": []})
                rec["results"].append(s)

    breaks = []
    for id_, rec in all_ids.items():
        if not rec["calls"] or not rec["results"]:
            continue
        call_in = any(s in win_set for s in rec["calls"])
        call_out = any(s not in win_set for s in rec["calls"])
        res_in = any(s in win_set for s in rec["results"])
        res_out = any(s not in win_set for s in rec["results"])
        if (call_in and res_out) or (res_in and call_out):
            breaks.append({"callId": id_, "calls": rec["calls"], "results": rec["results"]})
    return breaks


def shadowedNodes(surface, start, end):
    """lib.js shadowedNodes — position-slice semantics of surface replace."""
    try:
        si = surface.index(start)
    except ValueError:
        return []
    try:
        ei = surface.index(end)
    except ValueError:
        return []
    if si >= 0 and ei >= 0 and si <= ei:
        return list(surface[si : ei + 1])
    return []


def mergeRanges(ranges):
    """lib.js mergeRanges — merge adjacent/overlapping ranges (E2 batching)."""
    sorted_ranges = sorted(ranges, key=lambda r: r["start"])
    merged = []
    for r in sorted_ranges:
        if merged and r["start"] <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(merged[-1]["end"], r["end"])
            merged[-1]["labels"].append(r["label"])
        else:
            merged.append({"start": r["start"], "end": r["end"], "labels": [r["label"]]})
    return merged
