# DESIGN — MEV VAULT + ENGRAPHIS/MEMOS INTEGRATION (draft for Dallas confirmation)

Status: DRAFT 1 — ruling recorded in RUN-LOG 2026-10-28 (commit 0c0f4e9).
Nothing implemented. Every named decision below needs Dallas's sign-off before a
build charge is written.

## 1. Architecture of record (Dallas's ruling, formalized)

```
Agent (Hermes, any session)
   │  turn loop; every turn AFTER turn 5, the layer runs in the background
   ▼
[MEV LAYER]  — capture → typed records → lifecycle (promote/calcify/retract)
   │ writes                ▲ reads (assembly for re-entry)
   ▼                       │
ENGRAFIS  — THE primary interface for agent knowledge/memory queries
   │  special workspace: recently-evicted staging
   │  promotion path → permanent workspaces (reigning logic)
   ▼
MEMOS  — downstream consumer: receives data and information VIA Engraphis
   (retrieval + transmission integration; never fed directly by the agent)

OBSIDIAN — representation layer: a markdown mirror of the Engraphis vault
   (read/write representation, not the store of record)
```

Sole access rule: the agent NEVER writes to MemOS directly and NEVER reads the
vault except THROUGH Engraphis. Mini-eviction is the only writer into the staging
workspace. MemOS is the only consumer downstream of Engraphis.

## 2. The vault in Engraphis

- **Staging workspace** (`mev-staging`): every evicted item lands here as an
  ingested memory via `MemoryService.ingest(workspace='mev-staging', ...)`,
  metadata carrying the full provenance chain the program already produces:
  session_id, turn/exchange bounds, record_id, type, topic_hash, capture sha,
  surface mode. `trusted=False` until the capture is gate-verified.
- **Permanent homes**: existing/reigning workspaces per the reigning logic — the
  program's existing promotion machinery decides what leaves staging:
  - calcified rules → the permanent knowledge workspace (`kind='semantic'`,
    per-member quoting preserved in metadata);
  - superseded/falsified → carried WITH their retraction directive (forward-only
    markers intact), never silently deleted;
  - non-promotable evidence → long-tail workspace by domain (or retired after a
    retention window — DECISION D-vault-1 below).
- **Idempotency**: `directive_id`/`record_id` become ingest idempotency keys
  (metadata-matched); re-ingest of the same evicted item is a no-op.
- **The existing sha chain continues to govern**: the anchors, probes, frozen
  instruments stay as-is; the vault is a new SINK for what capture produces, not
  a change to the capture/lifecycle machinery.

## 3. Cadence: every turn after turn 5

- The layer hooks `sync_turn` (the write seam — the destructive variable is the
  number of writes, so this is where the budget lives), from turn 6 onward.
- Per-turn work is SLICED, not batched: the slice is (a) the turns that left the
  active window this boundary, or (b) if no compaction fired, the delta since the
  last processed boundary — whichever is non-empty. This is the paper-extraction
  slicing technique: bounded, anchored, `source_exchange_bounds` carried per
  slice. NO whole-transcript authoring passes (that technique is retired as of
  this ruling).
- One provider at a time: the layer is folded INTO the existing memory provider's
  pre-fetch/fetch path, never a second provider beside it.
- Injection remains governed by the existing 2,000-char budget + adequacy gate +
  grant check (unchanged; still reads the charge/authorization verbatim).

## 4. MemOS integration (retrieval + transmission)

- **Direction of truth**: Engraphis → MemOS, one-way feed. MemOS receives
  promoted/calcified content and retraction corrections; it never originates.
- **Mechanism**: after each promotion batch, the layer pushes the promoted set to
  MemOS via its ingest path (the lab store: `~/scratch/mem-migration/lab/
  memos-db/memos.db`, MemOS daemon on :7400 — daemon is DOWN between runs;
  dream_daily.sh starts it. DECISION D-vault-2 below on daemon lifecycle).
- **Retrieval/transmission**: MemOS's consolidated views (dream-pass products)
  are read back THROUGH Engraphis queries, not directly — Engraphis remains the
  only query surface the agent touches.
- **Two-layer rule honored** (from the provider battery): Engraphis preserves
  provenance; MemOS consolidates. Consolidation aggression never destroys the
  addressability of the Engraphis record.

## 5. Obsidian representation

- A markdown vault mirroring the Engraphis vault workspaces — generated, not
  hand-edited: one note per calcified rule / promoted record / retraction
  directive, front-matter carrying record_id, topic_hash, provenance shas,
  supersession links (both directions, per the atomic-revision rule).
- Direction of sync: Engraphis → Obsidian (render). Human edits in Obsidian are
  NOT the write path (DECISION D-vault-3 below: treat Obsidian edits as feedback
  via a manual procedure, or render-only).
- Staging workspace is NOT mirrored (it is machine-state, not knowledge).

## 7b. ADOPTED (rulings 2026-10-28, RUN-LOG appended)

- **D-vault-1 ADOPTED** — non-promotable evidence → **long-tail INDEX** with
  **sha-spine verification** and a **12-month entropy cadence**: entries decay on
  the schedule form (§8), the sha-spine is always re-verifiable (hash chain
  survives any offload), and at the 12-month boundary each entry's spine is
  verified — then re-promote (if a promotion rule now reaches it) or retire with
  the spine retained. Entropy cadence = degradation order/priority, never
  destruction: the spine is the promise that nothing becomes unverifiable.
- **D-vault-4 ADOPTED** — narrow first grant approved. Build order stands.

## 7c. Authoring policy: mechanical-first (Dallas ruling)

Provider tokens are spent only where semantics genuinely require them. Three
tiers, assigned per operation, MEASURED per tier in the D3 ledger:

- **T1 MECHANICAL (no tokens)** — everything with a deterministic rule: slice
  extraction, exchange bounds, sha/provenance chain, record scaffolding,
  partitioning, lifecycle transitions, budget accounting, all seven forms
  (§8) except diary content, Obsidian rendering, idempotency checks, spine
  verification, entropy scheduling. VERBATIM-carrier records (masked spans,
  corrections, decisions, identifiers) are captured whole by rule — no LLM pass.
- **T2 MECHANICAL-FIRST, LLM-FALLBACK (tokens only on low confidence)** —
  semantic typing of a captured item: a mechanical classifier emits (type,
  confidence); below the confidence floor the slice is queued for a compact LLM
  authoring pass (smallest capable model, per-slice — the paper-extraction
  technique, never batch). Fallback rate is a MEASURED number reported in the
  gate, not assumed.
- **T3 LLM (tokens, unavoidable)** — only: interpretation-bearing content that
  has no deterministic form (diary reflections over consolidated stores, dream
  induction where enabled), judge/critic seats (governance, standing), and
  promotion-rule authoring when a NEW rule type is needed. Never T3 for anything
  reachable by T1/T2.

The existing pipeline is already largely T1; the conversion ruling makes the
tier of every operation an explicit, auditable field (`authoring_tier`) on each
record and in the D3 cost ledger, so the token bill is attributable per tier.

## 8. Forms taxonomy (Dallas ruling; semantics assigned)

Seven form types, each a distinct epistemic role in the tracking estate. One
producer per form; a fact has exactly one owning form.

| Form | Meaning | Function | Owner instance |
|---|---|---|---|
| **INDEX** | Ordered pointers, no payload | Addressability: reach any item without carrying it; the long-tail index (D-vault-1) is the canonical instance | `longtail/index` |
| **MANIFEST** | Closed-list attestation | Completeness: a set claimed whole, with every member and every named omission; reconciles `rendered + not_rendered == items` | delivery manifests; Obsidian render manifest |
| **REGISTRY** | Authoritative entity + current-status record | State of record: what exists, its type, status (accepted/superseded/open/withdrawn), owner | record registry; authorization registry (live_grant) |
| **JOURNAL** | Append-only chronological events | History as it happened: dated appends only, zero deletions (append-only discipline is THE journal rule) | RUN-LOG.md (record of record) |
| **DIARY** | Reflective/interpretive layer over journal events | Meaning and lessons: may be LLM-authored (T3), always tagged distinct from observed fact (the dream-pass distinctness rule) | consolidation reflections (MemOS-side, read via Engraphis) |
| **SCHEDULE** | Forward-looking planned actions with triggers | What WILL happen and when: cadences, cron, the 12-month entropy cadence, boundary checks | entropy schedule; hook cadence schedule |
| **CALENDAR** | Time-anchored dated occurrences | What DID happen at a time and its schedule context: retention window boundaries, gate dates, grant dates, spine-verification dates | vault calendar (12-month entries) |

Cross-form discipline: the SCHEDULE names a trigger, the JOURNAL records its
firing, the CALENDAR anchors the date, the MANIFEST attests completeness of any
batch produced, the REGISTRY carries current status, the INDEX keeps everything
addressable, and the DIARY — alone — interprets. A promotion batch therefore
touches six of seven forms by mechanical code (T1), and only its DIARY note, if
any, costs tokens.

## 9. Open questions (remaining)

None — all four D-vault decisions ADOPTED (see §7b and RUN-LOG 2026-10-28).

- **D-vault-2 ADOPTED** — MemOS daemon runs persistently as a **systemd user
  unit**: `~/.config/systemd/user/memos.service` (Type=simple, ExecStart = the
  lab venv's server entry against `~/scratch/mem-migration/lab/memos-db/
  memos.db`, Restart=on-failure), enabled with `systemctl --user enable --now
  memos`; `loginctl enable-linger greenknight` so it starts at boot without an
  active login session. Health probe: `/health` at the server root (distinct
  from `/api/mem/count`). Engine-ownership split is part of the unit's
  contract: plugin heavy layer induces; dream-pass audits and gates; never both.
- **D-vault-3 ADOPTED (option d)** — hybrid field-level split.

## 7. What changes in the existing program

- capture.py: gains the slice extractor (per-turn slices, anchors preserved) —
  batch mode retained for bulk import only, marked legacy.
- New adapter: `engraphis_sink.py` — ingest wrapper honoring idempotency,
  trust gate, workspace routing (the only new module).
- New adapter: `memos_push.py` — promotion-batch push (daemon-aware).
- New generator: `obsidian_mirror.py` — render + manifest reconciliation
  (rendered + not_rendered == items, every omission named; the register wired to
  the renderer from day one).
- lifecycle: unchanged logic; promotions now ALSO enqueue to memos_push.
- authorization/assembly: unchanged.
- Everything staged as a new charge (V3) gated stage-by-stage as before; sandbox
  fixtures first; the vault adapters get two-sided fixtures before any live write.
