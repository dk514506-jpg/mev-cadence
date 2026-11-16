"""Engine→CLI bridge (G-F, CHARGE-G1-BUILD-ENGINE-V2 §2) — runs EvictionEngine
over a frozen fixture's events + ledger, derives plan.json + slice.json whose
package conforms to emit.build_package / corpus.load_manifest / the 89 field
paths (corpus/v16-field-paths.json), emits the derived package to a NEW output
path under reports/g1_plan/ (never the wave-1 package of record
micro_eviction/crystallizer/out/pkg-wave1-external-record-v1.json), then
validates through the REAL CLI path (python -m micro_eviction.crystallizer.cli
validate): the validate step exits 0 iff fail == 0, and the returned validation
result object carries real fail/pass/not_exercised counts and every
NOT_EXERCISED reason.

Derivation rule (binding): the package is DERIVED from staged episodes.
The episode source is the PROPOSAL artifact (G1FIX-4 / A-D4): derive_plan_and_slice
FIRST lands the mechanical proposal via episodes.write_proposal, reads it
back, verifies its content_sha256 recomputes, and uses the landed proposal's
episodes (never a fresh in-process re-derivation).  Every episode fully
staged by the engine (EvictionDecision action == stage_episode) contributes
one span payload (its canonical episode + decision record + the verbatim raw
tool-result text of its bulk-stripped seqs, so the package holds raw —
A-STRIP), one EPISODE atom over that span, and one named_items residual whose
exact_value is the episode name (byte-present in its own atom text).  For
each staged episode the bridge also binds the id namespaces (A3/C5): a
content-derived staged id minted via staging_registry.mint_package_id is
mapped to the provisional ledger id in the package-metadata id_mappings.
The plan's provenance block records engine participation and the proposal
hash; seeded fixture inputs (events + derived_ledger) are used ONLY because
the provenance block says so (seeded_fixture_inputs: true).  Host reporting
vocabulary: provenance says "staged", never the banned C6 token.

C10 (fidelity, binding): plan.json and the emitted package carry the
engine-side package-metadata block (fidelity: LOSSY + lossiness note with
message_raw/cot_raw NAMED but unpopulated).  It is NOT a V1.6 schema field —
it rides this bridge's own emit path, injected after build_package.

Byte-identity: plan.json and the package's canonical bytes are pure functions
of (events, ledger, tau, query_time_epoch) — no wall clock reads on the emit
path.  Two runs over the same frozen fixture are byte-identical; injecting a
timestamp into the canonical bytes breaks that identity.

The validation result object is the anti-stub seam (A7): it carries the CLI
report's real summary counts.  A validate step stubbed to "always return 0"
cannot produce fail > 0 for a seeded empty/degenerate plan, so such a stub is
detectable by asserting the counts.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from ..crystallizer.corpus import CAMPAIGN_ROOT, CorpusError, confine
from ..crystallizer.emit import build_package, canonical_bytes
from ..dsh_cwl.lib_port import toolResultText
from . import episodes, staging_registry
from .engine import STRIP_LEVEL_BULK, STRIP_LEVEL_FULL, EvictionEngine, canonical_decisions

QUERY_TIME_EPOCH = 1793664000.0  # wave-1 query_time_epoch (D-10)
CREATED_UTC = "2026-11-04T00:00:00Z"  # fixed (no wall clock on the emit path)
PLAN_NAME = "plan.json"
SLICE_NAME = "slice.json"
PKG_NAME = "pkg-g1-engine-plan-v1.json"
WAVE1_PKG_OF_RECORD = (
    "micro_eviction/crystallizer/out/pkg-wave1-external-record-v1.json"
)
DEFAULT_SPAN_DIR = CAMPAIGN_ROOT / "reports" / "g1_plan" / "spans"

RESIDUAL_DECAY = {
    "eps_prune": 0.5,
    "layer": "lexical",
    "strength": 1.0,
    "t_max": 86400.0,
}
RESIDUAL_INTERVAL = {
    "t_start": QUERY_TIME_EPOCH,
    "t_end": 1825200000.0,
}

# C10 (fidelity, binding): the engine-side package-metadata block carried by
# BOTH plan.json and the emitted package.  NOT a V1.6 schema field — the
# bridge's own emit path injects it after build_package (the C10-resolution
# form).  The raw message and CoT fields are NAMED but not populated.
PACKAGE_METADATA_KEY = "package_metadata"
# A3/C5 id binding: one {"ledger_package_id", "staged_package_id"} pair per
# staged episode — the stable-id resolution across the two namespaces.
# Minted ids (pkg-<sha12>) are content-derived and unique per episode;
# ledger ids (pkg-%04d) are provisional (C5's per-turn form).
ID_MAPPINGS_KEY = "id_mappings"
PACKAGE_METADATA = {
    "fidelity": "LOSSY",
    "lossiness_note": (
        "eviction packages are lossy: message_raw and cot_raw are "
        "named but not populated"
    ),
    "message_raw": None,
    "cot_raw": None,
}

# V1.6 contract gate set — the 13 registered assertions (validate.CHECK_ORDER).
# Carried as the package's checks[] so C9a/C9c bind to the real registry; the
# knowledge content (atoms/residuals/source) is derived from staged episodes.
_GATE_ROWS = (
    ("C1", "9a", "paraphrased atom text",
     "a byte-verbatim atom differs from its span", "BYTE_PRESENT", "atoms[]"),
    ("C2", "9a", "unresolvable evidence link",
     "dangling pointer or orphan abstraction", "POINTER_RESOLVES",
     "evidence_index"),
    ("C3", "9a", "token absent from residuals",
     "missing residual or empty verified_facts", "BYTE_PRESENT",
     "residuals[]"),
    ("C4", "9a", "recomputed hash differs",
     "fingerprint mismatch claimed fresh", "HASH_EQ", "freshness"),
    ("C5", "9a", "non-empty unified diff",
     "dropped or rewritten external entry", "BYTE_PRESENT",
     "source.external_record"),
    ("C6", "9a", "depended-upon-first order",
     "ineligible eviction order", "POINTER_RESOLVES", "dag"),
    ("C7", "9a", "coverage < 100%", "missing in-scope user span",
     "BYTE_PRESENT", "user_content"),
    ("C8", "9a", "seeded-constraint miss", "untagged or double-tagged atom",
     "TAG_CLOSED_SET", "atoms[]"),
    ("C9a", "9a", "seeded run exits 0", "executing check fails clean run",
     "BYTE_PRESENT", "checks[]"),
    ("C9b", "9b", "no V1.6 review_recorded field", "missing review record",
     "SEMANTIC_REVIEW", "*"),
    ("C9c", "9c", "checks[] empty", "empty check set or unbound pair",
     "SANDBOX_RULE", "*"),
    ("E-A8", "9a", "agent-only boundary", "triple-incomplete boundary",
     "BOUNDARY_TRIPLE", "boundaries[]"),
    ("E-A9", "9a", "both lifecycle fields null",
     "bare residual or stale trusted", "LIFECYCLE_STALE", "residuals[]"),
)


def _canonical_plan_bytes(plan: dict) -> bytes:
    return (
        json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _canonical_slice_bytes(manifest: dict) -> bytes:
    return (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def contract_gate_checks() -> list:
    """The fixed V1.6 gate set (13 registered checks) as plan check rows."""
    checks = []
    for cid, cls, falsifier, fires_on, kind, target in _GATE_ROWS:
        pred = {"kind": kind, "target": target}
        if cid == "C9b":  # SEMANTIC_REVIEW locator (D-17 required_one_of)
            pred["formal_id"] = "C9"
        checks.append({
            "check_id": cid,
            "class": cls,
            "falsifier": falsifier,
            "fires_on": fires_on,
            "predicate": pred,
            "sandbox_required": cid == "C9a",
            "status": "PROPOSED GATE",
        })
    return checks


def _episode_record(ep: dict, decisions: list, raw_by_seq=None) -> dict:
    """Canonical derived record for one staged episode (atom text payload).

    A-STRIP raw-in-package: the record carries the raw tool-result text of
    every bulk-stripped seq of the episode (the content the engine took off
    the surface), so the package/span holds it.
    """
    bulk_raw = []
    for d in decisions:
        if d.action != STRIP_LEVEL_BULK:
            continue
        for s in d.seqs:
            raw = (raw_by_seq or {}).get(s)
            if raw is not None:
                bulk_raw.append({"seq": s, "text": raw})
    bulk_raw.sort(key=lambda item: item["seq"])
    record = {
        "episode": ep.get("name", ""),
        "type": ep.get("type", ""),
        "startSeq": ep.get("startSeq"),
        "endSeq": ep.get("endSeq"),
        "completed": bool(ep.get("completed")),
        "deps": list(ep.get("deps") or []),
        "batches": list(ep.get("batches") or []),
        "resultSeqs": list(ep.get("resultSeqs") or []),
        "toolNames": list(ep.get("toolNames") or []),
        "readPaths": list(ep.get("readPaths") or []),
        "touchedPaths": list(ep.get("touchedPaths") or []),
        "package_id": decisions[0].package_id if decisions else "",
        "decisions": [d.to_dict() for d in decisions],
    }
    if bulk_raw:
        record["bulk_stripped_raw"] = bulk_raw
    return record


def read_verified_proposal(path):
    """Read a landed proposal file back and verify its content_sha256
    recomputes over the canonical episode content.

    This is the bridge's read-back verification (A-D4): a tampered landed
    file (modified episodes, or a seeded hash mismatch) FAILS here —
    raises ValueError.  Returns the landed proposal record.
    """
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        landed = json.load(f)
    eps = landed.get("episodes")
    canonical = json.dumps({"episodes": eps}, sort_keys=True,
                           separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != landed.get("content_sha256"):
        raise ValueError(
            "proposal read-back verification failed: content_sha256 mismatch "
            "(recomputed %s, recorded %s)" % (digest, landed.get("content_sha256"))
        )
    return landed


def derive_plan_and_slice(events, ledger, *, tau, fixture_id="frozen-fixture",
                          span_dir=None, out_dir=None,
                          query_time_epoch=QUERY_TIME_EPOCH, alarms_dir=None):
    """Run EvictionEngine over the frozen fixture and derive plan + slice.

    Writes one span payload per staged episode under `span_dir` (must live
    inside the campaign root; default reports/g1_plan/spans).  FIRST lands
    the mechanical episode proposal under `out_dir` (default
    <span_dir>/../proposals) and uses the landed, hash-verified proposal
    file as the episode source (A-D4).  Returns (plan, manifest) dicts; no
    wall-clock reads on the emit path — byte-stable for a fixed
    (events, ledger, tau, query_time_epoch).
    """
    if span_dir is None:
        span_dir = DEFAULT_SPAN_DIR
    span_dir = Path(span_dir)
    if out_dir is None:
        out_dir = span_dir.parent / "proposals"
    out_dir = Path(out_dir)

    # A-D4: the proposal artifact lands FIRST and is the episode source.
    # Read the landed file back and verify the content hash recomputes.
    record = episodes.write_proposal(events, out_dir=str(out_dir))
    proposal_path = out_dir / ("proposal-%s.json" % record["content_sha256"][:12])
    landed = read_verified_proposal(proposal_path)
    proposal_sha = landed["content_sha256"]
    derived_episodes = landed["episodes"]

    staging_registry.reset()
    for row in ledger:
        if row[2]:
            staging_registry.register(row[2])

    engine = EvictionEngine(events, ledger, tau=tau)
    surface = [e["seq"] for e in events]
    decisions, new_surface, alarms = engine.run(surface, alarms_dir=alarms_dir)

    by_episode: dict = {}
    for d in decisions:
        by_episode.setdefault(d.episode, []).append(d)

    # A-D4: episode source is the landed proposal (read+verified above).
    staged_eps = [
        ep for ep in derived_episodes
        if any(d.action == STRIP_LEVEL_FULL
               for d in by_episode.get(ep.get("name", ""), []))
    ]
    staged_eps.sort(key=lambda ep: (ep.get("startSeq", 0), ep.get("name", "")))
    if not staged_eps:
        raise ValueError(
            "derive_plan_and_slice: no fully staged episodes at tau=%r "
            "(engine made no %s decisions)" % (tau, STRIP_LEVEL_FULL)
        )

    span_dir.mkdir(parents=True, exist_ok=True)
    try:
        span_dir.resolve().relative_to(CAMPAIGN_ROOT.resolve())
    except ValueError:
        raise CorpusError(f"span_dir escapes campaign root: {span_dir}")

    raw_by_seq = {e["seq"]: toolResultText(e) for e in events
                  if e.get("type") == "tool/result"}
    atoms = []
    residuals = []
    spans = []
    files = {}
    source_episodes = []
    id_mappings = []
    for i, ep in enumerate(staged_eps, start=1):
        name = ep.get("name", "")
        decisions_for_ep = by_episode.get(name, [])
        record = _episode_record(ep, decisions_for_ep, raw_by_seq)
        source_episodes.append(record)
        # A3/C5 id binding: the content-derived staged id (minted and
        # registered in staging) is mapped to the provisional ledger id.
        id_mappings.append({
            "ledger_package_id": record.get("package_id", ""),
            "staged_package_id": staging_registry.mint_package_id(ep),
        })
        text = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        span_id = "evm-%s" % name
        span_file = span_dir / ("%s.txt" % span_id)
        # A-STRIP raw-in-package: the raw tool-result text of bulk-stripped
        # seqs rides the span payload verbatim (byte-present in the file).
        raw_concat = "".join(
            item["text"] for item in record.get("bulk_stripped_raw") or [])
        payload = (text + raw_concat).encode("utf-8")
        span_file.write_bytes(payload)
        rel = span_file.resolve().relative_to(CAMPAIGN_ROOT.resolve())
        rel_s = str(rel)
        confine(rel_s)  # fail-closed confinement + symlink checks
        files[rel_s] = hashlib.sha256(payload).hexdigest()
        spans.append({
            "span_id": span_id,
            "file": rel_s,
            "byte_start": 0,
            "byte_end": len(payload),
            "text_sha256": hashlib.sha256(payload).hexdigest(),
            "expected_residuals": [
                {"kind": "named_items", "exact_value": name}
            ],
        })
        atom_id = "EP%d" % i
        residual_id = "R%d" % i
        atoms.append({
            "atom_id": atom_id,
            "span_id": span_id,
            "retention_type": "EPISODE",
            "residual_refs": [residual_id],
        })
        residuals.append({
            "residual_id": residual_id,
            "kind": "named_items",
            "exact_value": name,
            "source_atom_id": atom_id,
            "invalidation_rule": None,
            "decay": dict(RESIDUAL_DECAY),
            "interval": dict(RESIDUAL_INTERVAL),
        })

    decision_blob = canonical_decisions(decisions)
    decision_sha = hashlib.sha256(decision_blob.encode("utf-8")).hexdigest()
    plan = {
        "atoms": atoms,
        "boundaries": [],
        "checks": contract_gate_checks(),
        "concept_map": [],
        "identity": {
            "build_run_id": "g1-engine-to-plan-%s" % decision_sha[:12],
            "corpus_refs": [SLICE_NAME],
            "created_utc": CREATED_UTC,
            "pkg_id": "pkg-g1-engine-plan-v1",
        },
        "manifest": SLICE_NAME,
        "package_metadata": {
            **PACKAGE_METADATA,
            ID_MAPPINGS_KEY: id_mappings,
        },
        "plan_id": "g1-engine-to-plan-v1",
        "query_time_epoch": float(query_time_epoch),
        "residuals": residuals,
        "source": {
            "episodes": source_episodes,
            "external_record": {},
            "user_spans": [],
        },
        "provenance": {
            "engine_participated": True,
            "engine_module": "micro_eviction.eviction.engine",
            "engine_class": "EvictionEngine",
            "engine_entrypoint": "EvictionEngine(events, ledger, tau).run(surface)",
            "decision_count": len(decisions),
            "decision_sha256": decision_sha,
            "proposal_sha256": proposal_sha,
            "staged_episodes": [ep.get("name", "") for ep in staged_eps],
            "alarm_kinds": [a.get("kind", "") for a in alarms],
            "seeded_fixture_inputs": True,
            "fixture_id": fixture_id,
            "fixture_input_note": (
                "seeded Stage-0 fixture inputs (events + derived_ledger rows) "
                "are permitted because this provenance block records them as "
                "seeded"
            ),
            "tau": tau,
            "derived_from": (
                "staged episodes (engine-derived decisions; host reports "
                "staging)"
            ),
            "checks_source": (
                "V1.6 contract gate set (13 registered assertions)"
            ),
            "wave1_record_excluded": WAVE1_PKG_OF_RECORD,
            "new_surface_len": len(new_surface),
        },
    }
    manifest = {
        "manifest_version": "g1-engine-to-plan-v1",
        "trailing_newline_policy": (
            "slices are end-exclusive and never include a trailing newline"
        ),
        "files": [
            {"path": p, "sha256": h} for p, h in sorted(files.items())
        ],
        "spans": spans,
    }
    return plan, manifest


def _validate_via_cli(package_path: Path, manifest_path: Path,
                      query_time: float) -> dict:
    """Run the REAL CLI validate path and parse its report.

    Exit contract (D-09): the step exits 0 iff fail == 0.  The result object
    carries the report's real summary counts and every NOT_EXERCISED reason.
    """
    argv = [
        sys.executable, "-m", "micro_eviction.crystallizer.cli", "validate",
        "--package", str(package_path),
        "--manifest", str(manifest_path),
        "--query-time", repr(float(query_time)),
    ]
    env = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": str(CAMPAIGN_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    proc = subprocess.run(
        argv, cwd=str(CAMPAIGN_ROOT), env=env,
        capture_output=True, text=True, timeout=300,
    )
    report = None
    error = None
    try:
        parsed = json.loads(proc.stdout)
        if isinstance(parsed, dict) and "summary" in parsed:
            report = parsed
        else:
            error = "validate CLI stdout is not a report: %r" % (
                proc.stdout[:200],)
    except json.JSONDecodeError as e:
        error = "validate CLI emitted no JSON report: %s" % e
    if report is not None:
        summary = dict(report["summary"])
        reasons = {
            c.get("check_id", ""): c.get("reason", "")
            for c in report.get("checks", [])
            if c.get("state") == "NOT_EXERCISED"
        }
        exit_iff = (proc.returncode == 0) == (summary.get("fail", -1) == 0)
    else:
        summary = {"fail": 1, "pass": 0, "not_exercised": 0, "stale_demoted": 0}
        reasons = {}
        exit_iff = (proc.returncode != 0)
    return {
        "exit": proc.returncode,
        "summary": summary,
        "not_exercised_reasons": reasons,
        "exit_iff_no_fail": exit_iff,
        "report": report,
        "error": error,
        "command": " ".join(argv),
    }


def _require_package_metadata(plan: dict) -> dict:
    """The bridge's own C10 + A3/C5 validation: the plan must carry the
    engine-side package-metadata block with exactly the charged fixed values
    and a well-formed one-to-one id-mapping set."""
    meta = plan.get(PACKAGE_METADATA_KEY)
    if not isinstance(meta, dict):
        raise ValueError(
            "plan missing the package-metadata block (C10: fidelity: LOSSY "
            "+ lossiness note + named unpopulated raw fields)")
    for key, want in PACKAGE_METADATA.items():
        if meta.get(key) != want:
            raise ValueError(
                "plan package-metadata %s is %r, want %r (C10: fidelity: "
                "LOSSY + lossiness note + named unpopulated raw fields)"
                % (key, meta.get(key), want))
    mappings = meta.get(ID_MAPPINGS_KEY)
    if not isinstance(mappings, list) or not mappings:
        raise ValueError(
            "plan package-metadata missing id_mappings (A3/C5 id binding)")
    # One-to-one on the staged (minted) side: each minted id belongs to
    # exactly one staged episode.  Ledger ids are provisional (C5's form,
    # per-turn) and may repeat across episodes of the same turn.
    seen_staged = set()
    for m in mappings:
        if (not isinstance(m, dict)
                or sorted(m) != ["ledger_package_id", "staged_package_id"]):
            raise ValueError("bad id mapping entry: %r" % (m,))
        led, stg = m["ledger_package_id"], m["staged_package_id"]
        if not isinstance(led, str) or not led or not isinstance(stg, str) or not stg:
            raise ValueError("bad id mapping entry: %r" % (m,))
        if stg in seen_staged:
            raise ValueError("id mapping is not one-to-one: %r" % (m,))
        seen_staged.add(stg)
    return meta


def emit_and_validate(plan: dict, manifest: dict, out_dir, *,
                      manifest_path=None, query_time_epoch=None) -> dict:
    """Build the package through emit.build_package, write it under `out_dir`,
    then validate through the REAL CLI path.

    The result's ``validation`` object always carries real fail/pass/
    not_exercised counts: emit-contract failures are recorded as fail > 0
    (never a vacuous pass), and validation failures carry the CLI report's
    live counts.
    """
    out_dir = Path(out_dir)
    if query_time_epoch is None:
        query_time_epoch = plan.get("query_time_epoch", QUERY_TIME_EPOCH)
    result = {
        "emit_ok": False,
        "emit_error": None,
        "package_path": None,
        "package_sha256": None,
        "validation": {
            "exit": 1,
            "summary": {"fail": 1, "pass": 0, "not_exercised": 0,
                        "stale_demoted": 0},
            "not_exercised_reasons": {},
            "exit_iff_no_fail": True,
            "report": None,
            "error": None,
        },
    }
    try:
        pkg = build_package(plan, manifest, query_time_epoch)
        meta = _require_package_metadata(plan)
        pkg[PACKAGE_METADATA_KEY] = dict(meta)
        pkg_bytes = canonical_bytes(pkg)
    except Exception as e:  # emit-contract failure counts as fail (A7)
        result["emit_error"] = "%s: %s" % (type(e).__name__, e)
        result["validation"]["error"] = result["emit_error"]
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    pkg_path = out_dir / PKG_NAME
    pkg_path.write_bytes(pkg_bytes)
    result["emit_ok"] = True
    result["package_path"] = str(pkg_path)
    result["package_sha256"] = hashlib.sha256(pkg_bytes).hexdigest()

    if manifest_path is None:
        manifest_path = out_dir / SLICE_NAME
        Path(manifest_path).write_bytes(_canonical_slice_bytes(manifest))
    result["validation"] = _validate_via_cli(
        Path(pkg_path), Path(manifest_path), float(query_time_epoch))
    return result


def run_engine_bridge(events, ledger, out_dir, *, tau, fixture_id="frozen-fixture",
                      span_dir=None, query_time_epoch=QUERY_TIME_EPOCH, alarms_dir=None) -> dict:
    """Full G-F pipeline: engine -> plan.json + slice.json -> package -> CLI
    validate.  Writes plan.json and slice.json under `out_dir`; the package is
    emitted to a NEW output path (PKG_NAME), never the wave-1 record.  The
    episode-proposal artifact lands under `out_dir` FIRST (A-D4) and is
    consumed (read back + hash-verified) as the episode source.
    """
    out_dir = Path(out_dir)
    plan, manifest = derive_plan_and_slice(
        events, ledger, tau=tau, fixture_id=fixture_id, span_dir=span_dir,
        out_dir=out_dir, query_time_epoch=query_time_epoch, alarms_dir=alarms_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir / PLAN_NAME
    slice_path = out_dir / SLICE_NAME
    plan_path.write_bytes(_canonical_plan_bytes(plan))
    slice_path.write_bytes(_canonical_slice_bytes(manifest))
    proposal_path = out_dir / (
        "proposal-%s.json" % plan["provenance"]["proposal_sha256"][:12])

    result = emit_and_validate(
        plan, manifest, out_dir, manifest_path=slice_path,
        query_time_epoch=query_time_epoch)
    result.update({
        "ok": bool(result["emit_ok"] and result["validation"]["exit"] == 0),
        "fixture_id": fixture_id,
        "tau": tau,
        "plan_path": str(plan_path),
        "slice_path": str(slice_path),
        "proposal_path": str(proposal_path),
        "id_mappings": plan["package_metadata"][ID_MAPPINGS_KEY],
        "provenance": plan.get("provenance"),
        "engine": {
            "decision_count": plan["provenance"]["decision_count"],
            "decision_sha256": plan["provenance"]["decision_sha256"],
            "staged_episodes": plan["provenance"]["staged_episodes"],
            "alarm_kinds": plan["provenance"]["alarm_kinds"],
        },
    })
    return result
