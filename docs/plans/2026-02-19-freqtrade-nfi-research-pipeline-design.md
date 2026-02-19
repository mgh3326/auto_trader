# Freqtrade + NostalgiaForInfinity Research Pipeline Design

**Date:** 2026-02-19  
**Status:** Approved

## 1. Problem Statement

We need a repeatable research pipeline using `freqtrade` + `NostalgiaForInfinity` (NFI) for Binance Spot, while keeping real trading execution strictly in `auto_trader`.

## 2. Goals

- Build a long-running research workflow for backtesting and strategy evaluation.
- Separate heavy research workloads (Mac) from always-on runtime duties (Raspberry Pi).
- Store research outputs in the existing `auto_trader` Postgres under a dedicated `research` schema.
- Keep strategy/version provenance explicit and reproducible.

## 3. Non-Goals

- Do not execute live trades with `freqtrade`.
- Do not automatically apply research outputs to `auto_trader` trading configuration.
- Do not merge `freqtrade` dependencies into this repository runtime stack.

## 4. Core Decisions (Confirmed)

- Market: **Binance Spot**.
- Main objective: **automated recurring research pipeline**.
- Infra split:
  - Mac: heavy backtests/hyperopt.
  - Raspberry Pi: always-on runtime responsibilities plus lightweight backtests.
- `freqtrade` usage: **research-only** (no dry-run/live in production path for trade execution).
- Real trading: **100% in `auto_trader`**.
- Output channel: generate files and persist parsed results to DB (no direct file copy into `auto_trader` repo).
- DB target: existing `auto_trader` Postgres, with `research` schema.
- Repository strategy: create and use a **fork-first** dedicated `freqtrade` project.

## 5. Repository and Environment Architecture

### 5.1 Dedicated Repository

- Use a separate repo (e.g. `auto-freqtrade`) based on a `freqtrade` fork.
- Track upstream `freqtrade` with a separate remote and controlled sync cadence.
- Pin NFI strategy by commit/tag for reproducibility.

### 5.2 Execution Profiles

- `mac-research-heavy`:
  - long-range backtests
  - hyperopt
  - cross-strategy comparison
- `pi-runtime`:
  - always-on operational services (non-trading execution helpers/monitoring)
- `pi-research-light`:
  - lightweight backtests only (short range, few pairs)

### 5.3 Guardrails

- No hyperopt on Pi.
- Pi backtests are bounded by fixed limits (time window + number of pairs).
- Promotion logic only labels candidates; no automatic deployment into `auto_trader` trade execution.

## 6. Data Flow

1. Run backtest (`heavy` on Mac or `light` on Pi).
2. Save raw run artifacts (JSON + markdown summary).
3. Parse key metrics and apply research gates.
4. Persist run metadata, metrics, pair details, and gate decisions to `research` schema.
5. Human reviews candidate records and manually reflects accepted ideas into `auto_trader`.

## 7. Reliability and Error Handling

- Isolate runtime and research processes/containers so research failures cannot disrupt always-on services.
- Handle Binance API transient failures with bounded retries and backoff.
- Fail backtest jobs on candle/data gaps and record explicit error reasons.
- Keep deterministic run metadata (strategy version, data range, runner host, config hash).

## 8. Research Quality Gates

### 8.1 Meaning of Minimum Trade Count

- `minimum_trade_count` means number of **closed trades** (entry+exit completed) within the test window.
- It is a **statistical reliability filter** for research acceptance, not a runtime deployment gate.

### 8.2 Candidate Gate Metrics

- minimum trade count
- profit factor lower bound
- max drawdown upper bound
- return/expectancy lower bound
- recent-window degradation tolerance vs baseline

Final threshold values are defined in implementation planning.

## 9. Database Design (`research` schema)

### 9.1 `research.backtest_runs`

- run metadata: run id, timestamps, exchange/market, strategy version, timeframe, timerange, runner (`mac`/`pi`)
- summary metrics: total trades, PF, MDD, win rate, expectancy, return
- artifact references: file path/hash

### 9.2 `research.backtest_pairs`

- per run + per pair metrics (return, trades, drawdown, etc.)
- used to detect pair-level outliers hidden by aggregate metrics

### 9.3 `research.promotion_candidates`

- gate results (`PASS`/`FAIL`) and failure reason codes
- represents recommendation state only, not auto-application

### 9.4 `research.sync_jobs`

- ingestion batch logs, error payloads, idempotency keys

## 10. Operational Outcome

- Research stack remains independent and reproducible in forked `freqtrade` project.
- `auto_trader` remains the sole live trading engine.
- Backtest intelligence is centralized in DB and reviewable over time.

