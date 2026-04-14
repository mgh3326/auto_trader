# Fix: Paper Portfolio `market` Kwarg Mismatch (#501)

**Date:** 2026-04-14
**Issue:** https://github.com/mgh3326/auto_trader/issues/501
**Status:** Approved

## Problem

`paper_portfolio_handler.collect_paper_positions()` calls
`PaperTradingService.get_positions(account_id=..., market=market_filter)`,
but the service signature only accepts `account_id`. This causes a runtime
`TypeError: unexpected keyword argument 'market'` when MCP portfolio tools
query paper accounts.

Tests did not catch this because fake services accept `market=None` even
though the real service does not.

## Design

### 1. Service Signature Change

**File:** `app/services/paper_trading_service.py` — `get_positions()`

- Add `market: str | None = None` parameter.
- When `market` is provided, add `PaperPosition.instrument_type == market`
  to the SQL WHERE clause.
- When `market is None`, return all positions (existing behavior).
- Internal callers (`get_position`, `get_portfolio_summary`,
  `calculate_performance`) pass no `market` argument — no changes needed.

### 2. Handler — No Change

**File:** `app/mcp_server/tooling/paper_portfolio_handler.py`

The existing call `service.get_positions(account_id=account.id, market=market_filter)`
becomes valid after the service change. The defensive post-filter at lines
228-232 stays as a safety net.

### 3. Tests

**Integration test (new):** Verify `PaperTradingService.get_positions(market=...)`
against a real async DB session.

- Insert positions with `equity_kr`, `equity_us`, `crypto` instrument types.
- Assert `market="equity_kr"` returns only KR positions.
- Assert `market=None` returns all positions.

**Fake service alignment (existing):** Update `_FakePaperService.get_positions`
in `test_paper_portfolio_handler.py` and `test_mcp_portfolio_tools.py` to
match the real service signature and apply the `market` filter, so signature
drift is caught by existing unit tests.

## Acceptance Criteria

- `account=paper` holdings queries work without argument errors.
- `market` filtering returns correct subset for `equity_kr`, `equity_us`, `crypto`.
- Integration test exercises real `PaperTradingService` + DB.
- Fake services mirror real service signature.
