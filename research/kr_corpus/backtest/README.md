# KR backtest harness (Stage A wiring + Stage-B execution bridge)

`JOB_PURPOSE=BACKTEST_HARNESS_WIRING_ONLY`

Research-only surface under `research/kr_corpus/backtest/`. Wires:

* declared schema contract (`schema_contract.v1.json`) — **SEALED_CORPUS_V1**
* manifest SHA-256 gate before parquet parse
* dual holdout refusal (path `HOLDOUT_DIR` + date `HOLDOUT_WINDOW`) as **exceptions**
* PIT universe from membership snapshots only
* explicit delisted terminal events (no silent drop)
* common KR/US baseline pipeline smoke `liquidity_proxy_decile_topN_D5` labeled **`PIPELINE_SMOKE_NOT_A_STRATEGY`**
* KR Stage-B `rev3_reclaim` bridge, using the shadow3 canonical signal owner
  (`research.three_market_shadow.calculations.calculate_signal`)

## Stage-B contract

Stage-B is a research backtest track, separate from execution acceptance. It
uses **t+1 open** for entry and **D+5 close** for exit. A run must explicitly
name `--cost-profile 43bp` or `--cost-profile 83bp`; there is no default cost
injection. The approved literals are:

* `43bp`: fee 3bp + transaction tax 20bp + slippage 10bp per side.
* `83bp`: fee 3bp + transaction tax 20bp + slippage 30bp per side.

The real-data runner verifies selected main-snapshot parquet bytes against
`checksums.sha256`, refuses holdout paths/dates, and writes canonical
`honest_trial.v3` evidence only after closed-trade statistics exist:

```bash
uv run python scripts/run_kr_stageb.py \
  --artifact-root /Users/mgh3326/work/herdr-artifacts/kr-corpus-v1 \
  --run-id kr-corpus-v1-20260803-1001 \
  --start 2015-01-01 --end 2024-12-31 \
  --market KOSPI --market KOSDAQ --max-symbols 20 \
  --cost-profile 43bp \
  --evidence /path/to/stageb-evidence.json
```

The result is descriptive research evidence only. It does not authorize
orders, account mutation, broker calls, holdout access, or promotion.

## D5 baseline definition

The baseline cannot rank by `trading_value`: the KR corpus `value` field is
100% null over the observed range, while the US intraday schema has no such
field. The common KR/US liquidity cohort is therefore the top decile of the
`close × volume` proxy at each session (top 10%, minimum one symbol), with
top-N equal-weight selection inside that cohort. This is a **proxy**, not
exchange-reported turnover. It can differ from actual turnover because it uses
the session close rather than VWAP and ignores the intraday price path,
trade-size distribution, and venue effects.

At session `t`, only bars with `session_date <= t` and membership rows with
`session_date <= t` are admitted; the ranking uses the current session's bar
only. This is the baseline's PIT rule. The US universe is a frozen active-symbol
snapshot and carries survivorship bias; this baseline does not correct it.

## Stage A constraints

* `CORPUS_ARTIFACT_ROOT` reads: **0**
* Holdout reads: **0**
* Real-data smoke: **Stage B only** (separate instruction)

## Fill assumption (same-bar close)

Baseline smoke marks entries and exits at session **t close on the decision
session** (same-bar close fill). It is **not** a t+1 open model. Documented
here and in `baseline_smoke.py` so the assumption is not silent.

## Commands

```bash
# unit tests
uv run pytest research/kr_corpus/backtest/tests/ -v

# fixture smoke — uses committed fixtures/synthetic_v1 (SHA gate real)
cd research/kr_corpus/backtest && uv run python smoke_cli.py

# optional: regenerate fixture then smoke (authoring only; not default)
cd research/kr_corpus/backtest && uv run python smoke_cli.py --rebuild-fixture
```

## Reuse

* `research/alpaca_track/persistence.py` — single-buffer SHA-then-parse load pattern
* `research_contracts.evaluation_windows.ClosedWindow` — window bounds
* Walk-forward / PIT boundary discipline inspired by `research/alpaca_track_walkforward`
