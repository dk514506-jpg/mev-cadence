"""C8 seeded-constraint recall probe — emission round-trip (D-11).

Each committed seed in fixtures/probe-seeds.json {text, expected:
CONSTRAINT, span} is injected as a synthetic atom through the T2 emission
path (real build_package + real resolve_span over the committed
probe-seed-texts.txt corpus) and must come out with retention_type ==
CONSTRAINT and C1 byte-equality. Zero-miss = every seed round-trips;
24/25 FAILS. The probe measures the emission path, not an upstream
classifier (none exists this cycle). Seeds are committed and frozen, never
derived from the package under test.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

SEEDS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "tests" / "crystallizer" / "fixtures" / "probe-seeds.json"
)


def load_seeds(path: Path = SEEDS_PATH) -> list:
    """Load the committed seed set."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    seeds = data.get("seeds", data) if isinstance(data, dict) else data
    assert isinstance(seeds, list) and seeds, "probe seed set empty"
    return seeds


def run_probe(manifest: dict, package: dict,
              seeds: list | None = None) -> dict:
    """Run every committed seed through the real emission path."""
    from . import emit as _emit
    from .corpus import resolve_span

    if seeds is None:
        seeds = load_seeds()
    misses = []
    for seed in seeds:
        span = seed.get("span")
        if (not isinstance(seed.get("text"), str) or not seed["text"]
                or not isinstance(span, dict)):
            misses.append(seed.get("id", "<missing-id>"))
            continue
        try:
            atom = _emit.emit_atom(
                {"atom_id": "PROBE",
                 "span_id": span.get("span_id", "__probe__"),
                 "retention_type": "CONSTRAINT",
                 "residual_refs": []},
                span)
        except Exception:
            misses.append(seed.get("id", "<missing-id>"))
            continue
        if (atom.get("retention_type") != seed.get("expected", "CONSTRAINT")
                or atom.get("text") != seed["text"]):
            misses.append(seed.get("id", "<missing-id>"))
    des = (len(seeds) - len(misses)) / len(seeds) if seeds else 0.0
    return {"misses": misses, "seeds_run": len(seeds),
            "descriptive_recall": des}


def probe_check_c8(package, ctx):
    """Registry entry point for the C8 probe (unused directly)."""
    raise NotImplementedError("use run_probe + check_c8")
