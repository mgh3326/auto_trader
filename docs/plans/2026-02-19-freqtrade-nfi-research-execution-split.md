# Freqtrade NFI Research Execution Split

**Date:** 2026-02-19  
**Purpose:** Quick execution split between `auto_freqtrade` and `auto_trader`.

## Source Plans

- `auto_freqtrade`: `/Users/robin/PycharmProjects/auto_freqtrade/docs/plans/2026-02-19-freqtrade-nfi-research-pipeline-implementation-plan.md`
- `auto_trader`: `/Users/robin/PycharmProjects/auto_trader/docs/plans/2026-02-19-freqtrade-nfi-research-pipeline-implementation-plan.md`

## Run In `auto_freqtrade`

- Task 1: Bootstrap fork workspace contract
- Task 2: Add NFI pin manifest and sync utility
- Task 3: Standardize backtest summary export format
- Task 8: Add lightweight Pi research job wrapper

Suggested start command:

```bash
cd /Users/robin/PycharmProjects/auto_freqtrade
```

## Run In `auto_trader`

- Task 4: Add `research` DB schema migration
- Task 5: Implement normalized payload schema/parser
- Task 6: Implement gate evaluator (`minimum_trade_count` + thresholds)
- Task 7: Add ingestion service + CLI entrypoint
- Task 9: Add runbook and operational checklists

Suggested start command:

```bash
cd /Users/robin/PycharmProjects/auto_trader
```

## Recommended Order

1. Complete `auto_freqtrade` Task 1-3 first (stable output format).
2. Implement `auto_trader` Task 4-7 (schema/parser/gate/ingestion).
3. Implement `auto_freqtrade` Task 8 (Pi light backtest wrapper).
4. Finish `auto_trader` Task 9 (runbook and final docs).

## Verification

In `/Users/robin/PycharmProjects/auto_trader`:

```bash
uv run ruff check app scripts tests
uv run pyright app
uv run pytest -q tests/test_research_backtest_parser.py tests/test_research_gate_service.py tests/test_research_ingestion_service.py
uv run pytest -q tests/integration/test_research_schema_migration.py tests/integration/test_ingest_freqtrade_report.py
```

In `/Users/robin/PycharmProjects/auto_freqtrade`:

```bash
pytest -q tests/test_repo_contract.py tests/test_sync_nfi_strategy.py tests/test_export_backtest_summary.py tests/test_run_bt_light.py
```

