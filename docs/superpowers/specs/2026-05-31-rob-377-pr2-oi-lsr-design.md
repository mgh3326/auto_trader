# ROB-377 PR2 — Derivatives sentiment: Open Interest + Long/Short Ratio (Binance public read-only)

**Date:** 2026-05-31
**Issue:** ROB-377 §A2 (parent ROB-369 Slice 4)
**Scope:** PR2 of a 3-PR slicing. PR1 (A1 crypto market index) is MERGED
(`ef4ae091`). This PR covers **A2** only: surface Binance USD-M open interest
and long/short ratio as MCP read tools. Liquidations are parked (no archive
vendor — see ROB-355). Upbit digital-asset indices are a separate follow-up
(PR3). A3 (on-chain/ETF) is parked.

## Problem

Crypto reports can read funding (`get_funding_rate`) but have no MCP visibility
into **open interest (OI)** or **long/short positioning (LSR)** — two of the
core derivatives-sentiment signals. They currently depend on out-of-band
(9222 Upbit) inspection.

## Goal (acceptance, ROB-377 AC ③)

Two new MCP read tools expose OI and LSR from Binance USD-M futures:

- `get_open_interest(symbol, period, limit)` → current OI + recent OI history +
  trend.
- `get_long_short_ratio(symbol, period, limit)` → global-account ratio (retail
  sentiment) + top-trader position ratio (smart money) + a divergence note.

Both are **public, read-only, no-auth** Binance endpoints on `fapi.binance.com`.

## The A2 gotcha (must be stated in the PR)

ROB-356's funding+OI feature builder consumes `data.binance.vision` **archives**
(monthly/daily zip, for PIT backtesting). This PR is a **live read client** over
different endpoints (`fapi.binance.com/futures/data/*` + `/fapi/v1/openInterest`).
It does **not** reuse ROB-356's archive code — surfacing the archive builder as a
live tool would be wrong (stale, not real-time).

## Approach — mirror the existing `get_funding_rate` pattern

`get_funding_rate` already implements the exact 4-layer shape we need, against
the same host. Extend each layer:

1. **Source** — `app/mcp_server/tooling/fundamentals_sources_binance.py`:
   add `_fetch_open_interest(symbol, period, limit)` and
   `_fetch_long_short_ratio(symbol, period, limit)`, plus URL constants; export
   in `__all__`.
2. **Handler** — `app/mcp_server/tooling/fundamentals/_crypto.py`:
   add `handle_get_open_interest()` and `handle_get_long_short_ratio()`,
   mirroring `handle_get_funding_rate` (normalize symbol via
   `_normalize_crypto_base_symbol`, validate, call source, `except` →
   `_error_payload(source="binance", instrument_type="crypto")`).
3. **Registration** — `app/mcp_server/tooling/fundamentals_handlers.py`:
   add two `@mcp.tool(...)` registrations + import the two handlers.
4. **Surface** — `app/mcp_server/__init__.py`:
   add `"get_open_interest"` and `"get_long_short_ratio"` to
   `AVAILABLE_TOOL_NAMES`.

## Endpoints (all `https://fapi.binance.com`, public, no-auth)

| Tool | Endpoint(s) |
|------|-------------|
| OI current | `GET /fapi/v1/openInterest?symbol=BTCUSDT` → `{openInterest, symbol, time}` |
| OI history | `GET /futures/data/openInterestHist?symbol=&period=&limit=` → `[{sumOpenInterest, sumOpenInterestValue, timestamp}]` |
| LSR global | `GET /futures/data/globalLongShortAccountRatio?symbol=&period=&limit=` → `[{longShortRatio, longAccount, shortAccount, timestamp}]` |
| LSR top-position | `GET /futures/data/topLongShortPositionRatio?symbol=&period=&limit=` → `[{longShortRatio, longAccount, shortAccount, timestamp}]` |

`/futures/data/*` history is most-recent-first or oldest-first depending on
Binance; the implementation sorts the returned history by timestamp ascending
and takes the latest entry as "current" for the ratio tools.

## Tool contracts

### `get_open_interest(symbol: str, period: str = "1h", limit: int = 30)`

```jsonc
{
  "symbol": "BTCUSDT",
  "current_open_interest": 123456.789,           // contracts, from /fapi/v1/openInterest
  "period": "1h",
  "open_interest_history": [
    {"time": "2026-05-31T00:00:00Z",
     "sum_open_interest": 123000.0,              // contracts
     "sum_open_interest_value_usd": 8.1e9}       // notional USD
  ],
  "oi_change_pct": 2.34,                          // (last - first) / first * 100 over the window, null if <2 points
  "interpretation": "OI 증가 — 신규 포지션 유입 (추세 강화 가능)" // by sign of oi_change_pct
}
```

### `get_long_short_ratio(symbol: str, period: str = "1h", limit: int = 30)`

```jsonc
{
  "symbol": "BTCUSDT",
  "period": "1h",
  "global_account": {                             // globalLongShortAccountRatio — retail
    "ratio": 1.85, "long_pct": 64.9, "short_pct": 35.1,
    "history": [{"time": "...", "ratio": 1.8, "long_pct": 64.3, "short_pct": 35.7}],
    "interpretation": "리테일 계정 롱 우위 (ratio>1)"
  },
  "top_position": {                               // topLongShortPositionRatio — smart money
    "ratio": 0.92, "long_pct": 47.9, "short_pct": 52.1,
    "history": [...],
    "interpretation": "상위 트레이더 포지션 숏 우위 (ratio<1)"
  },
  "divergence_note": "리테일 롱 / 스마트머니 숏 — contrarian 주의" // derived from the two current ratios
}
```

`longAccount`/`shortAccount` from Binance are proportions in `[0,1]`; surface as
percentages (`*100`, rounded). `ratio` = `longShortRatio` passthrough (float).

## Parameter validation

- `symbol` **required** (decision: no batch — `/futures/data/*` is per-symbol and
  has no all-symbols snapshot). Empty/missing → `ValueError`.
- `period` ∈ `{"5m","15m","30m","1h","2h","4h","6h","12h","1d"}` (Binance's
  allowed set) else `ValueError` (mirrors `get_market_index` period validation).
- `limit` clamped to `[1, 500]` (Binance max), default `30`.

## Interpretation / divergence logic (deterministic, no price coupling)

- OI: `oi_change_pct > 0` → "OI 증가 — 신규 포지션 유입"; `< 0` → "OI 감소 —
  포지션 청산/이탈"; `== 0` or `null` → neutral/"데이터 부족". No price coupling
  (we don't fetch price here), so phrasing is about position flow only.
- LSR per-leg: `ratio > 1` → 롱 우위; `< 1` → 숏 우위; `== 1` → 균형.
- `divergence_note`: compare `global_account.ratio` vs `top_position.ratio`
  across the 1.0 boundary — if retail and top traders sit on opposite sides,
  emit a contrarian-caution note; if same side, emit an alignment note; if a leg
  is unavailable, emit "divergence 판단 불가 (일부 데이터 없음)".

## Error handling & safety boundary

- **Read-only.** No broker/order/watch/order-intent mutation reachable.
- **No secrets.** Public endpoints; no API key, no signing, nothing printed or
  committed.
- **No new host.** `fapi.binance.com` is already used by `get_funding_rate`
  (URL constants pinned in the source module).
- **No scheduler.** No TaskIQ/Prefect/cron activation.
- **Fail-open**, identical to `get_funding_rate`: `httpx.AsyncClient(timeout=10)`
  + `.raise_for_status()`; the handler's `try/except` maps any failure to
  `_error_payload(source="binance", ...)`. A Binance outage degrades only these
  tools — never crashes a caller or fabricates values.

## Test plan (TDD)

Mirror `TestGetFundingRate` in `tests/test_mcp_fundamentals_tools.py`
(monkeypatch `httpx.AsyncClient.get`, drive via `build_tools()`):

1. `get_open_interest(symbol="BTC")` happy path → `current_open_interest`,
   `open_interest_history` rows, `oi_change_pct` computed, `interpretation`
   matches the change sign, `symbol == "BTCUSDT"`.
2. `oi_change_pct` is `null` when history has <2 points; interpretation neutral.
3. `get_long_short_ratio(symbol="BTC")` happy path → `global_account` +
   `top_position` each with `ratio`/`long_pct`/`short_pct`/`history`, and a
   `divergence_note` reflecting the two current ratios.
4. `divergence_note` contrarian vs aligned cases (retail long + top short ⇒
   contrarian; both long ⇒ aligned).
5. Missing/empty `symbol` → `ValueError` for both tools.
6. Invalid `period` → `ValueError` for both tools.
7. `limit` clamped to `[1, 500]` (e.g. 0 → 1, 9999 → 500) — assert the value
   passed to the Binance request.
8. Binance failure (mocked raise) → `_error_payload(source="binance")` for both
   tools (fail-open; no fabricated rows).
9. Source-level unit tests for `_fetch_open_interest` / `_fetch_long_short_ratio`
   (row shape + computed fields) with mocked httpx.
10. Regression: existing `TestGetFundingRate` and tool-registry tests pass;
    `AVAILABLE_TOOL_NAMES` includes the two new names.

## Verification

Deterministic unit tests prove AC ③. A live Binance round-trip is
operator-verifiable but **not a merge gate** (consistent with other slices).
**Linear Done is gated on main CI green + operator smoke evidence** — do not mark
Done before then.

CI gate before merge (repo convention): `ruff check app/ tests/` +
`ruff format --check app/ tests/` + `ty` + the Test workflow green.

## Files touched

- `app/mcp_server/tooling/fundamentals_sources_binance.py` (2 fetchers + URL constants)
- `app/mcp_server/tooling/fundamentals/_crypto.py` (2 handlers)
- `app/mcp_server/tooling/fundamentals_handlers.py` (2 registrations + import)
- `app/mcp_server/__init__.py` (2 tool names)
- `tests/test_mcp_fundamentals_tools.py` (new `TestGetOpenInterest` + `TestGetLongShortRatio`)

No migration. No new dependency. No new external host.
