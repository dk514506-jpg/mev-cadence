"""T2 — deterministic package emitter (regenerate-only).

Pure function of (plan, manifest). Every residual goes through
policy.write_residual via the §2.3.1 rename adapter; every true-case boundary
goes through policy.write_boundary. No clock reads. Output: canonical JSON
(sorted keys) + EMIT-RECEIPT.json. Emits exactly two files.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

from micro_eviction.vmem_lift import policy

from .corpus import CAMPAIGN_ROOT, CORPUS_DIR, CorpusError, load_manifest, resolve_span
from .validate import ASSERTIONS

OUT_DIR = Path(__file__).resolve().parent / "out"
PLAN_PATH = CORPUS_DIR / "wave1-external-record.plan.json"
FIELD_PATHS = CORPUS_DIR / "v16-field-paths.json"
PKG_NAME = "pkg-wave1-external-record-v1.json"
PKG_PATH = OUT_DIR / PKG_NAME
HONEST_LIMITS_PATH = OUT_DIR / "pkg-wave1-external-record-v1.honest-limits.md"


def _epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def emit_atom(plan_atom: dict, span: dict) -> dict:
    """Emit one atom through the T2 path: resolve span, attach type.

    This is the exact code build_package uses per atom (same function,
    same resolve_span); the probe calls it directly for synthetic seeds.
    """
    text = resolve_span(span)
    locator = {
        "file": span["file"],
        "byte_start": span["byte_start"],
        "byte_end": span["byte_end"],
        "text_sha256": span["text_sha256"],
    }
    return {
        "atom_id": plan_atom["atom_id"],
        "evidence_links": [locator],
        "profile": None,
        "residual_refs": list(plan_atom.get("residual_refs", [])),
        "retention_type": plan_atom["retention_type"],
        "status_tags": {
            "demonstrated": [],
            "extract_evidenced": [span.get("span_id", "")],
            "proposed_gates": [],
        },
        "text": text,
    }


def build_package(plan: dict, manifest: dict, query_time: float) -> dict:
    """Build the package dict from plan + manifest (no I/O, no clock)."""
    if not isinstance(query_time, (int, float)) or isinstance(query_time, bool):
        raise CorpusError(f"query_time must be numeric, got {query_time!r}")
    span_by_id = {s["span_id"]: s for s in manifest["spans"]}
    for atom in plan["atoms"]:
        if atom["span_id"] not in span_by_id:
            raise CorpusError(f"plan atom {atom['atom_id']}: unknown span")
    for res in plan["residuals"]:
        if "decay" not in res or "interval" not in res:
            raise CorpusError(
                f"plan residual {res.get('residual_id')}: missing decay/interval"
            )

    atoms = []
    for a in plan["atoms"]:
        span = span_by_id[a["span_id"]]
        atoms.append(emit_atom(a, span))

    residuals = []
    for r in plan["residuals"]:
        if r.get("decay") is None and r.get("interval") is None:
            raise CorpusError(
                f"residual {r.get('residual_id')}: bare residual refused"
            )
        written = policy.write_residual(
            residual_id=r["residual_id"],
            query_time=query_time,
            value=r["exact_value"],
            decay=r.get("decay"),
            interval=r.get("interval"),
            kind=r["kind"],
            source_atom_id=r["source_atom_id"],
            invalidation_rule=r.get("invalidation_rule"),
        )
        residuals.append(_to_v16_residual(written))

    boundaries = []
    for b in plan["boundaries"]:
        if b.get("relied_upon_for_eviction"):
            written = policy.write_boundary(
                boundary_id=b["id"],
                signal_name=b["signal_name"],
                threshold_value=b["threshold_value"],
                candidate_id=b["candidate_id"],
                confirmation_record=b["confirmation_record"],
                relied_upon_for_eviction=True,
            )
            boundaries.append(_to_v16_boundary(written))
        else:
            for key in ("id", "signal_name", "candidate_id",
                        "relied_upon_for_eviction"):
                if key not in b:
                    raise CorpusError(f"false-case boundary missing {key}")
            boundaries.append(
                {
                    "candidate_id": b["candidate_id"],
                    "confirmation_record": None,
                    "id": b["id"],
                    "relied_upon_for_eviction": False,
                    "signal_name": b["signal_name"],
                    "threshold_value": None,
                }
            )

    by_id = {a["atom_id"]: a for a in atoms}
    evidence_index = {
        a["atom_id"]: list(a["evidence_links"])
        for a in atoms
        if a["evidence_links"]
    }
    residual_index: dict[str, list] = {
        "dates": [], "named_items": [], "quantities": [], "selectors": [],
    }
    for r in residuals:
        residual_index[r["kind"]].append(r["residual_id"])

    checks = []
    for c in plan["checks"]:
        pred = {
            "kind": c["predicate"]["kind"],
            "target": c["predicate"]["target"],
        }
        if c["predicate"].get("formal_id") is not None:
            pred["formal_id"] = c["predicate"]["formal_id"]
        code_ref = c["predicate"].get("code_ref")
        if c["check_id"] in ASSERTIONS:
            fn = ASSERTIONS[c["check_id"]]
            src_file = Path(inspect.getsourcefile(fn))
            _, start = inspect.getsourcelines(fn)
            try:
                rel = src_file.resolve().relative_to(CAMPAIGN_ROOT.resolve())
            except ValueError:
                raise CorpusError(f"assertion {c['check_id']} outside campaign root")
            pred["code_ref"] = f"{rel}:{start}"
        elif code_ref is not None:
            pred["code_ref"] = code_ref
        checks.append(
            {
                "check_id": c["check_id"],
                "class": c["class"],
                "falsifier": c["falsifier"],
                "fires_on": c["fires_on"],
                "predicate": pred,
                "sandbox_required": c["sandbox_required"],
                "status": c["status"],
            }
        )

    source_block = {
        "episodes": list(plan["source"]["episodes"]),
        "external_record": dict(plan["source"]["external_record"]),
        "user_spans": list(plan["source"]["user_spans"]),
    }
    canon = lambda o: json.dumps(o, sort_keys=True, ensure_ascii=False)
    fps = {
        "source": hashlib.sha256(canon(source_block).encode()).hexdigest()[:12],
        "knowledge": hashlib.sha256(
            canon({"atoms": atoms, "residuals": residuals,
                   "residual_index": residual_index}).encode()
        ).hexdigest()[:12],
        "contract": hashlib.sha256(canon(checks).encode()).hexdigest()[:12],
        "evidence": hashlib.sha256(
            canon({"evidence_index": evidence_index,
                   "external_record": source_block["external_record"]}).encode()
        ).hexdigest()[:12],
    }
    intervals = [
        r["lifecycle"]["interval"] for r in residuals
        if r["lifecycle"]["interval"] is not None
    ]
    if intervals:
        default_vi = {
            "t_end": max(i["t_end"] for i in intervals),
            "t_start": min(i["t_start"] for i in intervals),
        }
    else:
        default_vi = {"t_end": query_time, "t_start": query_time}
    freshness = {
        "field_fingerprints": fps,
        "stale_flags": [],
        "validity_intervals": {"default": default_vi},
    }
    for _atom_id, _atom in by_id.items():
        pass
    pkg = {
        "atoms": atoms,
        "boundaries": boundaries,
        "build_provenance": {
            "corpus_renumbering_note": "",
            "crystallizer_version": "crystallizer-v1",
            "mode": "regenerate",
            "source_corpus_sha12": sorted(
                f["sha256"][:12] for f in manifest["files"]
            ),
        },
        "checks": checks,
        "concept_map": list(plan["concept_map"]),
        "dag": {"edges": [], "nodes": []},
        "evidence_index": evidence_index,
        "freshness": freshness,
        "identity": dict(plan["identity"]),
        "residual_index": residual_index,
        "residuals": residuals,
        "source": source_block,
        "user_content": {"live_context_turns": [], "out_of_scope_turns": []},
    }
    _assert_field_paths(pkg)
    errs = policy.validate_package_obligations(
        {"checks": pkg["checks"], "residuals": pkg["residuals"],
         "residual_index": pkg["residual_index"]}
    )
    if errs:
        raise CorpusError(f"emitted package fails obligations: {errs}")
    return pkg


def _to_v16_residual(written: dict) -> dict:
    """Rename-only adapter policy -> V1.6 Residual (§2.3.1)."""
    out = {
        "exact_value": written["value"],
        "invalidation_rule": written.get("invalidation_rule"),
        "kind": written["kind"],
        "lifecycle": {
            "decay": written["lifecycle"]["decay"],
            "interval": written["lifecycle"]["interval"],
            "stale_flag": written["lifecycle"]["stale_flag"],
            "trusted": written["trusted"],
        },
        "residual_id": written["id"],
        "source_atom_id": written["source_atom_id"],
    }
    return out


def _to_v16_boundary(written: dict) -> dict:
    """Rename-only adapter policy -> V1.6 Boundary (§2.3.1)."""
    conf = dict(written["confirmation_record"])
    if "date" in conf:
        conf["date_utc"] = conf.pop("date")
    return {
        "candidate_id": written["candidate_id"],
        "confirmation_record": conf,
        "id": written["id"],
        "relied_upon_for_eviction": written["relied_upon_for_eviction"],
        "signal_name": written["signal_name"],
        "threshold_value": written["threshold_value"],
    }


def _field_paths_of(obj, prefix: str = "") -> set:
    paths = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            paths.add(p)
            paths |= _field_paths_of(v, p)
    elif isinstance(obj, list):
        for item in obj:
            paths |= _field_paths_of(item, prefix)
    return paths


_TYPE_PREFIX = {
    "Package.atoms.": "Atom.",
    "Package.checks.": "Check.",
    "Package.checks.predicate.": "Check.predicate.",
    "Package.boundaries.": "Boundary.",
    "Package.boundaries.confirmation_record.": "Boundary.confirmation_record.",
    "Package.residuals.": "Residual.",
    "Package.residuals.lifecycle.": "Residual.lifecycle.",
    "Package.residuals.lifecycle.decay.": "Residual.lifecycle.decay.",
    "Package.residuals.lifecycle.interval.": "Residual.lifecycle.interval.",
    "Package.concept_map.": "ConceptMap.",
}


def _normalize(p: str) -> str:
    for long, short in sorted(_TYPE_PREFIX.items(), key=lambda kv: -len(kv[0])):
        if p.startswith(long):
            return short + p[len(long):]
    return p


def _assert_field_paths(pkg: dict) -> None:
    want = set(json.loads(FIELD_PATHS.read_text())["fields"])
    raw = _field_paths_of({"Package": pkg})
    got = {_normalize(p) for p in raw}
    if "Package.evidence_index.claim_id" in want:
        # claim ids exist only as evidence_index keys — real-content derivation
        ei = pkg.get("evidence_index")
        if isinstance(ei, dict) and ei:
            got |= {"Package.evidence_index.claim_id"}
    always_absent_ok = {
        "Atom.profile", "Atom.profile.profile_text",
        "Atom.profile.verified_facts", "Atom.profile.override_rule_recorded",
        "Boundary.confirmation_record", "Boundary.threshold_value",
        "Boundary.confirmation_record.reviewer_id",
        "Boundary.confirmation_record.date_utc",
        "Boundary.confirmation_record.candidate_ref",
        "Boundary.id", "Boundary.signal_name", "Boundary.candidate_id",
        "Boundary.relied_upon_for_eviction",
        "ConceptMap.concept", "ConceptMap.corpus_ground",
        "ConceptMap.schema_fields", "ConceptMap.posture",
    }
    missing = set()
    for w in want:
        if w in got or w in always_absent_ok:
            continue
        if w in ("Check.predicate.code_ref", "Check.predicate.formal_id"):
            if ("Check.predicate.code_ref" in got
                    or "Check.predicate.formal_id" in got):
                continue
        missing.add(w)
    extra = {g for g in got if g != "Package" and not any(
        g == w or g.startswith(w + ".") or w.startswith(g + ".")
        for w in want)}
    if missing:
        raise CorpusError(f"emitted package missing paths: {sorted(missing)}")
    if extra:
        raise CorpusError(f"emitted package has undefined paths: {sorted(extra)}")


def canonical_bytes(pkg: dict) -> bytes:
    """Canonical JSON: sorted keys, UTF-8, one trailing newline."""
    return (json.dumps(pkg, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def emit(plan_path: Path = PLAN_PATH, manifest_path=None,
         out_dir: Path = OUT_DIR) -> dict:
    """Emit package + EMIT-RECEIPT.json. Returns the receipt dict."""
    import sys

    if manifest_path is None:
        manifest = load_manifest()
    else:
        manifest = load_manifest(manifest_path)
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)
    query_time = plan["query_time_epoch"]
    pkg = build_package(plan, manifest, query_time)
    pkg_bytes = canonical_bytes(pkg)
    pkg_sha = hashlib.sha256(pkg_bytes).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    pkg_path = out_dir / PKG_NAME
    pkg_path.write_bytes(pkg_bytes)
    manifest_sha = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()
    if not HONEST_LIMITS_PATH.is_file():
        raise CorpusError(
            f"honest-limits sidecar missing: {HONEST_LIMITS_PATH}"
        )
    honest_limits_sha = hashlib.sha256(
        HONEST_LIMITS_PATH.read_bytes()
    ).hexdigest()
    receipt = {
        "command": " ".join(sys.argv) or "emit",
        "created_utc": plan["identity"]["created_utc"],
        "build_run_id": plan["identity"]["build_run_id"],
        "exit": 0,
        "input_manifest_sha256": manifest_sha,
        "output_package_sha256": pkg_sha,
        "honest_limits_sha256": honest_limits_sha,
    }
    (out_dir / "EMIT-RECEIPT.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt
