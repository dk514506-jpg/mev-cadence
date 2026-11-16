#!/usr/bin/env python3
"""memos_push — R9 MemOS feed push seam: fixture-gated start-if-down + the I3/R14 refusal rule.

Seat: BUILDER Ling (inclusionai/ling-3.1-flash, command-code cmd harness) — sandbox
s4-ling2. Stage: V3 S4 (memos feed + unit artifact). Built: 2026-10-29.
Plan of record: V3-ARCHITECT-PLAN §4 (R9 fixture gate, verified sink/push API
contracts), §7.3 (registry induction-ownership flag + memos_push refusal rule),
§2 R9/R14, §9.5 (A2 unit activation). Charge: MICRO-EVICTION-VAULT-V3.1-2026-10-28
(R9 deliver/activate seam; R14 I3 enforcement).

THE REFUSAL RULE (plan §7.3 + dispatch brief): a push is refused unless the
registry resolves to a deployed referent AND authorization evidence exists.
Concretely, BOTH must hold before any engine contact:
  (1) induction ownership resolves to EXACTLY ONE owner engine — a single named
      engine AND a boolean `effective` flag live-derived from a DEPLOYED config
      referent (algorithm.lightweightMemory.enabled). `unresolved-gap` (G1's
      honest live state on this host: the memos-local-plugin is not deployed),
      zero engines, or two engines -> refuse, loud.
  (2) authorization evidence: (a) the activated unit, proven by
      reports/activation_receipt.json or reports/activation_unit_receipt.json
      (Dallas-approved, A1/A2), or (b) the STANDING grant (RULING-2026-11-06
      / S-3: micro_eviction.authorization.STANDING_GRANT, resolved ONCE at
      import — the per-charge runtime parse is discarded). Absent both ->
      refuse.

THE FIXTURE GATE (plan §4.2): the daemon-aware start-if-down path is FORBIDDEN
against the real lab store (~/scratch/mem-migration/lab/memos-db/memos.db,
:7400) — it raises MemosActivationError, not a warning. It runs ONLY against a
fixture-created throwaway db with MEV_MEMOS_FIXTURE=1 + MEMOS_DB_PATH bound to
that db (env read at call time, never at import). Real pushes require the
activated unit (Dallas-approved A2 step) or explicit grant text.

BUILDER GUARD (plan §4.2.3): any engine spawn path reaching systemctl/loginctl
raises BuilderForbiddenError — absolute, no verb exceptions. The machine-state
canary's three read-only probes (plan §4.2.4) are the sanctioned evidence
channel: they live in machine_state() with a frozen argv allowlist and are the
ONLY systemctl/loginctl contact this module ever makes; they never mutate
state. The engine spawn seam (_spawn) applies builder_guard to everything it
launches (the throwaway memos-server is launched directly, never via
systemctl — the unit file is a DELIVERED artifact activated only at A2).

INERT ON IMPORT: importing this module has no side effects (no disk writes, no
env changes, no network, no estate reads). All writes by callers go through
micro_eviction.io.safe_write containment. Stdlib-only (urllib HTTP) so the seam
runs under any python3; the throwaway server is the lab venv's memos-server
binary spawned as a subprocess, never imported.

Deviations from plan text (nearest-safe versions, recorded for the gate):
  * D1 (§4.2.1): the throwaway server is launched via Popen(start_new_session=True)
    — the same setsid(2) semantics as the plan's `setsid --fork` form — because
    --fork detaches before the pid is knowable and fixture teardown + leak
    detection REQUIRE the pid. Teardown = SIGTERM (uvicorn lifespan stops the
    Node bridge) + wait + SIGKILL fallback.
  * D2 (§4.2.2): the plan names the unit-activation evidence file
    reports/activation_receipt.json in §4.2.2 but reports/activation_unit_receipt.json
    in §9.5. Nearest safe version: EITHER file is accepted as unit-activation
    evidence (each must carry an explicit approval marker: verdict PASS /
    approved true / a Dallas grant field). Neither exists on this host pre-A2,
    so the live state refuses — the safe state.
  * D3 (§4.1): MemOS StoreRequest.type has no vault-type enum (fact,
    preference, context, relationship, entity, custom). Vault records push as
    type="custom" with the vault type carried in metadata + tags — an honest
    mapping that never mislabels a vault rule/pattern as a MemOS fact.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── seat identity (dispatch rule: every new S4 file carries it) ──────────────
SEAT_ID = "BUILDER-Ling-s4-ling2"
SEAT_STAGE = "V3-S4"
SEAT_SANDBOX = "/tmp/seat-logs/s4-ling2/campaigns/bench/micro-eviction"

# campaign-root modules importable regardless of cwd (selftest + gate both run
# from the campaign root; the bootstrap makes in-process import robust).
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from micro_eviction import authorization  # noqa: E402  (standing grant carrier; RULING-2026-11-06 / S-3)

# ── endpoints (R-B1: the 7400 /api/mem daemon stays CLOSED; the default base
# is the MemOS 2.0.34 engine contract on 7411) ────────────────────────────────
DEFAULT_MEMOS_BASE = "http://127.0.0.1:7411"
HEALTH_PATH = "/health"          # server ROOT — distinct from /api/mem/count
STORE_PATH = "/store"            # POST under /api/mem (verified routes.py contract)
COUNT_PATH = "/count"            # GET under /api/mem
HTTP_TIMEOUT = 10.0

# ── the real lab store (plan §4.2.2) — start-if-down FORBIDDEN here ──────────
REAL_LAB_DB = Path.home() / "scratch" / "mem-migration" / "lab" / "memos-db" / "memos.db"
REAL_LAB_DIR = REAL_LAB_DB.parent
LAB_VENV_SERVER = Path.home() / "scratch" / "mem-migration" / "lab" / "venv" / "bin" / "memos-server"

# ── fixture gate (plan §4.2.1) ───────────────────────────────────────────────
FIXTURE_ENV = "MEV_MEMOS_FIXTURE"
DB_ENV = "MEMOS_DB_PATH"
BASE_ENV = "MEMOS_BASE"

# ── activation evidence (plan §4.2.2 case (a); §9.2 step 6; §9.5) ────────────
ACTIVATION_RECEIPT_NAMES = ("reports/activation_receipt.json",
                            "reports/activation_unit_receipt.json")
GRANT_SCOPE = "memos-push"   # program scope of the standing grant (RULING-2026-11-06)

# R14 amendment (Dallas 2026-10-29, Option 3): when the memos-local-plugin is
# provably undeployed (G1) AND a scoped verbatim Dallas grant exists, the
# micro-eviction promotion pipeline is the legal single owner engine. The
# grant-derived ownership record carries derived_from="grant" and the charge
# path as evidence; effective is True BY THE GRANT, not by a disk flag.
GRANT_OWNER_ENGINE = "micro-eviction-promotion"
GRANT_DERIVED_FROM = "grant"

# ── builder guard (plan §4.2.3) ──────────────────────────────────────────────
BLOCKED_PROGRAMS = frozenset({"systemctl", "loginctl"})

# ── machine-state canary (plan §4.2.4) — frozen READ-ONLY evidence channel ───
UNIT_NAME = "memos"
UNIT_FILE = Path.home() / ".config" / "systemd" / "user" / "memos.service"
CANARY_USER = "greenknight"
READONLY_PROBE_ARGV = (
    ("systemctl", "--user", "is-active", UNIT_NAME),
    ("loginctl", "show-user", CANARY_USER, "-p", "Linger"),
)

# ── I3/R14 induction-ownership resolution (plan §7.1/§7.2/§7.3) ──────────────
FLAG_REF = "algorithm.lightweightMemory.enabled"
OWNER_ENGINE = "memos-plugin-heavy"
FLAG_SOURCE_GAP = "undeployed-gap:memos-local-plugin"
EFFECTIVE_GAP = "unresolved-gap"

# §7.3 amendment (Dallas designation, 2026-11-02): the mev-cadence Hermes
# plugin IS the live induction writer for staging — it is designated the
# owner engine. Its deployed manifest (~/.hermes/plugins/mev-cadence/
# plugin.yaml) carries the ownership flag `induction.ownership.enabled`.
# The memos-local-plugin referent (above) is retained unchanged; if BOTH
# referents resolve, the derivation returns a two-engine owner list and the
# sink refuses (I3: exactly one) — never silently preferred.
MEV_OWNER_ENGINE = "mev-cadence"
MEV_FLAG_REF = "induction.ownership.enabled"
_MEV_PLUGIN_MANIFEST = Path.home() / ".hermes" / "plugins" / "mev-cadence" / "plugin.yaml"

# Deployed-referent candidate scan (plan §7.2's confirmed zero-hit list, read
# only, BOUNDED — no recursion into node_modules/dist/.git; re-verified
# zero-hit on this host 2026-10-29 by the S4 builder). The memos-2.0.34 TS
# source checkout (incl. apps/memos-local-plugin/templates/
# config.hermes.yaml) and the lab/memos server source checkout are
# deliberately EXCLUDED: they are source trees, not deployed referents
# (G1 — an undeployed source default is not a live setting).
_DEFAULT_CANDIDATE_FILES = (
    Path.home() / ".hermes" / "config.yaml",
    Path.home() / ".hermes" / "profile.yaml",
    Path.home() / "scratch" / "mem-migration" / "lab" / "env.sh",
    Path.home() / "scratch" / "mem-migration" / "lab" / "hermes-home" / "config.yaml",
    Path.home() / "scratch" / "mem-migration" / "lab" / "introspection.json",
    Path.home() / "scratch" / "mem-migration" / "lab" / "memos-node" / "package.json",
)
_DEFAULT_CANDIDATE_GLOBS = (
    (Path.home() / ".hermes" / "plugins", ("*.yaml", "*.yml", "*.json")),
    (Path.home() / ".hermes" / "estate", ("*.yaml", "*.yml", "*.json")),
    (Path.home() / ".hermes" / "hooks", ("*.yaml", "*.yml", "*.json")),
    (Path.home() / ".hermes" / "scripts", ("*.yaml", "*.yml", "*.json")),
)
_SCAN_PRUNE = {"node_modules", "dist", ".git"}
_SOURCE_CHECKOUT_MARKERS = ("/memos-2.0.34", "/lab/memos")


def _is_source_checkout(p: Path) -> bool:
    r = str(p.resolve())
    return any(m in r for m in _SOURCE_CHECKOUT_MARKERS)


def _deployed_candidates() -> list[Path]:
    out = [p for p in _DEFAULT_CANDIDATE_FILES if not _is_source_checkout(p)]
    for d, pats in _DEFAULT_CANDIDATE_GLOBS:
        if not d.is_dir():
            continue
        for pat in pats:
            for p in d.rglob(pat):
                if any(part in _SCAN_PRUNE for part in p.parts):
                    continue
                if not _is_source_checkout(p):
                    out.append(p)
    return sorted(set(out))


# ── errors ───────────────────────────────────────────────────────────────────

class MemosActivationError(RuntimeError):
    """The daemon-aware start-if-down path is forbidden here (R9 fixture gate
    / real-lab-store guard). NOT a warning — a loud refusal."""


class BuilderForbiddenError(RuntimeError):
    """A builder call path reached systemctl/loginctl (R9 builder guard)."""


class PushRefusedError(RuntimeError):
    """The refusal rule fired (plan §7.3 / dispatch brief).

    reason: "induction-ownership" (registry does not resolve to a deployed
    referent with exactly one owner engine and a boolean effective flag) or
    "authorization" (no activated-unit receipt and no scoped grant text).
    """

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f"memos_push refused ({reason}): {detail}")


class MemosTransportError(RuntimeError):
    """Loud engine/transport failure (health, spawn, HTTP). Never silent."""


# ── builder guard (plan §4.2.3) ──────────────────────────────────────────────

def builder_guard(argv) -> list:
    """Absolute guard: ANY argv whose program is systemctl or loginctl raises.

    No verb exceptions — read-only verbs included (the canary probes do not
    flow through this guard; they are the frozen allowlist in machine_state).
    Every engine spawn seam in this module passes through builder_guard, so no
    call path can reach systemctl/loginctl. Exists for blast-radius proof
    (dead code behind the fixture gate by design)."""
    argv = list(argv)
    if not argv:
        raise BuilderForbiddenError("empty argv refused by builder guard")
    prog = Path(argv[0]).name
    if prog in BLOCKED_PROGRAMS:
        raise BuilderForbiddenError(
            f"builder guard (R9): {prog} is forbidden on every builder call "
            f"path — argv={argv!r}. Builder runs NO systemctl/loginctl; unit "
            f"activation is the orchestrator's Dallas-gated A2 step.")
    return argv


def _spawn(argv, *, log_path=None, env=None):
    """The module's ONLY engine spawn seam. Guarded absolutely."""
    argv = builder_guard(argv)
    if log_path is not None:
        logf = open(log_path, "ab", buffering=0)
        stdout = stderr = logf
    else:
        stdout = stderr = subprocess.DEVNULL
    return subprocess.Popen(argv, env=env, start_new_session=True,
                            stdout=stdout, stderr=stderr)


# ── machine-state canary (plan §4.2.4) — sanctioned read-only evidence ───────

def machine_state(*, unit: str = UNIT_NAME, user: str = CANARY_USER) -> dict:
    """The three machine probes + unit-file existence, READ ONLY.

    This is the frozen evidence channel of §4.2.4 — the only systemctl/
    loginctl contact this module ever makes, restricted to the exact argv
    shapes below (mutation verbs are structurally impossible here). The
    returned dict is timestamp-free so before/after snapshots are
    byte-comparable (the canary fixture asserts byte-identity)."""
    raw = {}
    for argv in READONLY_PROBE_ARGV:
        try:
            cp = subprocess.run(list(argv), capture_output=True, text=True, timeout=10)
            raw[" ".join(argv)] = {"rc": cp.returncode, "out": cp.stdout.strip(),
                                   "err": cp.stderr.strip()}
        except Exception as exc:  # probe failure is recorded, never raised
            raw[" ".join(argv)] = {"rc": None, "out": "", "err": f"probe-error:{type(exc).__name__}:{exc}"}
    is_active = raw[" ".join(READONLY_PROBE_ARGV[0])]["out"] or "unknown"
    linger_line = raw[" ".join(READONLY_PROBE_ARGV[1])]["out"]
    linger = "unknown"
    for part in linger_line.split():
        if part.startswith("Linger="):
            linger = part.split("=", 1)[1]
    return {
        "unit": unit,
        "is_active": is_active,
        "linger": linger,
        "unit_file": str(UNIT_FILE),
        "unit_file_present": UNIT_FILE.exists(),
        "probes": [list(a) for a in READONLY_PROBE_ARGV],
        "raw": raw,
    }


# ── fixture gate (plan §4.2.1/§4.2.2) ────────────────────────────────────────

def is_real_store(db_path) -> bool:
    """True iff db_path is the real lab store (or anything under the lab dir)."""
    try:
        p = Path(db_path).resolve()
    except OSError:
        return False
    try:
        real = REAL_LAB_DB.resolve()
        real_dir = REAL_LAB_DIR.resolve()
    except OSError:
        return False
    return p == real or real_dir in p.parents


def fixture_gate(db_path) -> dict:
    """The hard gate around the daemon-aware start path.

    Satisfied iff MEV_MEMOS_FIXTURE=1 AND MEMOS_DB_PATH is set AND db_path is
    exactly the fixture-declared db AND the db is not the real lab store.
    Returns {satisfied, reason} — reason names the failing clause."""
    flag = os.environ.get(FIXTURE_ENV, "")
    env_db = os.environ.get(DB_ENV, "")
    db = str(Path(db_path).resolve()) if db_path else ""
    if flag != "1":
        return {"satisfied": False,
                "reason": f"{FIXTURE_ENV} != 1 (fixture gate: start-if-down runs "
                          f"only under the throwaway-db protocol)"}
    if not env_db:
        return {"satisfied": False,
                "reason": f"{DB_ENV} unset (fixture gate: the throwaway db must "
                          f"be declared in the environment)"}
    if not db:
        return {"satisfied": False, "reason": "no db path given"}
    if db != str(Path(env_db).resolve()):
        return {"satisfied": False,
                "reason": f"db path {db!r} != declared {DB_ENV} {env_db!r} "
                          f"(fixture gate binds the start to the fixture db)"}
    if is_real_store(db):
        return {"satisfied": False,
                "reason": "db is the real lab store (start-if-down is FORBIDDEN "
                          "there — R9; raises, never warns)"}
    return {"satisfied": True, "reason": "fixture gate satisfied (throwaway db protocol)"}


# ── engine: health probe + fixture-gated start-if-down ───────────────────────

def _base_root(base: str) -> str:
    b = base.rstrip("/")
    return b[: -len("/api/mem")] if b.endswith("/api/mem") else b


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def health_probe(base: str, *, timeout: float = HTTP_TIMEOUT) -> dict:
    """Probe the server ROOT /health (plan §4.1: distinct from /api/mem/count)."""
    url = _base_root(base) + HEALTH_PATH
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return {"ok": r.status == 200, "status": r.status, "url": url,
                    "body": body[:400]}
    except Exception as exc:
        return {"ok": False, "status": None, "url": url,
                "error": f"{type(exc).__name__}: {exc}"}


def start_if_down(db_path=None, *, base: str = None, server_bin=None,
                  health_timeout: float = 120.0, poll: float = 0.5,
                  log_path=None) -> dict:
    """Health-probe `base`; if down, start a THROWAWAY server (fixture-gated).

    Order of refusals (loud, never a warning):
      1. base up            -> return engine info, engine_path="already-up"
      2. db is real store   -> MemosActivationError (FORBIDDEN, R9)
      3. fixture gate unsat -> MemosActivationError (throwaway protocol only)
      4. otherwise          -> spawn the lab venv memos-server on a random
         free port with MEMOS_DB_PATH=<db>; wait for /health; return
         engine_path="throwaway-started".
    The thrown-away engine's `process` handle is returned for fixture teardown
    (deviation D1). Builder never binds :7400 — the started engine always uses
    a random free port; the unit (A2) owns the real port."""
    base = base or os.environ.get(BASE_ENV) or DEFAULT_MEMOS_BASE
    db_path = Path(db_path) if db_path else Path(os.environ.get(DB_ENV) or str(REAL_LAB_DB))
    probe = health_probe(base)
    if probe["ok"]:
        return {"engine_path": "already-up", "base": base, "requested_base": base,
                "db_path": str(db_path), "pid": None, "process": None,
                "health": probe}
    if is_real_store(db_path):
        raise MemosActivationError(
            "start-if-down FORBIDDEN against the real lab store "
            f"({db_path}; plan §4.2.2 / R9). Real activation is the "
            "Dallas-approved A2 step (ops/systemd/memos.service); builder "
            "executes NO systemctl/loginctl.")
    gate = fixture_gate(db_path)
    if not gate["satisfied"]:
        raise MemosActivationError(
            f"start-if-down refused — fixture gate: {gate['reason']}")
    server_bin = Path(server_bin or LAB_VENV_SERVER)
    if not server_bin.exists():
        raise MemosActivationError(f"memos-server binary absent: {server_bin}")
    port = free_port()
    env = dict(os.environ)
    env["MEMOS_DB_PATH"] = str(db_path)
    env["MEMOS_PORT"] = str(port)
    env["MEMOS_LOG_LEVEL"] = "warning"
    if log_path is None:
        log_path = str(db_path) + f"-server-{port}.log"
    proc = _spawn([str(server_bin)], log_path=log_path, env=env)
    engine_base = f"http://127.0.0.1:{port}/api/mem"
    deadline = time.time() + health_timeout
    last = probe
    while time.time() < deadline:
        if proc.poll() is not None:
            raise MemosActivationError(
                f"throwaway memos-server exited rc={proc.returncode} "
                f"(db={db_path}); log: {log_path}")
        last = health_probe(engine_base)
        if last["ok"]:
            return {"engine_path": "throwaway-started", "base": engine_base,
                    "requested_base": base, "db_path": str(db_path),
                    "port": port, "pid": proc.pid, "process": proc,
                    "log_path": log_path, "health": last,
                    "fixture_gate": gate}
        time.sleep(poll)
    proc.terminate()
    raise MemosActivationError(
        f"throwaway memos-server health timeout after {health_timeout}s "
        f"(db={db_path}); last probe: {last}; log: {log_path}")


def stop_engine(engine: dict) -> dict:
    """Fixture/A2 teardown: SIGTERM the throwaway server, wait, SIGKILL fallback."""
    proc = engine.get("process")
    if proc is None:
        return {"stopped": False, "reason": "no process handle (engine was already-up)"}
    if proc.poll() is not None:
        return {"stopped": True, "reason": "already-exited", "rc": proc.returncode}
    proc.terminate()
    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait(timeout=10)
        return {"stopped": True, "reason": "sigkill-after-timeout", "rc": rc}
    return {"stopped": True, "reason": "sigterm", "rc": rc}


# ── I3/R14: induction-ownership resolution (plan §7.1–§7.3) ──────────────────

def _scan_flag_text(text: str):
    """Strict mechanical scan for algorithm.lightweightMemory.enabled.

    Block style (the Hermes template shape, plan §7.1):
        algorithm:
          lightweightMemory:
            enabled: true
    Flow style is accepted as a fallback. Anything ambiguous -> None
    (fail closed: an undeployed or unparsable referent is NOT a boolean)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "algorithm:":
            indent = len(line) - len(line.lstrip())
            for j in range(i + 1, len(lines)):
                lj = lines[j]
                if not lj.strip():
                    continue
                ind_j = len(lj) - len(lj.lstrip())
                if ind_j <= indent:
                    break
                if lj.strip().startswith("lightweightMemory:"):
                    for k in range(j + 1, len(lines)):
                        lk = lines[k]
                        if not lk.strip():
                            continue
                        ind_k = len(lk) - len(lk.lstrip())
                        if ind_k <= ind_j:
                            break
                        if lk.strip().startswith("enabled:"):
                            val = lk.split("enabled:", 1)[1].split("#", 1)[0].strip().lower()
                            if val in ("true", "false"):
                                return val == "true"
                            return None
    m = None
    for line in lines:
        if "algorithm:" in line and "lightweightMemory" in line:
            m = line
            break
    if m:
        import re
        mm = re.search(r"enabled:\s*(true|false)", m)
        if mm:
            return mm.group(1) == "true"
    return None


def _scan_mev_ownership_flag(text: str):
    """Strict mechanical scan for induction.ownership.enabled (§7.3 amendment).

    Block style (the deployed plugin.yaml shape):
        induction:
          ownership:
            enabled: true
    Flow style accepted as fallback. Anything ambiguous -> None (fail closed)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "induction:":
            indent = len(line) - len(line.lstrip())
            for j in range(i + 1, len(lines)):
                lj = lines[j]
                if not lj.strip():
                    continue
                ind_j = len(lj) - len(lj.lstrip())
                if ind_j <= indent:
                    break
                if lj.strip().startswith("ownership:"):
                    for k in range(j + 1, len(lines)):
                        lk = lines[k]
                        if not lk.strip():
                            continue
                        ind_k = len(lk) - len(lk.lstrip())
                        if ind_k <= ind_j:
                            break
                        if lk.strip().startswith("enabled:"):
                            val = lk.split("enabled:", 1)[1].split("#", 1)[0].strip().lower()
                            if val in ("true", "false"):
                                return val == "true"
                            return None
    import re
    for line in lines:
        if "induction:" in line and "ownership" in line and "enabled:" in line:
            mm = re.search(r"enabled:\s*(true|false)", line)
            if mm:
                return mm.group(1) == "true"
    return None


def resolve_induction_ownership(candidate_paths=None, *, derived_from: str = "disk") -> dict:
    """Derive the induction-ownership flag from disk (plan §7.3).

    Scans deployed referent candidates for algorithm.lightweightMemory.enabled.
    First deployed referent carrying a boolean wins -> owner_engine
    "memos-plugin-heavy", effective=true/false. NO deployed referent -> the
    named gap G1: effective="unresolved-gap", owner_engine=None, flag_source
    "undeployed-gap:memos-local-plugin" (an undeployed source default is NOT a
    live setting). Source checkouts (memos-2.0.34 TS tree) are never candidates.
    Returns the §7.3 registry shape; the Stage-6 records_registry MIRRORS this
    derivation (it does not second-source it)."""
    if candidate_paths is None:
        candidates = _deployed_candidates()
    else:
        candidates = [Path(p) for p in candidate_paths]
    # §7.3 amendment: the deployed mev-cadence manifest is consulted on every
    # derivation (live and fixture). Tests pin the manifest via
    # memos_push._MEV_PLUGIN_MANIFEST; the memos referent scan is unchanged.
    mev_hit = None
    if _MEV_PLUGIN_MANIFEST.is_file():
        try:
            mev_val = _scan_mev_ownership_flag(
                _MEV_PLUGIN_MANIFEST.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            mev_val = None
        if isinstance(mev_val, bool):
            mev_hit = {"owner_engine": MEV_OWNER_ENGINE, "flag_ref": MEV_FLAG_REF,
                       "flag_source": str(_MEV_PLUGIN_MANIFEST), "effective": mev_val}
    seen = set()
    for p in candidates:
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        val = _scan_flag_text(text)
        if isinstance(val, bool):
            if mev_hit is not None:
                # BOTH referents live-resolve -> two engines; the sink refuses
                # (I3: exactly one). Never silently preferred.
                both = dict(mev_hit)
                both["owner_engine"] = [mev_hit["owner_engine"], OWNER_ENGINE]
                both["flag_source"] = f"{mev_hit['flag_source']} + {p}"
                both["derived_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                both["derived_from"] = derived_from
                return both
            return {"owner_engine": OWNER_ENGINE, "flag_ref": FLAG_REF,
                    "flag_source": str(p), "effective": val,
                    "derived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "derived_from": derived_from}
    if mev_hit is not None:
        mev_hit = dict(mev_hit)
        mev_hit["derived_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mev_hit["derived_from"] = derived_from
        return mev_hit
    return {"owner_engine": None, "flag_ref": FLAG_REF,
            "flag_source": FLAG_SOURCE_GAP, "effective": EFFECTIVE_GAP,
            "derived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "derived_from": derived_from}


# ── the refusal rule (plan §7.3; single-sourced I3 semantics from S1) ────────

def _resolve_ownership_single(ownership) -> str:
    """Exactly-one-engine rule (same semantics as engraphis_sink.resolve_ownership,
    single-sourced there to avoid a second copy of the I3 invariant)."""
    import engraphis_sink
    return engraphis_sink.resolve_ownership(ownership)


def authorization_evidence(campaign_root, charge_path=None) -> dict:
    """Authorization evidence for the refusal rule's second half.

    (a) activated unit: reports/activation_receipt.json or
        reports/activation_unit_receipt.json under campaign_root carrying an
        explicit approval marker (verdict PASS / approved true / grant field);
    (b) the STANDING grant: micro_eviction.authorization.STANDING_GRANT,
        resolved ONCE at import (RULING-2026-11-06 / S-3) — the per-charge
        runtime parse is discarded. The standing grant is program-scoped
        (GRANT_SCOPE); the first-whitespace-token rule applied to
        charge-carried grant lines and dies with the runtime parse.
    charge_path is provenance only (recorded, never parsed)."""
    root = Path(campaign_root)
    receipts = []
    for name in ACTIVATION_RECEIPT_NAMES:
        p = root / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                receipts.append({"path": str(p), "approved": False,
                                 "reason": "unparseable"})
                continue
            approved = (data.get("verdict") == "PASS" or data.get("approved") is True
                        or bool(data.get("grant")) or bool(data.get("dallas_grant")))
            receipts.append({"path": str(p), "approved": bool(approved),
                             "verdict": data.get("verdict")})
    activated_unit = any(r["approved"] for r in receipts)
    charge = Path(charge_path) if charge_path else (
        Path.home() / "Desktop" / "roof" / "charges"
        / "MICRO-EVICTION-VAULT-V3.1-2026-10-28.md")
    grant = authorization.STANDING_GRANT["grant"]
    scope_ok = bool(grant)
    return {"activated_unit": activated_unit,
            "receipts": receipts,
            "grant": grant,
            "grant_scope": GRANT_SCOPE,
            "grant_scope_ok": scope_ok,
            "charge_path": str(charge)}


def resolve_ownership_or_grant(campaign_root, charge_path=None,
                               candidate_paths=None) -> dict:
    """R14 amendment (2026-10-29, Option 3): disk derivation first; on the G1
    named gap (plugin undeployed), fall through to a grant-derived ownership
    record IF a scoped verbatim Dallas grant exists. Returns the §7.3 registry
    shape; the grant path pins the charge path as flag_source evidence.

    Never fabricates: no deployed referent AND no scoped grant -> the honest
    unresolved-gap record (push will refuse)."""
    disk = resolve_induction_ownership(candidate_paths)
    if isinstance(disk.get("effective"), bool):
        return disk
    ev = authorization_evidence(campaign_root, charge_path)
    if ev.get("grant_scope_ok") and ev.get("grant"):
        return {"owner_engine": GRANT_OWNER_ENGINE,
                "flag_ref": FLAG_REF,
                "flag_source": f"grant:{ev['charge_path']}",
                "effective": True,
                "derived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "derived_from": GRANT_DERIVED_FROM,
                "grant_scope": GRANT_SCOPE}
    return disk


def check_push_authorization(ownership, evidence) -> None:
    """The refusal rule, verbatim (plan §7.3 + dispatch brief) + R14 amendment.

    Raises PushRefusedError unless the registry resolves to a deployed
    referent with EXACTLY ONE owner engine and a boolean effective flag, OR a
    grant-derived ownership record (derived_from="grant") backs the resolved
    single engine — AND authorization evidence exists (activated unit receipt
    or scoped grant). A grant-derived ownership WITHOUT scoped grant evidence
    refuses (coupling: the grant is the only thing that makes it legal)."""
    grant_derived = (ownership.get("derived_from") == GRANT_DERIVED_FROM)
    if grant_derived and not evidence.get("grant_scope_ok"):
        raise PushRefusedError(
            "induction-ownership",
            "grant-derived ownership requires a scoped verbatim grant "
            f"('{GRANT_SCOPE}' first token); grant missing or out of scope")
    try:
        _resolve_ownership_single(ownership)
    except Exception as exc:
        raise PushRefusedError(
            "induction-ownership",
            f"registry does not resolve to exactly one owner engine with a "
            f"boolean effective flag (I3/R14): {exc}") from exc
    if not (evidence.get("activated_unit") or evidence.get("grant_scope_ok")):
        raise PushRefusedError(
            "authorization",
            "no authorization evidence: neither an activated-unit receipt "
            f"({'/'.join(ACTIVATION_RECEIPT_NAMES)}) nor a verbatim Dallas "
            f"grant naming scope '{GRANT_SCOPE}' exists (plan §4.2.2)")


# ── push ─────────────────────────────────────────────────────────────────────

VAULT_METADATA_CARRIERS = ("item_kind", "topic_hash", "to", "type", "record_ids",
                           "sessions", "content_source", "directive_id",
                           "directive_type", "target_topic_hash", "reason",
                           "counts", "evidence")


def _store_payload(item: dict) -> dict:
    """Map one batch item onto the verified MemOS StoreRequest contract
    (plan §4.1 / server routes.py): content + type=custom (honest mapping,
    deviation D3) + provenance in metadata (never a kwarg) + tags."""
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        content = (f"micro-eviction vault item: {item.get('item_kind', 'item')} "
                   f"topic_hash={item.get('topic_hash')}")
    metadata = {k: item[k] for k in VAULT_METADATA_CARRIERS if k in item}
    metadata["vault_item"] = True
    to = item.get("to") or item.get("directive_type") or "item"
    return {
        "content": content,
        "type": "custom",
        "metadata": metadata,
        "tags": ["mev", str(item.get("item_kind", "item")), str(to)],
        "importance": float(item.get("importance", 0.5)),
    }


def _http_json(url: str, *, payload: dict = None, timeout: float = HTTP_TIMEOUT) -> tuple:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8") or "{}")


def api_count(base: str, *, timeout: float = HTTP_TIMEOUT) -> int:
    """GET {base}/count — the fixture/gate verification channel (distinct from
    the root /health probe, plan §4.1)."""
    status, body = _http_json(base.rstrip("/") + COUNT_PATH, timeout=timeout)
    if status != 200:
        raise MemosTransportError(f"count probe status {status}")
    return int((body or {}).get("count", -1))


def api_retrieve(base: str, node_id: str, *, timeout: float = HTTP_TIMEOUT) -> dict | None:
    """POST {base}/retrieve {id} -> the stored node dict (the server returns
    the node DIRECTLY — verified against lab venv routes.py:163-172, NOT
    wrapped like /store's {"node": ...}) or None on 404."""
    status, body = _http_json(base.rstrip("/") + "/retrieve",
                              payload={"id": node_id}, timeout=timeout)
    if status == 404:
        return None
    if status != 200:
        raise MemosTransportError(f"retrieve status {status}")
    return body if isinstance(body, dict) else None


def ensure_engine(*, memos_base=None, db_path=None, **kw) -> dict:
    """Probe the base; start-if-down (fixture-gated) when down."""
    base = memos_base or os.environ.get(BASE_ENV) or DEFAULT_MEMOS_BASE
    probe = health_probe(base)
    if probe["ok"]:
        return {"engine_path": "already-up", "base": base, "requested_base": base,
                "db_path": str(db_path) if db_path else None, "pid": None,
                "process": None, "health": probe}
    return start_if_down(db_path, base=base, **kw)


def push(batch_items, *, campaign_root, ownership=None, charge_path=None,
         memos_base=None, db_path=None, engine=None, timeout: float = HTTP_TIMEOUT) -> dict:
    """Push a batch of items to the MemOS feed — AFTER the refusal rule.

    Order is load-bearing: (1) ownership resolution (deployed referent,
    exactly one engine, boolean effective), (2) authorization evidence
    (activated unit receipt or memos-push-scoped verbatim grant), (3) engine
    (health probe; fixture-gated start-if-down), (4) POST /store per item.
    Any refusal raises PushRefusedError BEFORE any engine contact — on this
    host the live state is G1 unresolved-gap, so every live push refuses."""
    items = list(batch_items)
    if not items:
        raise PushRefusedError("empty-batch", "no items to push")
    if ownership is None:
        ownership = resolve_ownership_or_grant(campaign_root, charge_path)
    evidence = authorization_evidence(campaign_root, charge_path)
    check_push_authorization(ownership, evidence)
    if engine is None:
        engine = ensure_engine(memos_base=memos_base, db_path=db_path)
    results = []
    for item in items:
        payload = _store_payload(item)
        status, body = _http_json(engine["base"] + STORE_PATH, payload=payload,
                                  timeout=timeout)
        node = (body or {}).get("node") or {}
        results.append({"item_id": item.get("record_id") or item.get("directive_id")
                        or item.get("topic_hash"),
                        "http_status": status, "node_id": node.get("id"),
                        "memos_type": node.get("type"),
                        "metadata_record_ids": (node.get("metadata") or {}).get("record_ids")})
    return {"engine_path": engine["engine_path"], "base": engine["base"],
            "requested_base": engine.get("requested_base"),
            "pid": engine.get("pid"), "port": engine.get("port"),
            "process": engine.get("process"),
            "db_path": engine.get("db_path"),
            "pushed": len(results), "results": results,
            "authorization": {"activated_unit": evidence["activated_unit"],
                              "grant_scope_ok": evidence["grant_scope_ok"]},
            "ownership": {"owner_engine": ownership.get("owner_engine"),
                          "effective": ownership.get("effective"),
                          "flag_source": ownership.get("flag_source")}}


__all__ = [
    "SEAT_ID", "DEFAULT_MEMOS_BASE", "REAL_LAB_DB", "LAB_VENV_SERVER",
    "FIXTURE_ENV", "DB_ENV", "BASE_ENV", "GRANT_SCOPE", "FLAG_REF",
    "OWNER_ENGINE", "FLAG_SOURCE_GAP", "EFFECTIVE_GAP",
    "MemosActivationError", "BuilderForbiddenError", "PushRefusedError",
    "MemosTransportError",
    "builder_guard", "machine_state", "is_real_store", "fixture_gate",
    "health_probe", "free_port", "start_if_down", "stop_engine", "ensure_engine",
    "resolve_induction_ownership", "authorization_evidence",
    "check_push_authorization", "push",
]
