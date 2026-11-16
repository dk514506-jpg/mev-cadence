"""Validators + ASSERTIONS registry.

Report model (§2.4, closed shape): each check reports exactly one of
PASS | FAIL | NOT_EXERCISED | STALE-DEMOTED (C4 only).
Registry contract (D-17): ASSERTIONS maps check_id -> callable; code_ref is
relpath:line within inspect.getsourcelines(callable), path relative to the
campaign root.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from datetime import datetime, timezone

from micro_eviction.vmem_lift import policy

from .corpus import CAMPAIGN_ROOT, CorpusError

PASS = "PASS"
FAIL = "FAIL"
NOT_EXERCISED = "NOT_EXERCISED"
STALE_DEMOTED = "STALE-DEMOTED"

RUN_CHECK = "python -m micro_eviction.crystallizer.cli run-check"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def compute_fingerprints(package: dict) -> dict:
    """Recompute the four C4 fingerprint classes over pinned subtrees (D-16).

    Shared with emit.py so the emitter and the validator hash identically.
    """
    canon = lambda o: json.dumps(o, sort_keys=True, ensure_ascii=False)
    source = package.get("source", {})
    return {
        "source": hashlib.sha256(canon(source).encode()).hexdigest()[:12],
        "knowledge": hashlib.sha256(
            canon(
                {
                    "atoms": package.get("atoms"),
                    "residuals": package.get("residuals"),
                    "residual_index": package.get("residual_index"),
                }
            ).encode()
        ).hexdigest()[:12],
        "contract": hashlib.sha256(
            canon(package.get("checks")).encode()
        ).hexdigest()[:12],
        "evidence": hashlib.sha256(
            canon(
                {
                    "evidence_index": package.get("evidence_index"),
                    "external_record": source.get("external_record"),
                }
            ).encode()
        ).hexdigest()[:12],
    }


def _evidence(check_id: str, package_sha: str, exit_code: int,
              note: str | None = None) -> dict:
    ev = {
        "artifact_sha256": package_sha,
        "command": f"{RUN_CHECK} {check_id}",
        "exit": exit_code,
    }
    if note is not None:
        ev["note"] = note
    return ev


def check_c1(package, ctx):
    """C1 constraint_verbatim: CONSTRAINT/EVIDENCE atoms byte-identical to span."""
    from .corpus import resolve_span

    atoms = package.get("atoms")
    if not isinstance(atoms, list):
        return {"state": FAIL,
                "violations": [{"id": "atoms", "detail": "atoms[] missing"}]}
    carriers = [
        a for a in atoms
        if a.get("retention_type") in ("CONSTRAINT", "EVIDENCE")
    ]
    if not carriers:
        return {"state": NOT_EXERCISED, "reason": "vacuous",
                "violations": []}
    violations = []
    for a in carriers:
        aid = a.get("atom_id", "<missing-id>")
        links = a.get("evidence_links") or []
        if not links:
            violations.append({"id": aid, "detail": "no evidence_links"})
            continue
        try:
            span_text = resolve_span(links[0])
        except CorpusError as e:
            violations.append({"id": aid, "detail": f"dangling span: {e}"})
            continue
        if a.get("text") != span_text:
            violations.append(
                {"id": aid, "detail": "atom text differs from span text"})
    state = FAIL if violations else PASS
    return {"state": state, "violations": violations}


def check_c2(package, ctx):
    """C2 evidence_link_integrity: both pointer directions + no orphans."""
    from .corpus import resolve_span

    atoms = package.get("atoms")
    index = package.get("evidence_index")
    if not isinstance(atoms, list):
        return {"state": FAIL,
                "violations": [{"id": "atoms", "detail": "atoms[] missing"}]}
    if not isinstance(index, dict):
        return {"state": FAIL,
                "violations": [{"id": "evidence_index",
                                "detail": "evidence_index missing"}]}
    if not atoms and not index:
        return {"state": NOT_EXERCISED, "reason": "vacuous",
                "violations": []}
    violations = []
    by_id = {a.get("atom_id"): a for a in atoms if isinstance(a, dict)}
    for a in atoms:
        aid = a.get("atom_id", "<missing-id>")
        if (a.get("evidence_links") or []) and aid not in index:
            violations.append(
                {"id": aid,
                 "detail": "atom has evidence_links but no evidence_index key"})
    for key, locators in index.items():
        if key not in by_id:
            violations.append(
                {"id": str(key),
                 "detail": "evidence_index key absent from atoms[]"})
            continue
        for loc in locators or []:
            try:
                resolve_span(loc)
            except CorpusError as e:
                violations.append(
                    {"id": str(key),
                     "detail": f"span unresolvable at T1: {e}"})
    for a in atoms:
        if a.get("retention_type") in (
            "SEMANTIC", "PROCEDURAL", "NARRATIVE_PROFILE"
        ) and not (a.get("evidence_links") or []):
            violations.append(
                {"id": a.get("atom_id", "<missing-id>"),
                 "detail": "derived atom with no evidence_links (orphan)"})
    for a in atoms:
        for loc in (a.get("evidence_links") or [])[1:]:
            try:
                resolve_span(loc)
            except CorpusError as e:
                violations.append(
                    {"id": a.get("atom_id", "<missing-id>"),
                     "detail": f"supporting span unresolvable at T1: {e}"})
    state = FAIL if violations else PASS
    return {"state": state, "violations": violations}


def check_c3(package, ctx):
    """C3 residual_precision: manifest-driven bijection + profile rule."""
    manifest = ctx.get("manifest") or {}
    expected = {(r.get("kind"), r.get("exact_value")): s.get("span_id")
                for s in manifest.get("spans", [])
                for r in s.get("expected_residuals", [])}
    residuals = package.get("residuals")
    if not isinstance(residuals, list):
        return {"state": FAIL,
                "violations": [{"id": "residuals",
                                "detail": "residuals[] missing"}]}
    violations = []
    actual = {}
    for r in residuals:
        if not isinstance(r, dict):
            violations.append({"id": "<non-object>",
                               "detail": "residual entry not an object"})
            continue
        actual[(r.get("kind"), r.get("exact_value"))] = r.get("residual_id")
    for (kind, value), span_id in expected.items():
        if (kind, value) not in actual:
            violations.append(
                {"id": f"{span_id}:{value}",
                 "detail": f"expected residual missing: {value!r}"})
    for (kind, value), rid in actual.items():
        if (kind, value) not in expected:
            violations.append(
                {"id": str(rid),
                 "detail": f"residual not listed in manifest: {value!r}"})
    by_atom = {a.get("atom_id"): a for a in package.get("atoms", [])
               if isinstance(a, dict)}
    for r in residuals:
        if not isinstance(r, dict):
            continue
        rid = r.get("residual_id", "<missing-id>")
        src = by_atom.get(r.get("source_atom_id"))
        if src is None:
            violations.append(
                {"id": str(rid),
                 "detail": "source_atom_id does not resolve to an atom"})
        elif (r.get("exact_value") or "") not in (src.get("text") or ""):
            violations.append(
                {"id": str(rid),
                 "detail": "exact_value not byte-present in source span"})
    for a in package.get("atoms", []):
        if (not isinstance(a, dict)
                or a.get("retention_type") != "NARRATIVE_PROFILE"):
            continue
        aid = a.get("atom_id", "<missing-id>")
        prof = a.get("profile")
        if not isinstance(prof, dict):
            violations.append(
                {"id": aid, "detail": "NARRATIVE_PROFILE atom missing profile"})
            continue
        if prof.get("override_rule_recorded") is not True:
            violations.append(
                {"id": aid, "detail": "profile.override_rule_recorded not true"})
        vf = prof.get("verified_facts")
        if not vf:
            violations.append(
                {"id": aid, "detail": "profile.verified_facts empty"})
        else:
            rids = {r.get("residual_id") for r in residuals
                    if isinstance(r, dict)}
            for v in vf:
                if v not in rids:
                    violations.append(
                        {"id": aid,
                         "detail": f"verified_fact does not resolve: {v!r}"})
    if not expected and not residuals and not violations:
        return {"state": NOT_EXERCISED, "reason": "vacuous", "violations": []}
    state = FAIL if violations else PASS
    return {"state": state, "violations": violations}


def check_c4(package, ctx):
    """C4 freshness_validity_state: PASS / STALE-DEMOTED / FAIL."""
    query_time = ctx.get("query_time")
    fresh = package.get("freshness")
    if not isinstance(fresh, dict):
        return {"state": FAIL,
                "violations": [{"id": "freshness",
                                "detail": "freshness block missing"}]}
    fps = fresh.get("field_fingerprints")
    if not isinstance(fps, dict) or set(fps) != {
        "source", "knowledge", "contract", "evidence"
    }:
        return {"state": FAIL,
                "violations": [{"id": "freshness",
                                "detail": "field_fingerprints missing or "
                                          "incomplete (unconditional FAIL)"}]}
    recomputed = compute_fingerprints(package)
    mismatches = [k for k in recomputed if recomputed[k] != fps.get(k)]
    vi = (fresh.get("validity_intervals") or {}).get("default") or {}
    covers = (
        isinstance(vi.get("t_start"), (int, float))
        and isinstance(vi.get("t_end"), (int, float))
        and vi["t_start"] <= query_time <= vi["t_end"]
    )
    stale_flags = fresh.get("stale_flags") or []
    clean = not mismatches and covers
    if clean and not stale_flags:
        return {"state": PASS, "violations": []}
    violations = []
    for k in mismatches:
        violations.append(
            {"id": f"fingerprint:{k}",
             "detail": f"recomputed {recomputed[k]} != recorded {fps.get(k)}"})
    if not covers:
        violations.append(
            {"id": "validity_intervals",
             "detail": "query_time outside validity interval"})
    residuals = package.get("residuals") or []
    stale_trusted = []
    for r in residuals:
        if not isinstance(r, dict):
            continue
        lc = r.get("lifecycle") or {}
        try:
            stale = policy.compute_stale_flag(
                lc.get("decay"), lc.get("interval"), query_time)
        except (KeyError, TypeError):
            continue
        if stale and lc.get("trusted") is not False:
            stale_trusted.append(str(r.get("residual_id")))
    if stale_trusted:
        for rid in stale_trusted:
            violations.append(
                {"id": rid,
                 "detail": "stale residual marked trusted"})
        return {"state": FAIL, "violations": violations}
    if clean and stale_flags:
        return {"state": STALE_DEMOTED, "violations": []}
    return {"state": FAIL, "violations": violations}


def check_c5(package, ctx):
    """C5 external_record_preservation: byte-identical external record."""
    source = package.get("source") or {}
    ext = source.get("external_record") or {}
    if not ext:
        return {"state": NOT_EXERCISED, "reason": "vacuous",
                "violations": []}
    atoms = package.get("atoms") or []
    texts = [a.get("text", "") for a in atoms if isinstance(a, dict)]
    violations = []
    for key, entry in ext.items():
        if not isinstance(entry, str):
            violations.append(
                {"id": str(key), "detail": "external_record entry not a string"})
            continue
        if entry in texts:
            continue
        best = max(texts, key=lambda t: difflib.SequenceMatcher(
            None, entry, t).ratio(), default="")
        hunk = "\n".join(difflib.unified_diff(
            entry.splitlines(), best.splitlines(), lineterm=""))
        violations.append(
            {"id": str(key),
             "detail": f"external entry dropped or rewritten:\n{hunk}"})
    state = FAIL if violations else PASS
    return {"state": state, "violations": violations}


def check_c6(package, ctx):
    """C6 dependency_ordered_eligibility: three-condition conjunction."""
    dag = package.get("dag")
    if not isinstance(dag, dict):
        return {"state": FAIL,
                "violations": [{"id": "dag", "detail": "dag missing"}]}
    nodes = dag.get("nodes")
    edges = dag.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return {"state": FAIL,
                "violations": [{"id": "dag", "detail": "dag nodes/edges missing"}]}
    if not nodes and not edges:
        return {"state": NOT_EXERCISED, "reason": "vacuous",
                "violations": []}
    violations = []
    adj: dict = {}
    try:
        for n in nodes:
            adj.setdefault(n, [])
    except TypeError:
        return {"state": FAIL,
                "violations": [{"id": "dag",
                                "detail": "dag node unhashable"}]}
    for e in edges:
        if (not isinstance(e, (list, tuple)) or len(e) != 2
                or not isinstance(e[0], str) or not isinstance(e[1], str)):
            violations.append(
                {"id": "dag", "detail": f"malformed edge: {e!r}"})
            continue
        u, v = e
        if u == v:
            violations.append(
                {"id": str(u), "detail": "cycle: self-edge"})
        adj.setdefault(u, []).append(v)
        adj.setdefault(v, [])
    color: dict = {}

    def visit(n, stack):
        color[n] = 1
        for m in adj.get(n, []):
            if color.get(m) == 1:
                violations.append(
                    {"id": str(n),
                     "detail": f"cycle: edge {n}->{m} closes a loop"})
            elif color.get(m) is None:
                visit(m, stack + [m])
        color[n] = 2

    for n in nodes:
        if color.get(n) is None:
            visit(n, [n])
    episodes = {
        e.get("id"): e
        for e in (package.get("source") or {}).get("episodes", [])
        if isinstance(e, dict)
    }
    for n in nodes:
        ep = episodes.get(n)
        if ep is None:
            continue
        if ep.get("prologue") or ep.get("active") or not ep.get("closed"):
            violations.append(
                {"id": str(n),
                 "detail": "prologue/active/non-closed episode in DAG"})
            continue
        if not ep.get("derived_externalized"):
            violations.append(
                {"id": str(n),
                 "detail": "own-state hole: derived_externalized false"})
        for dep in adj.get(n, []):
            de = episodes.get(dep, {})
            if not de.get("evicted") or not de.get("derived_externalized"):
                violations.append(
                    {"id": str(n),
                     "detail": f"depended-upon-first: dependent {dep} "
                              "un-evicted or unexternalized"})
    state = FAIL if violations else PASS
    return {"state": state, "violations": violations}


def check_c7(package, ctx):
    """C7 user_content_inviolability: 100% in-scope byte identity + reasons."""
    source = package.get("source") or {}
    spans = source.get("user_spans")
    if spans is None:
        return {"state": FAIL,
                "violations": [{"id": "source",
                                "detail": "source.user_spans missing"}]}
    if not isinstance(spans, list) or not spans:
        return {"state": NOT_EXERCISED, "reason": "vacuous",
                "violations": []}
    content = package.get("user_content") or {}
    live = content.get("live_context_turns")
    if not isinstance(live, list):
        return {"state": FAIL,
                "violations": [{"id": "user_content",
                                "detail": "live_context_turns missing"}]}
    out_scope = content.get("out_of_scope_turns")
    if not isinstance(out_scope, list):
        return {"state": FAIL,
                "violations": [{"id": "user_content",
                                "detail": "out_of_scope_turns missing"}]}
    in_scope = [s for s in spans
                if isinstance(s, dict) and s.get("in_scope")]
    violations = []
    for s in in_scope:
        sid = s.get("id", "<missing-id>")
        text = s.get("text", "")
        if text not in live:
            violations.append(
                {"id": str(sid),
                 "detail": "in-scope user span missing from "
                           "live_context_turns"})
    for o in out_scope:
        if not isinstance(o, dict) or not o.get("reason"):
            violations.append(
                {"id": str((o or {}).get("turn", o)),
                 "detail": "out_of_scope_turns entry without a reason"})
    state = FAIL if violations else PASS
    return {"state": state, "violations": violations}


def check_c8(package, ctx):
    """C8 type_label_coverage + seeded round-trip probe (zero-miss rule)."""
    from .probe import run_probe

    allowed = {"CONSTRAINT", "EVIDENCE", "EPISODE", "SEMANTIC", "PROCEDURAL",
               "NARRATIVE_PROFILE"}
    atoms = package.get("atoms")
    if not isinstance(atoms, list):
        return {"state": FAIL,
                "violations": [{"id": "atoms", "detail": "atoms[] missing"}]}
    violations = []
    for a in atoms:
        aid = a.get("atom_id", "<missing-id>")
        rt = a.get("retention_type")
        if not isinstance(rt, str) or rt not in allowed:
            violations.append(
                {"id": str(aid),
                 "detail": f"untagged or out-of-enum atom: {rt!r}"})
        if rt == "NARRATIVE_PROFILE" and not isinstance(a.get("profile"), dict):
            violations.append(
                {"id": str(aid),
                 "detail": "NARRATIVE_PROFILE atom missing profile"})
    probe = run_probe(ctx.get("manifest"), package)
    misses = probe.get("misses", [])
    for m in misses:
        violations.append({"id": m, "detail": "seeded-constraint miss"})
    if not atoms and not violations:
        return {"state": NOT_EXERCISED, "reason": "vacuous", "violations": []}
    state = FAIL if violations else PASS
    return {"state": state, "violations": violations,
            "probe": {"recall": probe.get("descriptive_recall"),
                      "seeds": probe.get("seeds_run", 0)}}


SANDBOX_LIMIT_NOTE = (
    "process-level sandbox: cwd containment, environment scrubbing, "
    "timeout; not kernel isolation"
)

SCHEMA_ABSENT_REASON = "schema_absent"


def check_c9a(package, ctx):
    """C9a sampled evidence: run-based verdict over the executable set.

    E9a: Supported(u) iff Executable(u) is non-empty and for every e in
    Executable(u), Run(e) passes. Executable(u) = the package's 9a/9c
    checks (the runnable ones; 9b semantic review is never executed).
    For each e except C9a itself (the runner never runs itself), the
    runner runs e against clean state (must exit 0) and against seeded
    state (must exit non-zero naming the violation id on stdout), inputs
    pinned by D-18. A check's pass/fail comes from those runs, never from
    its declaration or Check.status. Run reports record command, exit,
    stdout/stderr sha256, cwd, timeout, and the fixed sandbox-limit note.
    """
    from . import sandbox

    checks = package.get("checks")
    if not isinstance(checks, list) or not checks:
        return {"state": FAIL,
                "violations": [{"id": "checks",
                                "detail": "C9a requires a non-empty check set"}]}
    executable = [
        c for c in checks
        if isinstance(c, dict) and c.get("class") in ("9a", "9c")
    ]
    if not executable:
        return {"state": FAIL,
                "violations": [{"id": "executable_set",
                                "detail": "C9a: Executable(u) is empty "
                                          "(E9a nonempty support)"}]}
    violations = []
    runs = []
    for c in executable:
        cid = c.get("check_id", "<missing-id>")
        if cid == "C9a":
            continue  # the runner never runs itself
        for mode in sandbox.MODES:
            name = sandbox.FIXTURES.get((cid, mode))
            if name is None:
                violations.append(
                    {"id": str(cid),
                     "detail": f"fixture_absent: no {mode} fixture "
                               f"registered for {cid}"})
                continue
            try:
                report = sandbox.run(
                    cid, sandbox.FIXTURES_DIR / name, mode)
            except sandbox.SandboxRefused as e:
                violations.append(
                    {"id": str(cid),
                     "detail": f"sandbox refused the {mode} run: {e}"})
                continue
            runs.append(sandbox.report_record(report))
            for msg in sandbox.run_contract_violations(report):
                violations.append({"id": str(cid), "detail": msg})
    state = FAIL if violations else PASS
    return {"state": state, "violations": violations, "runs": runs}


check_c9a.executes_code = True


def check_c9c(package, ctx):
    """C9c enforceable invariant (unconditional FAIL on empty check set).

    PASS requires: checks[] non-empty and explicitly enumerated; every
    Check.class <-> predicate.kind pair bound (delegates
    policy.validate_check_class_binding); every 9b check satisfies
    required_one_of [code_ref, formal_id] and every other kind carries
    code_ref (delegates policy.validate_required_one_of); every formal_id
    resolves (delegates policy.validate_formal_id_resolution); every
    code_ref path exists and resolves to the registered assertion for
    that check id; sandbox_required true exactly for checks that execute
    code; a SANDBOX_RULE check carries target "*" and sandbox_required
    false.
    """
    import inspect
    from pathlib import Path

    campaign_root = CAMPAIGN_ROOT.resolve()

    checks = package.get("checks")
    if not isinstance(checks, list) or not checks:
        return {"state": FAIL,
                "violations": [{"id": "checks",
                                "detail": "C9c: empty check set "
                                "(unconditional FAIL)"}]}
    violations = []
    for err in policy.validate_check_class_binding(checks):
        violations.append({"id": err.split(":")[0],
                           "detail": f"C9c unbound pair: {err}"})
    for err in policy.validate_required_one_of(checks):
        violations.append({"id": err.split(":")[0],
                           "detail": f"C9c locator rule: {err}"})
    for err in policy.validate_formal_id_resolution(checks):
        violations.append({"id": err.split(":")[0],
                           "detail": f"C9c dangling id: {err}"})
    for c in checks:
        cid = c.get("check_id", "<missing-id>")
        pred = c.get("predicate") if isinstance(
            c.get("predicate"), dict) else {}
        kind = pred.get("kind")
        if c.get("class") == "9b" and kind == "SEMANTIC_REVIEW":
            continue
        ref = pred.get("code_ref")
        if not ref or ":" not in ref:
            violations.append(
                {"id": str(cid),
                 "detail": "C9c: code_ref missing or malformed"})
            continue
        rel, _, lineno = ref.partition(":")
        try:
            line_no = int(lineno)
        except ValueError:
            violations.append(
                {"id": str(cid),
                 "detail": f"C9c: code_ref line not an integer: {ref!r}"})
            continue
        try:
            full = (campaign_root / rel).resolve()
            full.relative_to(campaign_root)
        except (OSError, ValueError):
            violations.append(
                {"id": str(cid),
                 "detail": f"C9c: code_ref escapes campaign root: {ref!r}"})
            continue
        if not full.is_file():
            violations.append(
                {"id": str(cid),
                 "detail": f"C9c: code_ref path missing: {ref!r}"})
            continue
        fn = ASSERTIONS.get(cid)
        if fn is None:
            violations.append(
                {"id": str(cid),
                 "detail": "C9c: check id not in ASSERTIONS registry"})
            continue
        try:
            src_file = Path(inspect.getsourcefile(fn))
            src_lines, start = inspect.getsourcelines(fn)
        except (OSError, TypeError) as e:
            violations.append(
                {"id": str(cid),
                 "detail": f"C9c: registry source unreadable: {e}"})
            continue
        try:
            got_rel = src_file.resolve().relative_to(
                campaign_root)
        except ValueError:
            violations.append(
                {"id": str(cid),
                 "detail": f"C9c: registry source outside campaign root: "
                           f"{cid}"})
            continue
        end = start + len(src_lines) - 1
        if (str(got_rel) != rel or not (start <= line_no <= end)):
            violations.append(
                {"id": str(cid),
                 "detail": f"C9c: code_ref {ref!r} does not resolve to "
                           f"the registered assertion "
                           f"({got_rel}:{start}-{end})"})
    for c in checks:
        cid = c.get("check_id", "<missing-id>")
        pred = c.get("predicate") if isinstance(
            c.get("predicate"), dict) else {}
        kind = pred.get("kind")
        sbx = c.get("sandbox_required")
        if sbx not in (True, False):
            violations.append(
                {"id": str(cid),
                 "detail": "C9c: sandbox_required must be a boolean"})
            continue
        if kind == "SANDBOX_RULE":
            if pred.get("target") != "*" or sbx is not False:
                violations.append(
                    {"id": str(cid),
                     "detail": "C9c: SANDBOX_RULE check must carry "
                               'target "*" and sandbox_required false'})
            continue
        fn = ASSERTIONS.get(cid)
        if fn is None:
            continue  # already failed the registry rule above
        # sandbox_required is true exactly for checks that execute code.
        executes = bool(getattr(fn, "executes_code", False))
        if executes and sbx is not True:
            violations.append(
                {"id": str(cid),
                 "detail": "C9c: check executes code but declares "
                           "sandbox_required false"})
        elif not executes and sbx is True:
            violations.append(
                {"id": str(cid),
                 "detail": "C9c: sandbox_required true but the check "
                           "executes no code"})
    state = FAIL if violations else PASS
    return {"state": state, "violations": violations}


def check_c9b(package, ctx):
    """C9b review-recorded presence gate (schema_absent, never PASS).

    V1.6 has no review_recorded field, so this gate reports
    NOT_EXERCISED / schema_absent. It must never be PASS.
    """
    return {"state": NOT_EXERCISED, "reason": SCHEMA_ABSENT_REASON,
            "violations": []}


def check_e_a8(package, ctx):
    """E-A8 signpost_provenance (delegates to policy).

    Vacuous when boundaries[] is empty (T7 asserts boundaries == []).
    Emitted-package claim state for boundary-based eviction is
    boundaries[] non-empty; the validator-input flag
    package["boundary_based_eviction"] (fixtures/harness only, never
    emitted) restores the zero-boundaries FAIL clause.
    """
    boundaries = package.get("boundaries")
    if not isinstance(boundaries, list):
        return {"state": FAIL,
                "violations": [{"id": "boundaries",
                                "detail": "package missing boundaries[]"}]}
    if not boundaries:
        return {"state": NOT_EXERCISED, "reason": "vacuous",
                "violations": []}
    adapt = _adapt_package_for_policy(package)
    errs = policy.validate_e_a8(adapt)
    if errs:
        return {"state": FAIL,
                "violations": [{"id": _vio_id(e),
                                "detail": f"E-A8: {e}"} for e in errs]}
    return {"state": PASS, "violations": []}


def check_e_a9(package, ctx):
    """E-A9 residual_lifecycle_closure (delegates to policy).

    Crosses the §2.3.1 rename adapter so V1.6 Residual keys
    (residual_id/exact_value/lifecycle.trusted) validate against the
    policy-keyed E-A9 predicate; residual_index membership still
    delegates to policy.validate_residual_index via obligation 2.
    """
    adapt = _adapt_package_for_policy(package)
    query_time = ctx.get("query_time")
    errs = policy.validate_e_a9(adapt, query_time)
    for err in policy.validate_residual_index(
            package.get("residuals") or (),
            package.get("residual_index") or {}):
        errs.append(err)
    if errs:
        return {"state": FAIL,
                "violations": [{"id": _vio_id(e),
                                "detail": f"E-A9: {e}"} for e in errs]}
    if not package.get("residuals") and not any(
            (package.get("residual_index") or {}).values()):
        return {"state": NOT_EXERCISED, "reason": "vacuous", "violations": []}
    return {"state": PASS, "violations": []}


def _vio_id(err: str) -> str:
    head = err.split(":", 1)[0].strip()
    for token in ("residual ", "boundary "):
        if head.startswith(token):
            return head[len(token):].strip() or head
    return head


def _adapt_residual_for_policy(res: dict) -> dict:
    """Rename-only V1.6 Residual -> policy keys (§2.3.1 inverse adapter)."""
    lc = res.get("lifecycle") or {}
    return {
        "id": res.get("residual_id"),
        "value": res.get("exact_value"),
        "kind": res.get("kind"),
        "source_atom_id": res.get("source_atom_id"),
        "invalidation_rule": res.get("invalidation_rule"),
        "trusted": lc.get("trusted"),
        "lifecycle": dict(lc),
    }


def _adapt_boundary_for_policy(bnd: dict) -> dict:
    """Rename-only V1.6 Boundary -> policy keys (§2.3.1 inverse adapter)."""
    conf = dict(bnd.get("confirmation_record") or {})
    if "date_utc" in conf and "date" not in conf:
        conf["date"] = conf.pop("date_utc")
    out = dict(bnd)
    out["confirmation_record"] = conf
    return out


def _adapt_package_for_policy(package: dict) -> dict:
    """Copy a package with V1.6 keys renamed for policy-keyed validators."""
    adapt = dict(package)
    residuals = package.get("residuals")
    if isinstance(residuals, list):
        adapt["residuals"] = [
            _adapt_residual_for_policy(r) if isinstance(r, dict) else r
            for r in residuals
        ]
    boundaries = package.get("boundaries")
    if isinstance(boundaries, list):
        adapt["boundaries"] = [
            _adapt_boundary_for_policy(b) if isinstance(b, dict) else b
            for b in boundaries
        ]
    return adapt


ASSERTIONS = {
    "C1": check_c1,
    "C2": check_c2,
    "C3": check_c3,
    "C4": check_c4,
    "C5": check_c5,
    "C6": check_c6,
    "C7": check_c7,
    "C8": check_c8,
    "C9a": check_c9a,
    "C9b": check_c9b,
    "C9c": check_c9c,
    "E-A8": check_e_a8,
    "E-A9": check_e_a9,
}

CHECK_ORDER = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8",
               "C9a", "C9b", "C9c", "E-A8", "E-A9"]

PERMITTED_NOT_EXERCISED = frozenset(
    {"vacuous", "schema_absent", "fixture_absent", "skipped_by_policy"}
)

C9_NOTE_CHECKS = frozenset({"C9a", "C9c"})


def validate_package(package: dict, manifest: dict,
                     query_time: float | None = None) -> dict:
    """Run registered checks; return the §2.4 four-state report."""
    if query_time is None:
        raise CorpusError("query_time is required (no wall clock reads)")
    if isinstance(query_time, bool) or not isinstance(
            query_time, (int, float)):
        raise CorpusError(f"query_time must be numeric, got {query_time!r}")
    query_time = float(query_time)
    import hashlib as _hl

    package_sha = _hl.sha256(
        json.dumps(package, sort_keys=True, ensure_ascii=False).encode(
            "utf-8")).hexdigest()
    ctx = {"manifest": manifest, "query_time": query_time,
           "package_sha256": package_sha}
    checks = []
    for cid in CHECK_ORDER:
        res = ASSERTIONS[cid](package, ctx)
        state = res["state"]
        note = SANDBOX_LIMIT_NOTE if cid in C9_NOTE_CHECKS else None
        entry: dict = {
            "check_id": cid,
            "evidence": _evidence(
                cid, package_sha, 0 if state != FAIL else 1, note),
            "state": state,
            "violations": list(res.get("violations", [])),
        }
        if res.get("runs"):
            entry["evidence"]["runs"] = list(res["runs"])
        if state == NOT_EXERCISED:
            reason = res.get("reason", "vacuous")
            if reason not in PERMITTED_NOT_EXERCISED:
                entry["state"] = FAIL
                entry["evidence"] = _evidence(cid, package_sha, 1, note)
                entry["violations"] = [
                    {"id": cid,
                     "detail": f"NOT_EXERCISED reason not permitted: "
                               f"{reason!r}"}]
                state = FAIL
            else:
                entry["reason"] = reason
        if state == STALE_DEMOTED and cid != "C4":
            entry["state"] = FAIL
            entry["evidence"] = _evidence(cid, package_sha, 1, note)
            entry["violations"] = [
                {"id": cid,
                 "detail": "STALE-DEMOTED legal for C4 only"}]
            state = FAIL
        if state == FAIL:
            assert entry["violations"], f"{cid}: FAIL requires violations"
        if state == PASS:
            assert entry["evidence"].get("command"), \
                f"{cid}: PASS requires executed assertion"
        checks.append(entry)
    summary = {
        "fail": sum(1 for c in checks if c["state"] == FAIL),
        "not_exercised": sum(
            1 for c in checks if c["state"] == NOT_EXERCISED),
        "pass": sum(1 for c in checks if c["state"] == PASS),
        "stale_demoted": sum(
            1 for c in checks if c["state"] == STALE_DEMOTED),
    }
    assert (summary["pass"] + summary["fail"] + summary["not_exercised"]
            + summary["stale_demoted"]) == len(checks)
    return {
        "checks": checks,
        "package_sha256": package_sha,
        "query_time": _iso(query_time),
        "query_time_epoch": query_time,
        "summary": summary,
    }
