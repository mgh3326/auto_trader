# get_ohlcv Crypto 4H + Upbit Candle Core Design

**Date:** 2026-02-18
**Status:** Approved

## 1. Problem Statement

Current MCP `get_ohlcv` only supports `period="day|week|month"`.
For crypto entry timing, daily candles are too coarse, while lower-minute candles are too noisy for the current DCA style.
The highest-priority missing timeframe is `4h`.

At the same time, we already know we will likely need additional intraday intervals later (`1m/5m/15m/1h`).
A one-off `4h` special case in MCP would create technical debt.

## 2. Goals

- Add `period="4h"` support to MCP `get_ohlcv`.
- Restrict `4h` to crypto only.
- Keep existing `day/week/month` behavior backward compatible.
- Prepare Upbit service internals so future intraday intervals can be added with minimal code churn.
- Keep API load bounded with current cap policy (`count`/`days` capped at 200 for Upbit candles).

## 3. Non-Goals

- Do not expose `1m/5m/15m/1h` in MCP in this change.
- Do not change indicator pipeline timeframe defaults (currently daily-based).
- Do not add intraday caching/closed-bucket logic in this change.
- Do not add `hour4` alias (only `4h` accepted).

## 4. User-Facing Contract Decisions

### 4.1 MCP `get_ohlcv`

- Allowed periods become: `day`, `week`, `month`, `4h`.
- `period="4h"` is valid only when resolved market type is `crypto`.
- For non-crypto with `4h`, raise:
  - `ValueError("period '4h' is supported only for crypto")`
- Existing behavior for `day/week/month` remains unchanged.

### 4.2 Count Handling

- Keep current clamp semantics for Upbit candles.
- If requested count is over 200, clamp to 200 (no hard failure).

## 5. Architecture

### 5.1 Upbit Service Refactor Direction

Introduce a shared Upbit candle core used by `upbit.fetch_ohlcv`.

- New internal candle-fetch path (private helper):
  - Handles interval routing for both calendar candles and minute candles.
  - Interval routing targets:
    - `day -> /candles/days`
    - `week -> /candles/weeks`
    - `month -> /candles/months`
    - `4h -> /candles/minutes/240`
  - Designed so future mapping expansion (`1m/5m/15m/1h/...`) is data-only.

`upbit.fetch_ohlcv(...)` stays as the public compatibility boundary and delegates to the shared helper.

### 5.2 Caching and Closed-Bucket Behavior

- Keep existing cache and closed-bucket filtering only for `day/week/month`.
- `4h` bypasses this cache/filter path for now.

Reason:
- Current cache modules (`upbit_ohlcv_cache`) are day/week/month bucket semantics.
- Extending those semantics to intraday is separate work.

## 6. Data Flow

1. MCP `get_ohlcv(symbol, count, period, market, end_date)` validates input.
2. Market is resolved (`crypto` / `equity_kr` / `equity_us`).
3. If `period="4h"` and market is not crypto -> immediate `ValueError`.
4. Crypto path calls `upbit.fetch_ohlcv(market=symbol, days=min(count, 200), period=period, end_date=parsed_end_date)`.
5. Upbit service normalizes period and dispatches through shared candle core.
6. Response rows are normalized via existing MCP row normalizer.

## 7. Testing Strategy

## 7.1 MCP Tool Tests (`tests/test_mcp_server_tools.py`)

Add tests for:
- `get_ohlcv` crypto `period="4h"` success path.
- `count>200` still clamps to 200 with `period="4h"`.
- `period="4h"` with `market="kr"` raises expected `ValueError`.
- `period="4h"` with `market="us"` raises expected `ValueError`.
- Invalid-period test message updated to include `4h` as allowed period.

## 7.2 Upbit Service Tests (`tests/test_services.py` and/or focused Upbit service tests)

Add/adjust tests for:
- `fetch_ohlcv(period="4h")` uses Upbit minute-240 route.
- Existing `day/week/month` behavior remains intact.
- Clamp behavior remains intact for counts over 200.

## 8. Documentation Updates

Update MCP docs:
- `app/mcp_server/README.md`
  - Explicitly document `period="4h"` support (crypto-only).

## 9. Alternatives Considered

1. MCP-only `4h` special case without Upbit service refactor.
- Rejected due to future intraday expansion needs and duplicated branching.

2. Fully generalize public API immediately to all minute intervals.
- Deferred to avoid widening user-facing contract in one step.

3. Add separate `get_ohlcv_intraday` tool.
- Rejected because it fragments candle access contract and increases tool surface.

## 10. Risks and Mitigations

- Risk: intraday-specific semantics (session closure, caching) are not handled yet.
- Mitigation: keep intraday path isolated from day/week/month cache semantics.

- Risk: error message or period validation regressions in MCP tests.
- Mitigation: add explicit contract tests for `4h` acceptance and non-crypto rejection.

- Risk: hidden dependencies assuming only `day/week/month` in Upbit service.
- Mitigation: keep `fetch_ohlcv` signature unchanged and preserve old paths for existing periods.

## 11. Rollout Scope

- Included now:
  - MCP `get_ohlcv` accepts `4h` for crypto.
  - Upbit shared candle-core wiring sufficient for `4h` support and future extension.
- Deferred:
  - Public exposure of `1m/5m/15m/1h`.
  - Intraday candle caching policy.
  - Indicator timeframe customization.
