# STATE OF RECORD — micro-eviction / memory-stack program

**AS OF:** 2026-11-06 13:45 CST
**Owner:** Dallas. **Maintainer:** Silvey (orchestrator).
**Authority:** This file is the SINGLE place to read before making any claim about
the state of this program. It is OVERWRITTEN (not appended) at every phase close.
Every state claim must cite this file and its AS-OF. A claim that does not is void.

**Staleness rule:** if AS-OF is older than the newest commit or newest handoff in
the campaign, this file is stale and must be regenerated at disk before use. It
does not accumulate history; history lives in `RUN-LOG.md`, `GATE-LOG.md`, and the
dated `HANDOFF-*` / `JOURNAL-*` / `PROGRESS-*` files, which are pointers, not state.

---

## 0. One-screen verdict

We have the **testing instrument**, **pole 2 (the vMem policy lift)**, and a
**memory stack on disk**. We do **not** have a working micro-eviction feature:
the engine is built as a library but never called, no eviction has ever been
written to a store, and MemOS 2.0.34 is not running. The next phase is
**build-first, then connect** — not connection alone.

## 1. Decisions RULED — never re-open

| Date | Ruling | Record |
|---|---|---|
| 2026-11-02 | D1 salvage capture/staging, rebuild eviction engine + downstream | `DECISION-PACKAGE-MEV-REMEDIATION-V1.md` §5 |
| 2026-11-02 | D2 80,000-token ceiling, starting assumption | same |
| 2026-11-02 | D3 option (i): Obsidian staging preview WITH trust labels | same |
| 2026-11-02 | D4 agent annotation AFTER mechanical deduction | same |
| 2026-11-02 | D5 A (mechanical cut) core, C (model links) growth path | same |
| 2026-11-02 | D6 reading-campaign report + Round-2 targets | same |
| 2026-11-02 | CWL dependency-driven eviction SUPERSEDES 1:1 turn FIFO | `ADAPTATION-MAP-MEV-CWL-LETTA-V1.md` §"Approved rulings" |
| 2026-11-06 | Runtime authorization-to-fire DISCARDED; standing grant only | `RULING-2026-11-06-STANDING-AUTHORIZATION.md` |

## 2. BUILT and VERIFIED at disk

| Artifact | Measured evidence | Where |
|---|---|---|
| compose-next harness (testing instrument) | 223 tests pass; 149 crystallizer; C1–C6 closed; reuse gate OPEN; evidence ledger VALID 3 entries, head `4108154596…` | branch `0d57637a`; master `242a35b6` |
| pole 2 — vMem component lift | `policy.py` 787 lines + 1,168 lines tests; sealed audit exit 0; contract tests 8/8 pass (measured 2026-11-06) | `micro_eviction/vmem_lift/` |
| crystallization schema V1.6 | frozen instrument of record | `_campaign/CRYSTALLIZATION-PACKAGE-V1.6.md` |
| memory operating contract v0.1 | store of record, writer registry, allowlist, staging discipline | `docs/MEMORY-OPERATING-CONTRACT.md` |
| MemOS 2.0.34 promotion adapter | 28 KB, targets `POST /product/add` on 7411 | `memos_promote_2_0_34.py` |
| MemOS 2.0.34 engine source + unit | `__version__ = "2.0.34"`; `pyproject.toml` 2.0.34; `memos2.service` defined | `~/scratch/mem-migration/memos-2.0.34/` |
| live capture / staging / mirror scaffold | 418 firing rows (live-growing; re-derived 2026-11-06 13:45 CST); D1 ruled to keep | `~/.hermes/plugins/mev-cadence/` |
| design baselines | adaptation map (DRAFT, NOT a charge), two-pole model, C1–C16 builder conditions, four reference repos | `ADAPTATION-MAP-*`, `TWO-POLE-MODEL.md`, `DECISION-PACKAGE-*`, `reference/` |

## 3. NOT BUILT / NOT RUNNING

| Gap | Named in | State |
|---|---|---|
| Eviction engine proper (bookkeeping, pin layer, agent-facing recall, full-fidelity schema, narrative author) | D1; `DECISION-PACKAGE` §2 | No counterpart exists |
| dsh-cwl dependency-driven skeleton port | `ADAPTATION-MAP` §"Component mapping" | No Python port; map is DRAFT, not a charge |
| **The build charge for C1–C16** | `DECISION-PACKAGE` §6: "After Dallas answers D1–D6, the build charge can be written" | NEVER WRITTEN (D1–D6 ruled 11-02) |
| Record adapter (ledger rows → V1.6 packages) | `SPEC-COMPOSE-NEXT-V1.md` S3 row 4 | "Next compose-next increment" |
| R2 decompiler / latent narrative process | `DECISION-PACKAGE` §4 | Design ruled, no artifact |
| C15 renderer bound to dispatcher; C16 delivery gate | `DECISION-PACKAGE` §3 | Open (the "unwired sink" defect) |
| C4/C7 episode inference, C8 pin layer, C9 recall, C10 full-fidelity schema, C13 bookkeeping | `DECISION-PACKAGE` §3 | Not built |
| T0 forensics probes | plan §9 | "STILL UNVERIFIED" (10+ items) |
| T2 grammar (`subject_key`, `claim_kind`, `review_state`) | plan §7; P-1/P-2 ruling | Absent from `instruments/estate_search.py` |
| T2-X index cross-check | plan §9 | NOT-RUN |
| T3 R-5 transport + date Cut-1 | plan §7 | Not built |
| T4 (date Cut-2, MemOS broker, retrieval close, R-3 promotion push, FOLIAGE) | plan §7 | Ruled separately, deferred |
| Runtime | measured 2026-11-06 | `memos2.service` enabled/inactive; ports 7400 + 7411 refuse; `config.yaml` has NO MemOS provider |
| Version gate | measured 2026-11-06 | `assert-engine-version.sh` hangs 180 s (imports whole `memos` package); blocks unit start |
| Standing-authorization change | ruling 2026-11-06 | Recorded, code NOT changed (`authorization.py::live_grant`, `vault_hook.py:174-182`, `records_registry.py:664-669`) |
| Staging sink → Engraphis writer | measured 2026-11-06 13:45 CST | 418 rows: 272 report `staged_writes`, ALL 0; 146 below-cadence noops; 29 fired, 16 held; `TrustFlipUnavailable` |

Re-derived 2026-11-06 13:45 CST from `ledgers/plugin-fires.jsonl`. Both sides of
the count: 272 reporting + 146 not reporting = 418; every reported value is 0.
The ledger is LIVE-GROWING (it moved 410 → 415 → 418 during this program), so
re-derive at read time and never quote these figures as a fixed total.

## 4. Two-pole model (of record)

- **Pole 1** = the live estate on disk (`~/.hermes`, MemOS 2.0.34 lineage, vault,
  Engraphis store). Real and measurable, but not functioning as planned.
- **Pole 2** = the vMem adoption build. Built and tested; NOT installed.
- **The paper chain** (reviews, crystallization package, excavation map, verdicts)
  is NOT a pole; it is the INSTRUMENT that mediates between them.

Source: `_campaign/TWO-POLE-MODEL.md` (2026-11-03). Note: that file says pole 2 is
"not yet built" — STALE; `ADOPT-VMEM-LIFT-V1.md` says increments 1–3 COMPLETE
the same day, and the tests pass today.

## 5. Finish line, gates, and task status relative to done

**FINISH LINE (definition of done).** A cadence boundary fires; micro-eviction
selects and crystalizes records under D1–D6; the full-fidelity packages are
written to Engraphis (`staged_writes > 0`) and promoted to MemOS 2.0.34 on
port 7411; the compose-next harness validates them; agent-facing recall (R-5)
retrieves them; the C16 delivery gate is green. Falsifier: one end-to-end probe
in which a fired eviction produces a non-zero staged write, a lifecycle
promotion, and a successful recall, with ledger and gate receipts.

**Gates.** Seven gates G0–G6; the finish line sits immediately after G6.
`distance = 7 − gate_index`, i.e. the number of gates still between a task and
done. **Count rule:** gates are counted by this index, not by task rows; a gate
is CLOSED only when every task in it is verified at disk.

| gate | name | status | tasks | distance |
|---|---|---|---|---|
| G0 | Rulings of record | CLOSED | 3 | 0 |
| G1 | Build: charge → engine | NOT STARTED | 6 | 6 |
| G2 | Engine runtime (7411) | NOT RUNNING | 3 | 5 |
| G3 | Sink: writes + delivery gate | NOT BUILT | 3 | 4 |
| G4 | Harness on real records | NOT BUILT | 2 | 3 |
| G5 | Semantics: grammar/transport | NOT BUILT | 4 | 2 |
| G6 | End-to-end probe | NOT STARTED | 1 | 1 |

**Both sides of the count:** 1 of 7 gates CLOSED; 6 of 7 NOT closed.
22 tasks total; 3 verified, 19 outstanding (6 OPEN-UNBLOCKED, 12 blocked,
1 DEFERRED-RULED). No percentage of completion is asserted — the measure is
gates closed. Task rows are counted by `id`, gate rows by `gate`; these are
different denominators and must not be mixed.

**Task status vocabulary** (relative to finish line, not absolute):
`CLOSED` verified at disk · `OPEN-UNBLOCKED` buildable now, no dependency ·
`BLOCKED-BY-G<n>` · `DEFERRED-RULED` · `NOT-RUNNING`.

| id | gate | task | status | blocked by | evidence |
|---|---|---|---|---|---|
| G0.1 | G0 | D1–D6 rulings | CLOSED | — | `DECISION-PACKAGE` §5 |
| G0.2 | G0 | Runtime auth discarded; standing grant | CLOSED | — | `RULING-2026-11-06-STANDING-AUTHORIZATION.md` |
| G0.3 | G0 | MemOS 2.0.34 / 7411 pinned, 1.6 contract forbidden | CLOSED | — | `memos_promote_2_0_34.py`, `memos2.service` |
| G1.1 | G1 | **Write the C1–C16 build charge** | OPEN-UNBLOCKED | — | `DECISION-PACKAGE` §6; never written |
| G1.2 | G1 | Eviction engine proper: CWL dependency-driven, full-fidelity packages, no summarization | BLOCKED-BY-G1.1 | G1.1 | D1; no counterpart exists |
| G1.3 | G1 | Port `reference/dsh-cwl/lib.js` skeleton (384 lines) | BLOCKED-BY-G1.1 | G1.1 | `ADAPTATION-MAP` DRAFT, not a charge; 0 hits |
| G1.4 | G1 | R2 decompiler / latent narrative process (D5 A core, C growth) | BLOCKED-BY-G1.1 | G1.1 | design ruled, no artifact |
| G1.5 | G1 | C4/C7 episode inference + D4 annotation after deduction | BLOCKED-BY-G1.1 | G1.1 | `DECISION-PACKAGE` §3 |
| G1.6 | G1 | C8 pin layer, C9 recall, C10 full-fidelity schema, C13 bookkeeping | BLOCKED-BY-G1.1 | G1.1 | `DECISION-PACKAGE` §3 |
| G2.1 | G2 | Fix `assert-engine-version.sh` to read `__version__` directly | OPEN-UNBLOCKED | — | hangs 180 s on package import (exit 124) |
| G2.2 | G2 | qdrant/neo4j prereqs; start `memos2.service` on 7411 | BLOCKED-BY-G2.1 | G2.1 | enabled/inactive; ports dead |
| G2.3 | G2 | `config.yaml` MemOS provider; resolve 7400/7411 refusal; dedupe engraphis-MCP pids | BLOCKED-BY-G2.2 | G2.2 | no provider; 2 MCP pids |
| G3.1 | G3 | Standing-auth replacement in 3 code sites | OPEN-UNBLOCKED | — | `authorization.py::live_grant`, `vault_hook.py:174-182`, `records_registry.py:664-669` |
| G3.2 | G3 | Connect staging sink → Engraphis writer (the unwired sink) | BLOCKED-BY-G3.1 | G3.1 | 272/418 report `staged_writes`, all 0 (146 noops); live-growing |
| G3.3 | G3 | C15 renderer bound to dispatcher + C16 delivery gate | BLOCKED-BY-G3.1 | G3.1 | `DECISION-PACKAGE` §3 |
| G4.1 | G4 | Record adapter: pole-1 ingestion (ledger rows → V1.6 packages) | OPEN-UNBLOCKED | — | `SPEC-COMPOSE-NEXT-V1.md` S3 row 4; reuse gate OPEN |
| G4.2 | G4 | Run harness on real eviction records + 4 hygiene residuals | BLOCKED-BY-G4.1 | G4.1 | delta CONFORMANT / C-W-C |
| G5.1 | G5 | T2 grammar: `subject_key`, `claim_kind`, `review_state` | OPEN-UNBLOCKED | — | absent from `instruments/estate_search.py` |
| G5.2 | G5 | T3 R-5 transport (recall facade as MCP) + date Cut-1 | BLOCKED-BY-G5.1 | G5.1 | plan §7, contract v0.1 deferral |
| G5.3 | G5 | T4 deferred: date Cut-2, MemOS broker, retrieval close, R-3 push, FOLIAGE | DEFERRED-RULED | — | plan §7 |
| G5.4 | G5 | T0 forensics probes (§9 STILL UNVERIFIED, 10+ items) + T2-X NOT-RUN | OPEN-UNBLOCKED | — | `PLAN-OF-RECORD-amended.md` §9 |
| G6.1 | G6 | End-to-end probe: fire → write → promote → recall, with receipts | BLOCKED-BY-G1..G5 | G1–G5 | not attempted |

**Critical path:** G1.1 → G1.2 → G3.2 → G6.1. G1.1 is the single owed
artifact with no dependency; everything on the critical path is behind it.

## 6. Pointers (evidence, not state)

`RUN-LOG.md` (append-only, 146 KB) · `GATE-LOG.md` (compose-next receipts) ·
`DECISION-PACKAGE-MEV-REMEDIATION-V1.md` · `PLAN-OF-RECORD-amended.md` ·
`HANDOFF-2026-11-04-compose-next-close.md` · `PROGRESS-2026-11-03.md` (STALE) ·
`ops/BOARD.md` (STALE, 2026-10-23, different program) ·
`SESSION-STATE-2026-11-03.json` (STALE: increment 2 marked IN_FLIGHT).

## 7. Read-before-claim rule (mechanism fix)

The failure class that produced three wrong answers in one session was
generalizing from a partial read to a whole-program universal. The countermeasure:
before any claim containing "all", "every", "none", or "done", read this file's
§2 AND §3 and report BOTH counts. A claim about program state that does not
reference this file's AS-OF is void.
