# Backtest Report Design

**Date:** 2026-04-03

## Goal

Add a `--mode report` workflow to the backtest CLI so a human can inspect detailed strategy performance for one split plus cross-validation results, with both text and JSON output.

## Scope

- Add `report` mode and `--output text|json` to `backtest/backtest.py`
- Add `equity_dates` to `prepare.BacktestResult`
- Add new `backtest/report.py` for report generation and rendering
- Preserve existing `single` and `cv` behavior
- Do not modify `backtest/strategy.py`

## Approved Approach

Use the clean option: store equity timestamps directly in `BacktestResult` during `run_backtest()`, then build all reporting logic in a dedicated `backtest/report.py` module.

This keeps the reporting code out of the engine, avoids brittle date inference, and makes monthly drawdown and round-trip calculations deterministic.

## Report Structure

The report payload uses a nested dict with these top-level keys:

- `summary`
- `monthly_returns`
- `per_symbol`
- `top_trades`
- `bottom_trades`
- `cv`
- `risk_metrics`

`--output text` renders those sections in human-readable order. `--output json` serializes the same payload for later machine parsing.

## Core Rules

### Round-Trip Trade Definition

Trades are grouped by symbol and matched as one round trip from first `buy` until position size returns to zero. Partial sells stay inside the same round trip.

Each round trip stores:

- `symbol`
- `entry_date`
- `exit_date`
- `holding_days`
- `pnl`
- `return_pct`
- `entry_reason`
- `exit_reason`
- aggregate buy/sell amounts as needed for calculations

### Monthly Returns

Monthly rows are derived from `equity_dates` and `equity_curve`.

- Monthly return = `(month_end_equity / month_start_equity - 1) * 100`
- Monthly max drawdown = peak-to-trough drawdown within that month slice
- Monthly trades = count of raw trade log events in that month

### Risk Metrics

- `Calmar Ratio` = `CAGR / max_drawdown_ratio`
- `Avg Win / Avg Loss` from round-trip trades
- `Max Consecutive Losses` and `Max Consecutive Wins` from round-trip trade outcomes
- `Longest Drawdown Period` from equity staying below prior peak
- `Recovery Time from Max DD` from max drawdown trough until prior peak is recovered
- `Time in Market` from bar dates where any position is open divided by total bars

## CLI Behavior

`uv run backtest/backtest.py --mode report --split <split> --output <text|json> --interval <interval>`

Execution flow:

1. Load split data and run single backtest
2. Run CV
3. Build one payload with split metadata and CV fold summaries
4. Print text report or JSON

## Verification Target

At minimum verify:

- `uv run pytest tests/backtest/test_prepare.py ...`
- `uv run pytest tests/backtest/test_report.py ...`
- `uv run backtest/backtest.py --mode report`
- `uv run backtest/backtest.py --mode report --output json`
- `uv run backtest/backtest.py --mode single`
- `uv run backtest/backtest.py --mode cv`
