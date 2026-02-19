# Freqtrade Research Pipeline Runbook

## Scope

- `auto_freqtrade` runs research backtests only.
- `auto_trader` remains the only live trading runtime.

## Daily Research Routine

1. Run full backtest on Mac in `auto_freqtrade`.
2. Run lightweight sanity backtest on Raspberry Pi in `auto_freqtrade`.
3. Export normalized summary JSON from `auto_freqtrade`.
4. Ingest summary into `auto_trader` research schema.
5. Review promotion candidate PASS/FAIL outputs.

## Ingestion Command

```bash
uv run python scripts/ingest_freqtrade_report.py \
  --input /absolute/path/to/summary.json \
  --runner mac \
  --minimum-trade-count 20 \
  --minimum-profit-factor 1.2 \
  --maximum-drawdown 0.25
```

## Operational Checks

- Verify migration head is applied before ingestion: `uv run alembic upgrade head`.
- Verify payload includes required fields: `run_id`, `strategy_name`, `timeframe`, `runner`, `total_trades`, `profit_factor`, `max_drawdown`.
- Verify ingestion log has `Research summary ingested: run_id=...`.
- Verify research tables updated:
  - `research.backtest_runs`
  - `research.backtest_pairs`
  - `research.promotion_candidates`
  - `research.sync_jobs`

## Failure Checklist

1. Check JSON payload path and format.
2. Re-run with same `--idempotency-key` only for intentional retry.
3. Check DB connectivity and schema availability.
4. Check gate threshold values and failure reason code.
5. Record failed run context in `research.sync_jobs.error_payload`.
