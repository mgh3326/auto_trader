# Daily Brief Latency Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove duplicated downstream work inside `GET /api/n8n/daily-brief` while preserving the existing response schema and router contract.

**Architecture:** Refactor `fetch_daily_brief()` into a two-stage orchestrator. Stage 1 collects shared request inputs once (`pending_orders` and `portfolio_overview`) and derives `symbols_by_market`. Stage 2 reuses those shared symbols for `fetch_market_context()` and `_fetch_yesterday_fills()` so they stop re-fetching pending orders and portfolio data. Keep partial-failure semantics and response payload shape unchanged.

**Tech Stack:** FastAPI service layer, asyncio, existing n8n services, pytest, unittest.mock.AsyncMock.

---

### Task 1: Add Regression Tests For Shared Input Reuse

**Files:**
- Modify: `tests/test_n8n_daily_brief_service.py:12-163`
- Modify: `app/services/n8n_daily_brief_service.py:407-525`

**Step 1: Write the failing tests**

Add tests to `tests/test_n8n_daily_brief_service.py` for these cases:

```python
    @pytest.mark.asyncio
    async def test_daily_brief_passes_explicit_crypto_symbols_to_market_context(self):
        pending = _fake_pending_result(
            "all",
            orders=[
                {"market": "crypto", "symbol": "BTC", "raw_symbol": "KRW-BTC"},
                {"market": "kr", "symbol": "005930", "raw_symbol": "005930"},
            ],
        )
        portfolio = {
            "success": True,
            "positions": [
                {"market_type": "CRYPTO", "symbol": "KRW-ETH", "name": "ETH"},
            ],
            "warnings": [],
        }

        with (
            patch(
                "app.services.n8n_daily_brief_service.fetch_pending_orders",
                new_callable=AsyncMock,
                return_value=pending,
            ) as mock_pending,
            patch(
                "app.services.n8n_daily_brief_service._get_portfolio_overview",
                new_callable=AsyncMock,
                return_value=portfolio,
            ),
            patch(
                "app.services.n8n_daily_brief_service.fetch_market_context",
                new_callable=AsyncMock,
                return_value=_fake_market_context(),
            ) as mock_context,
            patch(
                "app.services.n8n_daily_brief_service._fetch_yesterday_fills",
                new_callable=AsyncMock,
                return_value={"total": 0, "fills": []},
            ),
        ):
            from app.services.n8n_daily_brief_service import fetch_daily_brief

            await fetch_daily_brief(markets=["crypto", "kr"])

        assert mock_pending.await_count == 1
        assert mock_pending.await_args.kwargs["include_indicators"] is False
        assert mock_context.await_args.kwargs["symbols"] == ["BTC", "ETH"]

    @pytest.mark.asyncio
    async def test_daily_brief_passes_shared_symbols_to_yesterday_fills(self):
        pending = _fake_pending_result(
            "all",
            orders=[
                {"market": "crypto", "symbol": "BTC", "raw_symbol": "KRW-BTC"},
                {"market": "us", "symbol": "NVDA", "raw_symbol": "NVDA"},
            ],
        )
        portfolio = {
            "success": True,
            "positions": [
                {"market_type": "KR", "symbol": "005930", "name": "Samsung"},
            ],
            "warnings": [],
        }

        with (
            patch(
                "app.services.n8n_daily_brief_service.fetch_pending_orders",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.n8n_daily_brief_service._get_portfolio_overview",
                new_callable=AsyncMock,
                return_value=portfolio,
            ),
            patch(
                "app.services.n8n_daily_brief_service.fetch_market_context",
                new_callable=AsyncMock,
                return_value=_fake_market_context(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._fetch_yesterday_fills",
                new_callable=AsyncMock,
                return_value={"total": 0, "fills": []},
            ) as mock_fills,
        ):
            from app.services.n8n_daily_brief_service import fetch_daily_brief

            await fetch_daily_brief(markets=["crypto", "kr", "us"])

        assert mock_fills.await_args.kwargs["symbols_by_market"] == {
            "crypto": {"KRW-BTC", "KRW-ETH"},
            "kr": {"005930"},
            "us": {"NVDA"},
        }
```

Use deterministic assertions rather than timing assertions.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_n8n_daily_brief_service.py -v`

Expected: FAIL because `fetch_daily_brief()` still calls `fetch_market_context(symbols=None)`, still allows indicator enrichment in `fetch_pending_orders()`, and `_fetch_yesterday_fills()` still has the old signature.

**Step 3: Commit**

```bash
git add tests/test_n8n_daily_brief_service.py
git commit -m "test(daily-brief): add shared input reuse regressions"
```

### Task 2: Derive Shared Symbol Context In Daily Brief Service

**Files:**
- Modify: `app/services/n8n_daily_brief_service.py:32-161`
- Modify: `app/services/n8n_daily_brief_service.py:407-525`
- Test: `tests/test_n8n_daily_brief_service.py`

**Step 1: Write the helper implementation**

Add a helper that derives a shared symbol map from pending orders and portfolio positions.

Suggested shape:

```python
def _collect_symbols_by_market(
    pending_result: dict[str, Any],
    portfolio_result: dict[str, Any],
) -> dict[str, set[str]]:
    symbols_by_market: dict[str, set[str]] = {}

    for order in pending_result.get("orders", []):
        market = str(order.get("market") or "").strip()
        raw_symbol = str(order.get("raw_symbol") or order.get("symbol") or "").strip()
        if market and raw_symbol:
            symbols_by_market.setdefault(market, set()).add(raw_symbol)

    market_map = {"CRYPTO": "crypto", "KR": "kr", "US": "us"}
    for position in portfolio_result.get("positions", []):
        market = market_map.get(str(position.get("market_type") or "").upper())
        symbol = str(position.get("symbol") or "").strip()
        if not market or not symbol:
            continue
        if market == "crypto" and "-" not in symbol:
            symbol = f"KRW-{symbol.upper()}"
        symbols_by_market.setdefault(market, set()).add(symbol)

    return symbols_by_market
```

**Step 2: Change `_fetch_yesterday_fills()` to accept shared symbols**

Update the function signature and remove its internal calls to `fetch_pending_orders()` and `_get_portfolio_overview()`.

Target shape:

```python
async def _fetch_yesterday_fills(
    *,
    markets: list[str],
    symbols_by_market: dict[str, set[str]],
) -> dict[str, Any]:
    ...
```

Only iterate the provided symbols and keep the current normalization/output format behavior.

**Step 3: Refactor `fetch_daily_brief()` orchestration**

Change `fetch_daily_brief()` so it:

1. gathers `pending_orders` and `portfolio_overview` first
2. calls `fetch_pending_orders(... include_indicators=False, ...)`
3. derives `symbols_by_market`
4. derives `crypto_symbols` from `symbols_by_market["crypto"]`
5. calls `fetch_market_context(symbols=crypto_symbols or ["BTC"], ...)`
6. calls `_fetch_yesterday_fills(markets=effective_markets, symbols_by_market=symbols_by_market)`
7. preserves `return_exceptions=True` and current error accumulation

Do not change the returned response keys or nested schema.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_n8n_daily_brief_service.py -v`

Expected: PASS

**Step 5: Commit**

```bash
git add app/services/n8n_daily_brief_service.py tests/test_n8n_daily_brief_service.py
git commit -m "refactor(daily-brief): reuse shared inputs across subqueries"
```

### Task 3: Verify Endpoint Contract And Regression Surface

**Files:**
- Test: `tests/test_n8n_daily_brief_api.py`
- Test: `tests/test_n8n_daily_brief_service.py`

**Step 1: Run targeted service and API tests**

Run:

```bash
uv run pytest \
  tests/test_n8n_daily_brief_service.py \
  tests/test_n8n_daily_brief_api.py \
  -v
```

Expected: PASS

**Step 2: Run adjacent regression tests that touch shared services**

Run:

```bash
uv run pytest \
  tests/test_n8n_market_context.py \
  -v
```

Expected: PASS

This confirms the `market-context` endpoint still behaves the same when called directly.

**Step 3: Optional local runtime smoke test**

If the runtime environment is available, run:

```bash
uv run python - <<'PY'
import asyncio
from app.services.n8n_daily_brief_service import fetch_daily_brief

async def main():
    result = await fetch_daily_brief(markets=["crypto", "kr", "us"])
    print(result["success"], sorted(result["pending_orders"].keys()))

asyncio.run(main())
PY
```

Expected: prints `True` and the unchanged market keys without schema errors.

**Step 4: Commit**

```bash
git add tests/test_n8n_daily_brief_service.py tests/test_n8n_daily_brief_api.py
git commit -m "test(daily-brief): verify contract after latency refactor"
```
