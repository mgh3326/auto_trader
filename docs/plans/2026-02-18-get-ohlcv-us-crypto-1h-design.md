# get_ohlcv US/Crypto 1H Design

**Date:** 2026-02-18
**Status:** Approved

## 1. Problem Statement

`get_ohlcv` currently supports `day/week/month` for all markets and `4h` for crypto.
US intraday (`1h`) is not yet supported, and crypto `1h` is also unavailable through the same MCP contract.

## 2. Goals

- Add `period="1h"` support to MCP `get_ohlcv`.
- Allow `1h` only for US equity and crypto.
- Keep existing `4h` policy unchanged (`crypto` only).
- Keep existing count caps unchanged:
  - US equity: `100`
  - crypto: `200`
- Keep day/week/month cache and closed-bucket behavior backward compatible.

## 3. Non-Goals

- Do not add `1h` support for KR equity (KIS path).
- Do not change indicator default timeframes.
- Do not add new MCP tools (no `get_ohlcv_intraday` split).
- Do not add new aliases; public contract accepts `1h` as the canonical value.

## 4. User-Facing Contract

### 4.1 Allowed periods

- `day`, `week`, `month`, `4h`, `1h`

### 4.2 Market restrictions

- `period="4h"`: crypto only (existing rule).
- `period="1h"`: US equity and crypto only.
- `period="1h"` with KR equity should raise:
  - `ValueError("period '1h' is not supported for korean equity")`

### 4.3 Count caps

- US path remains capped at `100`.
- Crypto path remains capped at `200`.

## 5. Architecture

### 5.1 MCP routing (`app/mcp_server/tooling/market_data_quotes.py`)

- Extend `get_ohlcv` period validation to include `1h`.
- Keep existing `4h` crypto-only guard.
- Add new guard:
  - if `period == "1h"` and resolved market is `equity_kr`, reject with explicit `ValueError`.
- Reuse existing market-specific fetchers:
  - US -> `_fetch_ohlcv_equity_us(...)`
  - crypto -> `_fetch_ohlcv_crypto(...)`

### 5.2 Yahoo service (`app/services/yahoo.py`)

- Extend period map with `1h -> 60m`.
- Cache path remains only for `day/week/month`.
- Closed-bucket filtering remains only for `day/week/month`.
- `1h` returns raw fetched candles (no cache/bucket filtering).

### 5.3 Upbit service (`app/services/upbit.py`)

- No contract change required for this scope.
- Existing intraday-capable interval routing (`1h` and `4h`) is reused.

## 6. Data Flow

1. `get_ohlcv(symbol, count, period, market, end_date)` validates input.
2. Symbol is normalized and market type is resolved.
3. Guard checks:
   - `4h` requires crypto.
   - `1h` rejects KR equity.
4. Request dispatches to market-specific fetcher.
5. Service layer fetches candles and returns normalized rows.

## 7. Testing Strategy

### 7.1 MCP contract tests (`tests/test_mcp_server_tools.py`)

- Add success case: US equity `period="1h"`.
- Add success case: crypto `period="1h"`.
- Add rejection case: KR equity `period="1h"`.
- Update invalid-period expectation message to include `1h`.

### 7.2 Yahoo service tests (`tests/test_services.py`)

- Add test verifying `fetch_ohlcv(period="1h")` uses `yf.download(..., interval="60m")`.

### 7.3 Yahoo cache boundary tests (`tests/test_yahoo_service_cache.py`)

- Add test ensuring `period="1h"` does not call cache entrypoint and follows raw path.

## 8. Documentation Updates

- Update `app/mcp_server/README.md` `get_ohlcv` period contract to include `1h`.
- Explicitly note market restrictions:
  - `4h`: crypto only
  - `1h`: US equity + crypto (KR not supported)

## 9. Risks and Mitigations

- Risk: Ambiguity around intraday period support by market.
  - Mitigation: explicit market guard tests and README contract update.
- Risk: Regression in existing day/week/month cache behavior.
  - Mitigation: confine cache changes to period allowlist only; keep current branches intact.

## 10. Rollout Scope

- Included:
  - MCP `get_ohlcv` supports `1h` for US/crypto.
  - Yahoo `1h` fetch support via `60m` interval.
- Deferred:
  - KR intraday support.
  - Intraday caching/closed-bucket policy for `1h`.
