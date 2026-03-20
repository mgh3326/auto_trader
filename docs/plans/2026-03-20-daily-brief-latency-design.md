# Daily Brief Latency Design

## Goal

Reduce latency of `GET /api/n8n/daily-brief` without changing the response schema or router contract.

## Problem Summary

The current `daily-brief` flow duplicates expensive downstream work inside a single request.

Current call graph:

1. `fetch_daily_brief()` calls `fetch_pending_orders(market="all")`
2. `fetch_daily_brief()` calls `fetch_market_context(symbols=None)`
3. `fetch_market_context()` calls `fetch_pending_orders()` again when `symbols` is `None`
4. `fetch_daily_brief()` calls `_fetch_yesterday_fills()`
5. `_fetch_yesterday_fills()` calls `fetch_pending_orders()` again
6. `_fetch_yesterday_fills()` calls `_get_portfolio_overview()` again
7. `_fetch_yesterday_fills()` performs per-symbol filled-order lookups using the duplicated symbol set

This creates repeated `http.client` spans for:

- pending-order fanout across brokers
- current-price lookups
- crypto indicator OHLCV/ticker lookups
- per-symbol filled-order history lookups
- portfolio and quote retrieval

## Constraints

- Keep `GET /api/n8n/daily-brief` response schema unchanged
- Keep router signature unchanged
- Remove duplicate work first; do not redesign filled-order retrieval into a new external contract in this change
- Preserve partial-failure behavior: the endpoint should still return a successful response with collected errors when one sub-source fails

## Options Considered

### Option 1: Shared Request Context

Build shared intermediate results once inside `fetch_daily_brief()` and pass them to downstream helpers.

Pros:

- removes the largest duplicated work
- preserves schema
- keeps changes contained to service orchestration
- makes future performance work easier because data dependencies become explicit

Cons:

- requires helper signatures to change
- introduces a two-stage orchestration flow instead of one big `gather()`

### Option 2: In-request Memoization Wrapper

Wrap existing service calls with request-local caching.

Pros:

- narrower code change

Cons:

- hides coupling instead of fixing it
- leaves the current N+1 structure intact
- makes future maintenance harder

### Option 3: Bulk Filled-order Redesign

Replace per-symbol yesterday-fill collection with market-level bulk queries.

Pros:

- potentially the largest latency drop

Cons:

- broader behavior change
- more broker-specific complexity
- outside the approved scope for this pass

## Chosen Design

Use Option 1.

`fetch_daily_brief()` will become a two-stage orchestrator.

### Stage 1: Collect Shared Inputs Once

Run these in parallel:

- `fetch_pending_orders(market="all", include_indicators=False, ...)`
- `_get_portfolio_overview(...)`

Then derive:

- `symbols_by_market`
- `crypto_symbols_for_context`

Important details:

- `include_indicators=False` is safe here because the daily brief pending-order section does not render per-order indicators
- `symbols_by_market` should combine pending orders and portfolio positions so `yesterday_fills` still sees the same symbol universe as before
- symbol normalization should preserve the existing market-specific symbol shape expected by downstream consumers

### Stage 2: Reuse Shared Inputs

Run these in parallel:

- `fetch_market_context(market="crypto", symbols=crypto_symbols_for_context, ...)`
- `_fetch_yesterday_fills(markets=effective_markets, symbols_by_market=symbols_by_market)`

This removes:

- the nested `fetch_pending_orders()` inside `fetch_market_context()`
- the nested `fetch_pending_orders()` inside `_fetch_yesterday_fills()`
- the nested `_get_portfolio_overview()` inside `_fetch_yesterday_fills()`

## Helper Changes

### `app/services/n8n_daily_brief_service.py`

Add a helper to build `symbols_by_market` from:

- normalized pending-order rows
- portfolio positions

Change `_fetch_yesterday_fills()` to accept `symbols_by_market` as input and stop fetching its own pending orders or portfolio overview.

Change `fetch_daily_brief()` to:

- collect shared inputs once
- pass explicit symbols to `fetch_market_context()`
- pass shared symbols to `_fetch_yesterday_fills()`

### `app/services/n8n_market_context_service.py`

No contract change is required.

The existing `fetch_market_context()` already skips its internal pending-order fetch when `symbols` is provided. The refactor should rely on that behavior rather than broadening the service API.

## Expected Impact

The change should reduce repeated external calls in a single `daily-brief` request by:

- eliminating two extra pending-order fetch paths
- eliminating one extra portfolio overview fetch path
- avoiding unnecessary indicator enrichment in the top-level pending-order fetch

This should significantly reduce the total `http.client` span count for the trace, even before any deeper filled-order bulk optimization.

## Risks

### Symbol Drift

If `symbols_by_market` derivation does not exactly match the previous combination of pending orders plus portfolio positions, `yesterday_fills` may miss symbols.

Mitigation:

- add regression tests that assert both pending and portfolio symbols are forwarded

### Behavior Drift for Empty Crypto Symbols

`fetch_market_context()` currently falls back to `["BTC"]` when no symbols are available.

Mitigation:

- preserve the current fallback behavior by passing an empty list only when the service still handles fallback consistently, or normalize to `["BTC"]` in `daily-brief`

### Partial Failure Regression

The current endpoint tolerates source failures and still returns a response.

Mitigation:

- keep `asyncio.gather(..., return_exceptions=True)` in both orchestration stages
- preserve the current error collection behavior

## Test Strategy

Add or update unit tests for `fetch_daily_brief()` to verify:

- `fetch_pending_orders()` is called once with `include_indicators=False`
- `fetch_market_context()` receives explicit crypto symbols instead of `symbols=None`
- `_fetch_yesterday_fills()` receives the shared `symbols_by_market`
- partial failures still produce a successful top-level response with errors

No API schema test changes are expected because the endpoint contract stays the same.
