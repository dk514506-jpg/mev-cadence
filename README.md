# mev-cadence 1.2.0 — GitHub package

Session/cadence crystallization for Hermes Agent: a fail-closed
`post_llm_call` observer plugin + the commissioned lookup instrument +
the G6 seeded-constraint probe.

## Version law

- **1.0.x** — initial observer plugin (staging hold era).
- **1.1.x** — vault mirror link law + writer-root records.
- **1.2.0** (this release) — Engraphis record pipeline complete:
  store_path plumbing fixed (`engraphis_db` config key), TrustFlip
  hold eliminated, watchdog + meta self-healing timers, dual-root
  link law (sweep exit 0, 166 links), lookup instrument
  (`engraphis_lookup.py`, ro-enforced), and the **G6 probe included**
  per Dallas ruling 2026-11-16 (probe records pre-ratified, retained,
  tagged `g6-probe`).

## Contents

- `mev-cadence-plugin/` — installable Hermes plugin (`plugin.yaml` +
  `__init__.py`). Observer only: never raises into the host turn
  pipeline; reads session store read-only; slices + stages conversation
  deltas to the Engraphis store; writes dated checkpoint records and
  vault `log.md` mirror lines.
- `tools/engraphis_lookup.py` — commissioned read-only lookup
  instrument (`census | records | get`); `file:...?mode=ro` by
  construction; replaces ad-hoc SQL per cost-gate C5.
- `tools/g6_probe.py` — G6 seeded-constraint probe (probe pattern
  pre-ratified in G6.1 Stage 2; 3 probe records in-store, tagged,
  retained).

## Install

1. Copy `mev-cadence-plugin/` to `~/.hermes/plugins/mev-cadence/`.
2. In `~/.hermes/config.yaml`, under `plugins.entries.mev-cadence.settings`,
   set `engraphis_db: <absolute path to your Engraphis memory.db>`.
3. Restart the Hermes gateway.

## Verification

- `python tools/engraphis_lookup.py --db <db> census` — per-workspace totals.
- `python tools/engraphis_lookup.py --db <db> records --workspace mev-staging`
- Probe: `python tools/g6_probe.py --help`

## Provenance

Roof estate, campaigns/bench/micro-eviction. Full run log of record:
`reports/run-log-estate-repair-G2G3.md` (A4/A5 GREEN 2026-11-16).
MANIFEST.sha256 pins every packaged file.
