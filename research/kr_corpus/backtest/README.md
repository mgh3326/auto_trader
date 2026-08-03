# KR backtest harness (Stage A — fixture wiring)

`JOB_PURPOSE=BACKTEST_HARNESS_WIRING_ONLY`

Research-only surface under `research/kr_corpus/backtest/`. Wires:

* declared schema contract (`schema_contract.v1.json`) — **INFERRED_FROM_LITERALS**
* manifest SHA-256 gate before parquet parse
* dual holdout refusal (path `HOLDOUT_DIR` + date `HOLDOUT_WINDOW`) as **exceptions**
* PIT universe from membership snapshots only
* explicit delisted terminal events (no silent drop)
* baseline pipeline smoke `value_rank_topN_D5` labeled **`PIPELINE_SMOKE_NOT_A_STRATEGY`**

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
