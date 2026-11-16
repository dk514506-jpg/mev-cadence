"""R2 decompiler (G-C, CHARGE-G1-BUILD-ENGINE-V2 §R2) — D5 design of record.

A background process captures the full context as a snapshot; the agent NEVER
writes the narrative.  Option A (pure mechanical cut, zero model) is the CORE;
Option C (model adds only labeled links between atoms) is the GROWTH path and
is NOT built here.  Links under the A core are STRUCTURAL ONLY (sequence +
containment); any model invocation on the decompiler path is a gate failure
(C2/A5).

Spine (§R2, binding): the snapshot is captured whole with its hash;
decomposition proceeds cut -> type -> index -> link, each stage landing as its
own gate-checked step under worktree reports/decompiler/ (live Engraphis
landing is OUT OF SCOPE); every atom carries the provenance chain (snapshot
hash -> character range -> turn id -> parent atoms); recomposition reassembles
the original byte-identically; recombination synthesizes new orderings without
touching the original.

Atoms are the disjoint leaf pieces at the snapshot's recorded seams (one per
recorded segment: user | assistant | tool_call | tool_result), so every atom
is a recomposition unit — mutating any single atom visibly changes
recomposition (the A5 falsifier).  The parent pieces of an atom are its
enclosing turn pieces, named in the provenance chain and carried in the
containment link graph ("turn-<id>" nodes).

Atom well-formedness at the `type` stage is the D6 crystallization rubric
(DECISION-PACKAGE §4; the rubric file is named in reports/fixtures_manifest.json
— see RUBRIC_FILE below): byte-verbatim external-record span (C1/C5),
resolvable provenance chain (C2), exactly one closed-set type per atom with
zero untagged atoms (C8), user content never retyped (C7).  A model-authored
atom — or any atom whose text is not the byte-verbatim span of its provenance
range — is REJECTED (A5).

Input: `decompiler_snapshot` fixtures (flat text + recorded turn boundaries
and per-segment character offsets).  Pure stdlib; deterministic; LLM-free.
"""

import hashlib
import json
import os

# D6 rubric of record (§R2d).  Must match reports/fixtures_manifest.json
# `rubric.file` / `rubric.section`.
RUBRIC_FILE = "reading-campaign-2026-11-02/_campaign/CRYSTALLIZATION-PACKAGE-V1.6.md"
RUBRIC_SECTION = "Section 3 RUBRIC SELF-CHECK (C1-C10, C-A8, C-A9 criteria)"

SNAPSHOT_KIND = "decompiler_snapshot"

# Closed set of atom types (C8: exactly one label per atom, zero untagged).
LEAF_TYPES = ("user", "assistant", "tool_call", "tool_result")
ATOM_TYPES = LEAF_TYPES

STAGES = ("cut", "type", "index", "link")

# Truthy markers of model authorship — rejected outright (A5 / C2).
_MODEL_MARKERS = ("model_authored", "authored_by", "generated_by")


class DecompileError(Exception):
    """Structural gate failure on the decompiler path."""


class AtomRejected(DecompileError):
    """An atom failed the D6 well-formedness rubric (A5)."""


def canonical_bytes(obj):
    """Canonical bytes per the fixture-manifest contract: sorted-key compact
    JSON, UTF-8, no ASCII escaping."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def snapshot_sha256(snapshot):
    """Hash of the snapshot captured whole: sha256 hex over the text bytes."""
    text = snapshot.get("text")
    if not isinstance(text, str):
        raise DecompileError("snapshot text must be a string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def turn_node_id(turn_id):
    """Link-graph id of a turn piece (the containment parent of its atoms)."""
    return "turn-%s" % turn_id


# --- stage 1: cut ---------------------------------------------------------

def cut(snapshot):
    """Mechanical cut at the snapshot's recorded segment offsets.

    Returns one atom per recorded segment, in document order, carrying the
    provenance chain seeded (snapshot hash, character range, turn id; parent
    pieces are filled at the link stage).  Gate: the recorded offsets must
    tile the text exactly and every span must be non-empty.
    """
    text = snapshot.get("text")
    turns = snapshot.get("turns")
    if not isinstance(text, str) or not isinstance(turns, list):
        raise DecompileError("snapshot must carry text and turns")
    sha = snapshot_sha256(snapshot)
    atoms = []
    pos = 0
    for turn in turns:
        t_id = turn.get("turn_id")
        t_start, t_end = turn.get("start"), turn.get("end")
        if not isinstance(t_id, int):
            raise DecompileError("turn_id must be an int")
        if t_start != pos or not isinstance(t_end, int) or t_end <= t_start:
            raise DecompileError(
                "turn %s does not tile the text (expected start %d, got %r)"
                % (t_id, pos, t_start)
            )
        if t_end > len(text):
            raise DecompileError("turn %s ends past the snapshot text" % t_id)
        seg_pos = t_start
        for seg in turn.get("segments") or []:
            s_start, s_end = seg.get("start"), seg.get("end")
            if s_start != seg_pos or not isinstance(s_end, int) or s_end <= s_start:
                raise DecompileError(
                    "segments of turn %s do not tile the turn (expected start "
                    "%d, got %r)" % (t_id, seg_pos, s_start)
                )
            if s_end > t_end:
                raise DecompileError("segment ends past its turn (%s)" % t_id)
            atoms.append({
                "text": text[s_start:s_end],
                "provenance": {
                    "snapshot_sha256": sha,
                    "start": s_start,
                    "end": s_end,
                    "turn_id": t_id,
                    "parents": [],
                },
            })
            seg_pos = s_end
        if seg_pos != t_end:
            raise DecompileError("segments of turn %s leave a gap" % t_id)
        pos = t_end
    if pos != len(text):
        raise DecompileError("turns do not tile the snapshot text")
    if not atoms:
        raise DecompileError("cut produced no atoms")
    return atoms


# --- stage 2: type --------------------------------------------------------

def check_atom(atom, snapshot):
    """D6 rubric check for one atom (§R2d).  Raises AtomRejected on failure.

    C1/C5: text must be the byte-verbatim span of its provenance range.
    C2: the provenance chain must resolve (hash, range, turn id).
    C8: exactly one type from the closed set, zero untagged.
    C7: user content is preserved (recorded user segments stay "user").
    A5: a model-authored atom is rejected.
    """
    if not isinstance(atom, dict):
        raise AtomRejected("atom must be a dict")
    for marker in _MODEL_MARKERS:
        if atom.get(marker):
            raise AtomRejected("model-authored atom rejected (A5): %s" % marker)
    prov = atom.get("provenance")
    if not isinstance(prov, dict):
        raise AtomRejected("atom provenance missing")
    text = snapshot.get("text") or ""
    sha = snapshot_sha256(snapshot)
    if prov.get("snapshot_sha256") != sha:
        raise AtomRejected("provenance snapshot hash does not resolve (C2)")
    start, end = prov.get("start"), prov.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start < end <= len(text)):
        raise AtomRejected("provenance character range out of bounds")
    if atom.get("text") != text[start:end]:
        raise AtomRejected(
            "atom text is not the byte-verbatim span of its provenance range (C1/C5)"
        )
    turn_id = prov.get("turn_id")
    turns = snapshot.get("turns") or []
    turn = next((t for t in turns if t.get("turn_id") == turn_id), None)
    if turn is None:
        raise AtomRejected("provenance turn id does not resolve (C2)")
    if not (turn.get("start") <= start and end <= turn.get("end")):
        raise AtomRejected("atom range escapes its turn")
    kind = None
    for seg in turn.get("segments") or []:
        if (seg.get("start"), seg.get("end")) == (start, end):
            kind = seg.get("kind")
            break
    if kind is None:
        raise AtomRejected("atom range matches no recorded segment")
    atype = atom.get("type")
    if atype not in ATOM_TYPES:
        raise AtomRejected("atom type not in closed set (C8): %r" % (atype,))
    if atype != kind:
        raise AtomRejected(
            "atom type %r does not match recorded kind %r" % (atype, kind)
        )
    if kind == "user" and atype != "user":
        raise AtomRejected("user content must never be retyped (C7)")
    return True


def type_atoms(atoms, snapshot):
    """Assign each cut atom its closed-set type (the snapshot's recorded
    segment kind) and gate it on the D6 rubric.  Returns NEW dicts; inputs
    are never mutated.
    """
    turns = snapshot.get("turns") or []
    out = []
    for atom in atoms:
        prov = atom["provenance"]
        start, end = prov["start"], prov["end"]
        turn_id = prov["turn_id"]
        turn = next((t for t in turns if t.get("turn_id") == turn_id), None)
        if turn is None:
            raise AtomRejected("provenance turn id does not resolve (C2)")
        kind = None
        for seg in turn.get("segments") or []:
            if (seg.get("start"), seg.get("end")) == (start, end):
                kind = seg.get("kind")
                break
        if kind is None or kind not in LEAF_TYPES:
            raise AtomRejected("segment kind not in closed set (C8)")
        typed = {
            "text": atom["text"],
            "type": kind,
            "provenance": {
                "snapshot_sha256": prov["snapshot_sha256"],
                "start": start,
                "end": end,
                "turn_id": turn_id,
                "parents": list(prov.get("parents") or []),
            },
        }
        check_atom(typed, snapshot)
        out.append(typed)
    return out


# --- stage 3: index -------------------------------------------------------

def index_atoms(atoms):
    """Assign deterministic atom ids "atom-%04d" in document order.
    Gate: ids are unique.  Returns NEW dicts.
    """
    out = []
    seen = set()
    for i, atom in enumerate(atoms, start=1):
        atom_id = "atom-%04d" % i
        if atom_id in seen:
            raise DecompileError("duplicate atom id %s" % atom_id)
        seen.add(atom_id)
        prov = atom["provenance"]
        out.append({
            "atom_id": atom_id,
            "text": atom["text"],
            "type": atom["type"],
            "provenance": {
                "snapshot_sha256": prov["snapshot_sha256"],
                "start": prov["start"],
                "end": prov["end"],
                "turn_id": prov["turn_id"],
                "parents": list(prov.get("parents") or []),
            },
        })
    return out


# --- stage 4: link --------------------------------------------------------

def link_atoms(atoms):
    """Structural links ONLY (sequence + containment) — §R2c.

    containment: turn piece ("turn-<id>") -> its atoms (fills
        provenance.parents with the parent pieces).
    sequence: consecutive atoms in document order.
    Gate: the links dict carries exactly these two kinds and every atom is
    covered by both spines.  Returns (NEW atoms, links).
    """
    out = []
    order = []
    containment = []
    for atom in atoms:
        parent = turn_node_id(atom["provenance"]["turn_id"])
        containment.append([parent, atom["atom_id"]])
        order.append(atom["atom_id"])
        prov = atom["provenance"]
        out.append({
            "atom_id": atom["atom_id"],
            "text": atom["text"],
            "type": atom["type"],
            "provenance": {
                "snapshot_sha256": prov["snapshot_sha256"],
                "start": prov["start"],
                "end": prov["end"],
                "turn_id": prov["turn_id"],
                "parents": [parent],
            },
        })
    sequence = [[order[i], order[i + 1]] for i in range(len(order) - 1)]
    links = {"sequence": sequence, "containment": containment}
    if set(links) != {"sequence", "containment"}:
        raise DecompileError("links must be structural only (sequence + containment)")
    if len(order) != len(atoms) or len(containment) != len(atoms):
        raise DecompileError("link spines must cover every atom exactly once")
    return out, links


# --- pipeline -------------------------------------------------------------

def decompose(snapshot, land_dir=None):
    """Full decomposition: cut -> type -> index -> link.

    Each stage is its own gate-checked step (raises on violation).  When
    `land_dir` is set, each stage lands its artifact there as it completes
    (§R2b: the landing target is worktree reports/decompiler/).
    """
    if snapshot.get("kind") not in (None, SNAPSHOT_KIND):
        raise DecompileError("input is not a %s" % SNAPSHOT_KIND)
    fixture_id = snapshot.get("fixture_id") or "snapshot"
    sha = snapshot_sha256(snapshot)

    def _land(stage, payload):
        if land_dir is None:
            return
        os.makedirs(land_dir, exist_ok=True)
        record = {
            "stage": stage,
            "fixture_id": fixture_id,
            "snapshot_sha256": sha,
        }
        record.update(payload)
        path = os.path.join(land_dir, "%s.json" % stage)
        with open(path, "wb") as f:
            f.write(canonical_bytes(record))

    cut_atoms = cut(snapshot)
    _land("cut", {"atoms": cut_atoms})

    typed = type_atoms(cut_atoms, snapshot)
    _land("type", {"atoms": typed})

    indexed = index_atoms(typed)
    _land("index", {"atoms": indexed})

    linked, links = link_atoms(indexed)
    _land("link", {"atoms": linked, "links": links})

    return {
        "fixture_id": fixture_id,
        "snapshot_sha256": sha,
        "atoms": linked,
        "links": links,
    }


def default_land_dir(fixture_id):
    """§R2b landing target: worktree reports/decompiler/<fixture_id>/."""
    base = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return os.path.join(base, "reports", "decompiler", fixture_id)


# --- recomposition / recombination ---------------------------------------

def recompose(atoms):
    """Reassemble the original text byte-identically from the atoms, ordered
    by provenance character range.  Every atom is a recomposition unit (the
    atoms are the disjoint leaf pieces), so mutating any single atom visibly
    changes the result (A5).  Pure: inputs never mutated."""
    ordered = sorted(
        atoms, key=lambda a: (a["provenance"]["start"], a["provenance"]["end"])
    )
    return "".join(a["text"] for a in ordered)


def recompose_bytes(atoms):
    """Recomposition as UTF-8 bytes (the A5 byte-compare oracle)."""
    return recompose(atoms).encode("utf-8")


def recombine(atoms, order):
    """Synthesize the text of a new atom ordering without touching the
    original.  `order` is a list of atom ids; overlapping spans are refused
    (a synthesized ordering must not double-count content)."""
    by_id = {a.get("atom_id"): a for a in atoms}
    spans = []
    pieces = []
    for atom_id in order:
        atom = by_id.get(atom_id)
        if atom is None:
            raise DecompileError("unknown atom id %r" % (atom_id,))
        spans.append((atom["provenance"]["start"], atom["provenance"]["end"], atom_id))
        pieces.append(atom["text"])
    ordered = sorted(spans)
    for i in range(1, len(ordered)):
        if ordered[i][0] < ordered[i - 1][1]:
            raise DecompileError("recombination order overlaps itself")
    return "".join(pieces)


def roundtrip_record(snapshot):
    """The canonical A5 round-trip record for a snapshot: full decomposition
    plus the recomposed text.  Byte-compared (canonical sorted-key compact
    JSON) against tests/g1/fixtures/decompiler/."""
    de = decompose(snapshot)
    return {
        "fixture_id": de["fixture_id"],
        "kind": "decompiler_roundtrip",
        "schema_version": "mev-fixture-v1",
        "feeds": ["A5"],
        "snapshot_sha256": de["snapshot_sha256"],
        "atoms": de["atoms"],
        "links": de["links"],
        "recomposed_text": recompose(de["atoms"]),
    }
