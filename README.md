# mev-cadence — context eviction & crystallization protocol for Hermes Agent

**Version 1.3.0** — scope-complete representation of the program.

mev-cadence is a **context eviction protocol**: when an agent's context
window compacts, nothing of value is lost — the conversation's content is
captured, hashed, staged, and made queryable *before and while* eviction
happens. The host agent (Hermes) evicts; this program crystallizes.

The closed loop this package implements (and is extending):

```
evict (Hermes host) → crystallize (mev-cadence observer)
   → stage (Engraphis mev-staging) → promote (receipt-chained)
   → retrieve (ro lookup instrument) → rehydrate (context-engine bridge,
     authorized, Stage B gated)
```

## Architecture (four layers)

### 1. `mev-cadence-plugin/` — the organic pipeline (installable Hermes plugin)

A fail-closed `post_llm_call` observer. From turn ordinal 6 onward it:
- **slices** the session store (read-only) into conversation deltas,
  including compaction-exit slices;
- **stages** them into the Engraphis workspace `mev-staging` via the
  receipt-chained sink;
- **Task A**: mirrors every 75k seat-state checkpoint as one dated,
  idempotent summary line in the knowledge-vault `log.md`;
- **Task B**: writes full STATE-NN records (model, measured input tokens,
  cumulative burn, cost, slice path + sha) at each 75k measured growth
  crossing.

Laws: honours `MEV_VAULT_HOOK_OFF`; fails closed on vault errors; **never
raises into the host turn pipeline**; never mutates context. Compression
behavior of the host agent is untouched by this layer.

### 2. `promotion/` — staging → permanent homes

`engraphis_sink.py` (the single `svc.ingest` invocation point, R15
idempotency, spark metadata carriers), `memos_push.py` /
`memos_promote_2_0_34.py` (MemOS 2.0.34 promotion), `memos_dispatcher.py`,
`staging_provider.py`. Every write is receipt-chained and auditable.

### 3. `micro_eviction-src/micro_eviction/` — theory-of-operation package

25+ modules: `capture`, `cadence`, `eviction` + `eviction_hook`,
`retrieval` (read-only, staleness-explicit retrieval discipline),
`injection` (sandboxed scope-routed assembly), `crystallizer/` (emit /
validate / probe / sandbox verification scaffolding), `lifecycle`,
`schema`, `records_registry`, forms taxonomy (`directives`, `topic_identity`,
`t2_classifier`), `partition`, `preformat`, `vault_hook`, `canary`,
`authorization`, `letta_bookkeeping` (structural port of letta-code
compaction bookkeeping), `reverie` logic, `vmem_lift`.

### 4. `systemd/` + `tools/` — operations

- `cc_mev_watchdog.timer/.service` + `cc_mev_meta.timer/.service` —
  self-healing cadence timers; `cc_mev_meta_guard.sh` re-enables a
  stopped watcher automatically (kill-and-recover measured).
- `engraphis_lookup.py` — commissioned read-only lookup instrument
  (`census | records | get`); `file:...?mode=ro` by construction.
  Replaces ad-hoc SQL (cost-gate C5).
- `engraphis_backfill.py` — idempotent backfill writer (record_id-keyed,
  fail-closed per record, dry-run + confirm gates).
- `g6_probe.py` — G6 seeded-constraint probe (probe pattern pre-ratified;
  probe records tagged, retained).

### `docs/` — design record

`DESIGN-VAULT-ENGRAPHIS-MEMOS-2026-10-28.md` (architecture of record,
rulings, forms taxonomy), `RUN-LOG.md`, `STATE-OF-RECORD.md`.

## Design rulings the code honours

- **Observer law**: the plugin is timeout-bounded and fail-open on
  timeout; it records errors (fire log + INDEX cursor) and never raises
  into the host.
- **Provenance chain**: every record carries `record_id`, `capture_sha`,
  `bounds`, `topic_hash`, `slice_path` + `slice_sha256_12` — the exact
  chain DESIGN §2 specifies.
- **Two-sided falsifiers**: every acceptance gate names its falsifier;
  verification happens at disk/store readback, never from receipts alone.
- **Stage-boundary amendment (2026-11-16)**: the never-touch-context law
  was a stage boundary for the observer stage. A context eviction
  protocol that *complements* mev-cadence — retrieval tools now,
  injection later behind gates — is authorized. The next stage
  (`engraphis-recall` context-engine bridge) exposes voluntary recall
  tools with byte-identical compression; injection is gated on soak
  evidence, latency measurement, staleness discipline, and a
  fold-vs-lineage ruling.

## Install

1. Copy `mev-cadence-plugin/` to `~/.hermes/plugins/mev-cadence/`.
2. In `~/.hermes/config.yaml`, under `plugins.entries.mev-cadence.settings`,
   set `engraphis_db: <absolute path to your Engraphis memory.db>`.
3. Optionally install the `systemd/` units into `~/.config/systemd/user/`
   (`daemon-reload && enable --now cc_mev_watchdog.timer cc_mev_meta.timer`).
4. Restart the Hermes gateway.

## Verification

```bash
python tools/engraphis_lookup.py --db <db> census
python tools/engraphis_lookup.py --db <db> records --workspace mev-staging
python tools/g6_probe.py --help
```

## Version law

- **1.0.x** — initial observer plugin (staging-hold era).
- **1.1.x** — vault mirror link law + writer-root records.
- **1.2.0** — Engraphis record pipeline complete (store_path plumbing,
  TrustFlip hold eliminated, dual-root link law, lookup instrument,
  probe included).
- **1.3.0** (this release) — **scope-complete representation**: promotion
  machinery, the full micro_eviction package, systemd self-healing ops,
  backfill instrument, and the design record join the package.

## Provenance

Roof estate, `campaigns/bench/micro-eviction`. Run log of record:
`docs/RUN-LOG.md`. `MANIFEST.sha256` pins every packaged file.
