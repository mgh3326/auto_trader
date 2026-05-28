# ROB-353 PR1 — PIT data layer (Binance USD-M universe + klines) design

**Status:** approved (brainstorming, 2026-05-29)
**Issue:** ROB-353 (blocked-by ROB-349). This is **PR1 of 2**. PR2 = `specs → run_campaign`
bridge + the actual families 1–3 RUN + verdict artifact/report.
**Boundary:** research/backtest only. Read-only public data. No live, no Demo `confirm=true`,
no broker/order/watch/order-intent mutation, no scheduler/TaskIQ/Prefect/cron/daemon, no prod
DB/env/secret, no `/invest` exposure, no raw large data committed, no credential logging.

## Background

PR #993 (ROB-351) landed the cost-blind funnel **code + synthetic self-test** under
`research/nautilus_scalping/`, but no real Binance USD-M market-data verdict exists. The
`run_rob351_campaign.py` default mode is a print stub; `--self-test` builds `specs` from
hand-coded synthetic trades. There is **no bridge** that turns real OHLCV + a PIT manifest into
`campaign.run_campaign` inputs.

ROB-349 already built the hard part as a `/tmp/factor_research` prototype (read-only, public
data) and recorded it in three gstack notes:

- A PIT universe index over **all 843** archived Binance USD-M symbols
  (`build_pit_universe.py` → `pit_universe.csv`/`.json`, metadata only — not raw klines).
- Per-symbol PIT klines (`pit_raw/<SYM>/<SYM>-1d-YYYY-MM.csv`, standard Binance kline schema)
  and funding (`pit_fund/`).
- A PIT-membership backtest harness (`pit_backtest.py`) with freeze-tail trimming.

ROB-349 explicitly recommended durableizing this as **reusable infra** and verified the
survivorship blocker is resolvable from `data.binance.vision` alone (no vendor). It also already
re-ran the cross-sectional momentum/carry family (≈ ROB-353 **family 3**) on the corrected panel
and reached **reject / needs_more_data** (the strongest promote argument — smooth per-year carry
— collapsed once delisted coins were included). So ROB-353's only *untested* families are
**family 1 (breakout/range-expansion continuation)** and **family 2 (time-series trend basket)**.

PR1's job is therefore to **durableize the ROB-349 PIT data layer into the repo as reusable
infra**, wired to the existing `pit_universe.PITManifest` and `families.Bar`, so PR2 can run
families 1–2 through the official funnel and re-confirm family 3, leaving a durable verdict even
if the honest outcome is negative.

## Goals / non-goals

**Goals**
- Port the ROB-349 builder + PIT-membership trimming into committed repo modules.
- Commit a versioned **metadata-only** PIT universe manifest (JSON) with a snapshot hash.
- Provide a public-data klines fetcher (1d + 1h) and a bar loader that emits PIT-trimmed
  `families.Bar` series — the data half of the PR2 bridge.
- Tests + research-local import guards; full docs of source/window/universe/exclusions.

**Non-goals (deferred to PR2 or out of scope)**
- The `specs → campaign.run_campaign` bridge and the actual families 1–3 RUN / verdict report.
- Funding/OI/liquidation families (ROB-351 family 4/5; parked in `TODOS.md`).
- Any change to `validated_gate.py`, `campaign.py`, `rob343_label.py`, `frozen_config.py`.

## Components (all under `research/nautilus_scalping/`, pure stdlib + existing venv)

### 1. `pit_klines_fetcher.py`
Download USD-M klines daily dumps for `{1d, 1h}` from `data.binance.vision`, mirroring
`fetch_agg_trades.py` (pure stdlib `urllib`, public data only, `.CHECKSUM` verify when present,
404 ⇒ missing month tolerated, no keys). Writes to the **raw-data root** (gitignored), layout
`klines/<interval>/<SYMBOL>/<SYMBOL>-<interval>-YYYY-MM.csv`. CLI: `--symbol --interval
{1d,1h} --from --to [--market um]`. No secrets printed.

**Raw-data root resolution** (new tiny helper `pit_data_root()`, distinct from
`artifact_paths.resolve_artifact_path` which is reserved for citable discovery/gate outputs):
`AUTO_TRADER_RESEARCH_ARTIFACT_ROOT` if set (non-blank), else `<research>/data`. Either path is
gitignored, so raw klines never enter git regardless of which is used. Read via `os.environ` only.

- URL base: `https://data.binance.vision/data/futures/um/{daily|monthly}/klines/<SYM>/<iv>/`.

### 2. `pit_universe.py` (extend in place — keep PR #993 contract)
Add ROB-349 fields without breaking the existing minimal contract:

- `SymbolListing` gains optional `status: Literal["live","settling","dead"] | None`,
  `kline_coverage: float | None`, `funding_coverage: float | None`,
  `confidence: Literal["high","medium","low"] | None`, `missing_data_reason: str | None`.
  Existing `listed_from` / `delisted_at` (epoch ms, `delisted_at` exclusive) and
  `tradeable_at` / `validate` are **unchanged**. ROB-349 day-precise `active_from`/`active_to`
  map to `listed_from`/`delisted_at` via midnight-UTC → epoch ms during load.
- `from_records` / `to_records` round-trip the new optional fields (absent ⇒ `None`).
- New `PITManifest.strict_usdt_perp() -> PITManifest`: keep `status ∈ {live, dead}` `*USDT`
  perps; drop `settling`, dated/quarterly, BUSD/USDC-quoted, `*SETTLED`. This is the
  perp-only universe ROB-349 named for an honest re-run.
- `universe_as_of` unchanged (still the per-rebalance survivorship-safe authority).

### 3. `build_pit_universe.py`
Port the ROB-349 builder: read-only parallel S3 listing of `data.binance.vision/futures/um` +
bounded boundary-month downloads for non-live symbols, emitting the metadata manifest (JSON) and
a `*.meta.json` sidecar carrying a **snapshot hash** (sha256 over canonical manifest records) +
provenance (build window, symbol count, source URL, schema version). Reproduces / audits the
committed snapshot; never writes raw klines to git.

### 4. `pit_bars.py`
The data half of the PR2 bridge. `load_bars(symbol, interval, manifest) -> list[families.Bar]`:
read klines CSVs from the raw-data root, parse the standard kline schema, build `families.Bar`,
and apply **PIT membership trimming** — keep only `[first vol>0, last vol>0]` and drop the
post-delist price-frozen zero-volume tail — using the manifest's listing bounds. Also a
panel-aligned accessor for cross-sectional families (timestamp-indexed close/volume across the
`universe_as_of` membership at each rebalance). Pure transformation; no network.

### 5. Committed artifact (metadata only)
- `research/nautilus_scalping/data_manifests/pit_universe.v1.json` — the ROB-349 metadata
  manifest (~334 KB; 843 symbols). **JSON only** — `.gitignore` globally ignores `*.csv`, so the
  CSV stays a regenerable convenience export (not committed; avoids the force-add smell).
- `research/nautilus_scalping/data_manifests/pit_universe.v1.meta.json` — snapshot hash + build
  provenance. The v1 snapshot is the verified `/tmp/factor_research` manifest, re-validated
  through `PITManifest.from_records` before commit.

### 6. Tests (`research/nautilus_scalping/tests/`)
- Extend `test_pit_universe.py`: new optional fields round-trip; `strict_usdt_perp` keeps/drops
  the right classes; committed `pit_universe.v1.json` loads and its snapshot hash matches
  `*.meta.json` (stability guard); existing contract tests still pass.
- New `test_pit_bars.py`: synthetic klines CSVs prove trimming (leading/trailing zero-vol and
  freeze-tail removed; interior kept) and `families.Bar` mapping; panel alignment respects
  `universe_as_of`.
- New `test_pit_klines_fetcher.py`: **no network** — URL construction for `{1d,1h}`, daily/
  monthly path selection, 404-tolerance, and "no secrets in output" only.
- Guard (mirroring `test_discovery_paths.py`): new modules import no `app.*` Settings/pydantic;
  raw-data paths resolve under a gitignored root via `os.environ` only.

### 7. Docs
`docs/runbooks/rob-353-pit-data-layer.md`: data source + retrieval,
date window, intervals, USDT-perp-only universe, active+delisted handling, exclusions
(BUSD/USDC/dated/SETTLED/settling), liquidity/membership timestamps, manifest format + snapshot
hash, raw-data root path shape (no secrets), and the fetch procedure. Feeds PR2's required
"data/universe definition" report section.

## Data flow

```
build_pit_universe.py ──(read-only S3 list + boundary dl)──> data_manifests/pit_universe.v1.json  (committed, metadata only)
pit_klines_fetcher.py ──(public daily/monthly dumps)───────> <raw-root>/klines/<iv>/<SYM>/...      (gitignored)
                                                                      │
PITManifest.load(pit_universe.v1.json).strict_usdt_perp() ───────────┤
                                                                      ▼
                                          pit_bars.load_bars(sym, iv, manifest) → [families.Bar...] (PIT-trimmed)
                                                                      │
                                                                      ▼  (PR2: build specs → campaign.run_campaign)
```

## Safety / boundaries
- Raw klines + `*.csv` never committed (`data/`, `*.csv` already gitignored; raw-data root is
  gitignored). Only metadata JSON manifests are committed.
- Research-local `os.environ` only; zero `app.*` imports (ROB-339 boundary, guard-tested).
- Network limited to public `data.binance.vision`, read-only, no secrets. No broker/order/
  scheduler/DB/env mutation.
- PR1 stops at the data layer. The `specs → run_campaign` bridge and the RUN are PR2.

## Acceptance (PR1)
- New modules + extended `pit_universe.py` committed with tests green under uv/Python 3.13.
- `pit_universe.v1.json` committed (metadata only) with a matching snapshot-hash sidecar.
- `pit_bars.load_bars` returns PIT-trimmed `families.Bar` series proven by tests.
- Fetcher CLI documented; runbook covers the full data/universe definition.
- Import guards + gitignore confirm no `app.*` import and no raw-data/secret leakage.
- `run_rob351_campaign.py --self-test` still passes; config hash unchanged.

## Expectations (honesty note)
ROB-349 already labeled the cross-sectional family (≈ family 3) reject/needs_more_data on the
PIT-corrected panel, and ROB-316/320/324/339/342 repeatedly killed generic trend/momentum/
reversal families net-negative. The likely PR2 outcome is mostly reject/needs_more_data →
recommend family 4/5 feasibility. PR1's value is the durable, auditable, reusable PIT data layer
that lets PR2 reach that verdict through the committed ex-ante funnel rather than ad hoc.
