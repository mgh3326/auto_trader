# n8n Pending Orders REST Endpoint Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `GET /api/n8n/pending-orders` that returns pending/partial orders with current prices, KRW amounts, and age calculations for n8n workflow consumption.

**Architecture:** Thin router + typed Pydantic schema + dedicated service. The service calls `get_order_history_impl()` per-market (fan-out for `market=all`), tags each batch with the market, normalizes timestamps to KST ISO8601, enriches with current prices (crypto=batch Upbit, KR/US=`get_quote()` with Semaphore(5)), converts US amounts to KRW via a new cached exchange rate service, and computes gap_pct/age/summary.

**Tech Stack:** FastAPI, Pydantic v2, asyncio (gather + Semaphore), httpx, existing `get_order_history_impl()`, `app.services.market_data.get_quote()`, `fetch_multiple_current_prices_cached()`

---

## Task 1: Exchange Rate Service

**Files:**
- Create: `app/services/exchange_rate_service.py`
- Test: `tests/test_n8n_api.py` (exchange rate unit tests section)

**Step 1: Write the failing test**

Create `tests/test_n8n_api.py` with exchange rate tests only:

```python
"""Tests for n8n pending orders API and supporting services."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Exchange Rate Service Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestExchangeRateService:
    """Tests for app.services.exchange_rate_service."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """Reset module-level cache between tests."""
        from app.services import exchange_rate_service as mod

        mod._cache.update({"rate": None, "expires_at": 0.0})
        yield
        mod._cache.update({"rate": None, "expires_at": 0.0})

    @pytest.mark.asyncio
    async def test_fetches_rate_on_cache_miss(self):
        from app.services.exchange_rate_service import get_usd_krw_rate

        mock_response = AsyncMock()
        mock_response.json.return_value = {"rates": {"KRW": 1350.0}}
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.exchange_rate_service.httpx.AsyncClient", return_value=mock_client):
            rate = await get_usd_krw_rate()

        assert rate == 1350.0
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_cached_rate_on_hit(self):
        from app.services import exchange_rate_service as mod
        from app.services.exchange_rate_service import get_usd_krw_rate

        mod._cache.update({"rate": 1400.0, "expires_at": time.monotonic() + 300})

        with patch("app.services.exchange_rate_service.httpx.AsyncClient") as mock_cls:
            rate = await get_usd_krw_rate()

        assert rate == 1400.0
        mock_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_refetches_after_ttl_expires(self):
        from app.services import exchange_rate_service as mod
        from app.services.exchange_rate_service import get_usd_krw_rate

        mod._cache.update({"rate": 1400.0, "expires_at": time.monotonic() - 1})

        mock_response = AsyncMock()
        mock_response.json.return_value = {"rates": {"KRW": 1450.0}}
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.exchange_rate_service.httpx.AsyncClient", return_value=mock_client):
            rate = await get_usd_krw_rate()

        assert rate == 1450.0
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_n8n_api.py::TestExchangeRateService -v`
Expected: FAIL — module not found

**Step 3: Write minimal implementation**

Create `app/services/exchange_rate_service.py`:

```python
"""USD/KRW exchange rate service with module-level TTL cache."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
_TTL_SECONDS = 300.0  # 5 minutes

_cache: dict[str, float | None] = {"rate": None, "expires_at": 0.0}
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_lock() -> asyncio.Lock:
    """Get or create an event-loop-safe asyncio.Lock."""
    global _lock, _lock_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


async def get_usd_krw_rate() -> float:
    """Return cached USD/KRW exchange rate, fetching from API on cache miss."""
    now = time.monotonic()
    cached_rate = _cache["rate"]
    if cached_rate is not None and _cache["expires_at"] > now:
        return cached_rate

    lock = _get_lock()
    async with lock:
        # Double-check after acquiring lock
        now = time.monotonic()
        cached_rate = _cache["rate"]
        if cached_rate is not None and _cache["expires_at"] > now:
            return cached_rate

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(_EXCHANGE_RATE_URL)
            response.raise_for_status()
            rate = float(response.json()["rates"]["KRW"])

        _cache["rate"] = rate
        _cache["expires_at"] = time.monotonic() + _TTL_SECONDS
        logger.debug("Fetched USD/KRW rate: %.2f (TTL=%ds)", rate, _TTL_SECONDS)
        return rate
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_n8n_api.py::TestExchangeRateService -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add app/services/exchange_rate_service.py tests/test_n8n_api.py
git commit -m "feat(n8n): add USD/KRW exchange rate service with TTL cache"
```

---

## Task 2: Pydantic Response Schemas

**Files:**
- Create: `app/schemas/n8n.py`
- Modify: `app/schemas/__init__.py`

**Step 1: Create response schemas**

Create `app/schemas/n8n.py`:

```python
"""Pydantic response models for n8n integration endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class N8nPendingOrderItem(BaseModel):
    """Single pending order in n8n format."""

    order_id: str = Field(description="Unique order identifier")
    symbol: str = Field(description="Normalized symbol (crypto prefix stripped)")
    raw_symbol: str = Field(description="Original symbol from broker")
    market: str = Field(description="Market: crypto, kr, or us")
    side: str = Field(description="buy or sell")
    status: str = Field(description="pending or partial")
    order_price: float = Field(description="Order price")
    current_price: float | None = Field(None, description="Current market price (null if unavailable)")
    gap_pct: float | None = Field(None, description="Gap between order and current price in percent")
    amount_krw: float = Field(description="Estimated KRW amount (order_price * remaining_qty, converted for US)")
    quantity: float = Field(description="Originally ordered quantity")
    remaining_qty: float = Field(description="Remaining unfilled quantity")
    created_at: str = Field(description="Order creation time in KST ISO8601")
    age_hours: int = Field(description="Hours since order creation (floored)")
    age_days: int = Field(description="Days since order creation (age_hours // 24)")
    currency: str = Field(description="KRW or USD")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "order_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "symbol": "BTC",
                "raw_symbol": "KRW-BTC",
                "market": "crypto",
                "side": "buy",
                "status": "pending",
                "order_price": 95000000.0,
                "current_price": 96500000.0,
                "gap_pct": 1.58,
                "amount_krw": 95000000.0,
                "quantity": 1.0,
                "remaining_qty": 1.0,
                "created_at": "2026-03-15T10:30:00+09:00",
                "age_hours": 5,
                "age_days": 0,
                "currency": "KRW",
            }
        },
    )


class N8nPendingOrderSummary(BaseModel):
    """Aggregate summary of filtered pending orders."""

    total: int = Field(description="Total number of orders after filtering")
    buy_count: int = Field(description="Number of buy orders")
    sell_count: int = Field(description="Number of sell orders")
    total_buy_krw: float = Field(description="Sum of amount_krw for buy orders")
    total_sell_krw: float = Field(description="Sum of amount_krw for sell orders")


class N8nPendingOrdersResponse(BaseModel):
    """Top-level response for GET /api/n8n/pending-orders."""

    success: bool = Field(description="True if at least one market succeeded")
    as_of: str = Field(description="Response timestamp in KST ISO8601")
    market: str = Field(description="Requested market filter (echoed back)")
    orders: list[N8nPendingOrderItem] = Field(description="Filtered and enriched pending orders")
    summary: N8nPendingOrderSummary = Field(description="Aggregate summary")
    errors: list[dict] = Field(default_factory=list, description="Per-market or per-symbol errors")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "as_of": "2026-03-15T15:30:00+09:00",
                "market": "all",
                "orders": [],
                "summary": {
                    "total": 0,
                    "buy_count": 0,
                    "sell_count": 0,
                    "total_buy_krw": 0.0,
                    "total_sell_krw": 0.0,
                },
                "errors": [],
            }
        },
    )
```

**Step 2: Update schemas __init__.py**

Add n8n schema exports to `app/schemas/__init__.py`.

**Step 3: Commit**

```bash
git add app/schemas/n8n.py app/schemas/__init__.py
git commit -m "feat(n8n): add Pydantic response schemas for pending orders endpoint"
```

---

## Task 3: n8n Pending Orders Service

**Files:**
- Create: `app/services/n8n_pending_orders_service.py`
- Test: `tests/test_n8n_api.py` (service tests section)

This is the main business logic. Key behaviors:
- Fan-out per market (3 calls for `market=all`, 1 call for specific market)
- Tag each order batch with market
- Normalize timestamps to KST ISO8601
- Enrich with current prices (crypto=batch, KR/US=get_quote with Semaphore(5))
- Convert US amounts to KRW
- Filter by min_amount, compute gap_pct/age, sort by created_at asc

**Step 1: Write failing tests for the service**

Add to `tests/test_n8n_api.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

KST = timezone(timedelta(hours=9))


def _make_crypto_order(
    *,
    symbol: str = "KRW-BTC",
    side: str = "buy",
    status: str = "pending",
    ordered_price: float = 95_000_000.0,
    ordered_qty: float = 0.01,
    remaining_qty: float = 0.01,
    ordered_at: str = "2026-03-15T10:00:00+09:00",
) -> dict:
    return {
        "order_id": "uuid-crypto-1",
        "symbol": symbol,
        "side": side,
        "status": status,
        "ordered_qty": ordered_qty,
        "filled_qty": 0.0,
        "remaining_qty": remaining_qty,
        "ordered_price": ordered_price,
        "filled_avg_price": 0.0,
        "ordered_at": ordered_at,
        "filled_at": "",
        "currency": "KRW",
    }


def _make_kr_order(
    *,
    symbol: str = "005930",
    side: str = "buy",
    status: str = "pending",
    ordered_price: int = 70000,
    ordered_qty: int = 10,
    remaining_qty: int = 10,
    ordered_at: str = "20260315 100000",
) -> dict:
    return {
        "order_id": "KR-001",
        "symbol": symbol,
        "side": side,
        "status": status,
        "ordered_qty": ordered_qty,
        "filled_qty": 0,
        "remaining_qty": remaining_qty,
        "ordered_price": ordered_price,
        "filled_avg_price": 0,
        "ordered_at": ordered_at,
        "filled_at": "",
        "currency": "KRW",
    }


def _make_us_order(
    *,
    symbol: str = "AAPL",
    side: str = "buy",
    status: str = "pending",
    ordered_price: float = 180.50,
    ordered_qty: int = 5,
    remaining_qty: int = 5,
    ordered_at: str = "20260315 090000",
) -> dict:
    return {
        "order_id": "US-001",
        "symbol": symbol,
        "side": side,
        "status": status,
        "ordered_qty": ordered_qty,
        "filled_qty": 0,
        "remaining_qty": remaining_qty,
        "ordered_price": ordered_price,
        "filled_avg_price": 0.0,
        "ordered_at": ordered_at,
        "filled_at": "",
        "currency": "USD",
    }


def _impl_result(*, orders: list[dict], market: str = "crypto", errors: list | None = None) -> dict:
    """Build a mock get_order_history_impl return value."""
    return {
        "success": True,
        "symbol": None,
        "market": market,
        "status": "pending",
        "filters": {},
        "orders": orders,
        "summary": {"total_orders": len(orders), "filled": 0, "pending": len(orders), "partial": 0, "cancelled": 0},
        "truncated": False,
        "total_available": len(orders),
        "errors": errors or [],
    }


@pytest.mark.unit
class TestN8nPendingOrdersService:
    """Tests for app.services.n8n_pending_orders_service."""

    @pytest.mark.asyncio
    async def test_market_all_fans_out_three_calls(self):
        """market=all should call get_order_history_impl 3 times."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
        ) as mock_impl:
            mock_impl.return_value = _impl_result(orders=[])

            result = await fetch_pending_orders(market="all", include_current_price=False)

        assert mock_impl.call_count == 3
        markets_called = sorted(call.kwargs.get("market") or call.args[0] for call in mock_impl.call_args_list)
        # Extract market kwarg from each call
        markets_called = sorted(c.kwargs["market"] for c in mock_impl.call_args_list)
        assert markets_called == ["crypto", "kr", "us"]

    @pytest.mark.asyncio
    async def test_market_specific_single_call(self):
        """market=kr should call get_order_history_impl once with market='kr'."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
        ) as mock_impl:
            mock_impl.return_value = _impl_result(orders=[], market="kr")

            result = await fetch_pending_orders(market="kr", include_current_price=False)

        mock_impl.assert_called_once()
        assert mock_impl.call_args.kwargs["market"] == "kr"

    @pytest.mark.asyncio
    async def test_crypto_symbol_stripping(self):
        """Crypto orders should have prefix stripped from symbol but kept in raw_symbol."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_crypto_order()]),
        ):
            result = await fetch_pending_orders(market="crypto", include_current_price=False)

        order = result["orders"][0]
        assert order["raw_symbol"] == "KRW-BTC"
        assert order["symbol"] == "BTC"

    @pytest.mark.asyncio
    async def test_kr_symbol_preserved(self):
        """KR orders should keep symbol as-is."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_kr_order()], market="kr"),
        ):
            result = await fetch_pending_orders(market="kr", include_current_price=False)

        assert result["orders"][0]["symbol"] == "005930"
        assert result["orders"][0]["raw_symbol"] == "005930"

    @pytest.mark.asyncio
    async def test_created_at_kis_format_normalized_to_kst_iso(self):
        """KIS 'YYYYMMDD HHMMSS' timestamps should become KST ISO8601."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_kr_order(ordered_at="20260315 143000")], market="kr"),
        ):
            result = await fetch_pending_orders(market="kr", include_current_price=False)

        created = result["orders"][0]["created_at"]
        assert "+09:00" in created
        assert "2026-03-15T14:30:00" in created

    @pytest.mark.asyncio
    async def test_orders_sorted_by_created_at_ascending(self):
        """Orders should be sorted by normalized created_at ascending (oldest first)."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        orders = [
            _make_kr_order(ordered_at="20260315 140000", symbol="005930"),
            _make_kr_order(ordered_at="20260315 100000", symbol="000660"),
        ]

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=orders, market="kr"),
        ):
            result = await fetch_pending_orders(market="kr", include_current_price=False)

        symbols = [o["raw_symbol"] for o in result["orders"]]
        assert symbols == ["000660", "005930"]  # 10:00 before 14:00

    @pytest.mark.asyncio
    async def test_include_current_price_false_skips_quotes(self):
        """include_current_price=False should skip price enrichment."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_crypto_order()]),
        ), patch(
            "app.services.n8n_pending_orders_service.fetch_multiple_current_prices_cached",
        ) as mock_prices:
            result = await fetch_pending_orders(market="crypto", include_current_price=False)

        mock_prices.assert_not_called()
        assert result["orders"][0]["current_price"] is None
        assert result["orders"][0]["gap_pct"] is None

    @pytest.mark.asyncio
    async def test_gap_pct_calculation(self):
        """gap_pct = round((current - order) / order * 100, 2)."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_crypto_order(ordered_price=100_000_000.0)]),
        ), patch(
            "app.services.n8n_pending_orders_service.fetch_multiple_current_prices_cached",
            new_callable=AsyncMock,
            return_value={"KRW-BTC": 105_000_000.0},
        ):
            result = await fetch_pending_orders(market="crypto", include_current_price=True)

        assert result["orders"][0]["gap_pct"] == 5.0

    @pytest.mark.asyncio
    async def test_age_hours_and_days(self):
        """age_hours = floor(delta), age_days = age_hours // 24."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        two_days_ago = (datetime.now(KST) - timedelta(hours=50)).strftime("%Y-%m-%dT%H:%M:%S+09:00")

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_crypto_order(ordered_at=two_days_ago)]),
        ):
            result = await fetch_pending_orders(market="crypto", include_current_price=False)

        order = result["orders"][0]
        assert order["age_hours"] >= 49  # ~50 hours, floored
        assert order["age_days"] == order["age_hours"] // 24

    @pytest.mark.asyncio
    async def test_min_amount_filter(self):
        """Orders below min_amount (in KRW) should be excluded."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        orders = [
            _make_kr_order(ordered_price=1000, ordered_qty=1, remaining_qty=1),  # 1000 KRW
            _make_kr_order(ordered_price=100000, ordered_qty=10, remaining_qty=10, symbol="000660"),  # 1M KRW
        ]
        orders[1]["order_id"] = "KR-002"

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=orders, market="kr"),
        ):
            result = await fetch_pending_orders(market="kr", include_current_price=False, min_amount=10000)

        assert len(result["orders"]) == 1
        assert result["orders"][0]["raw_symbol"] == "000660"

    @pytest.mark.asyncio
    async def test_us_amount_krw_uses_exchange_rate(self):
        """US order amount_krw should be order_price * remaining_qty * usd_krw_rate."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_us_order(ordered_price=100.0, ordered_qty=2, remaining_qty=2)], market="us"),
        ), patch(
            "app.services.n8n_pending_orders_service.get_usd_krw_rate",
            new_callable=AsyncMock,
            return_value=1400.0,
        ), patch(
            "app.services.n8n_pending_orders_service.get_quote",
            new_callable=AsyncMock,
            side_effect=Exception("skip"),
        ):
            result = await fetch_pending_orders(market="us", include_current_price=True)

        assert result["orders"][0]["amount_krw"] == 100.0 * 2 * 1400.0

    @pytest.mark.asyncio
    async def test_quote_failure_preserves_order_with_null_price(self):
        """Quote fetch failure should keep the order with null price fields and add error."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_kr_order()], market="kr"),
        ), patch(
            "app.services.n8n_pending_orders_service.get_quote",
            new_callable=AsyncMock,
            side_effect=Exception("KIS API timeout"),
        ):
            result = await fetch_pending_orders(market="kr", include_current_price=True)

        assert len(result["orders"]) == 1
        assert result["orders"][0]["current_price"] is None
        assert result["orders"][0]["gap_pct"] is None
        assert len(result["errors"]) >= 1

    @pytest.mark.asyncio
    async def test_summary_aggregation(self):
        """Summary should count buy/sell and sum KRW amounts."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        orders = [
            _make_kr_order(side="buy", ordered_price=10000, remaining_qty=5),
            _make_kr_order(side="sell", ordered_price=20000, remaining_qty=3, symbol="000660"),
        ]
        orders[1]["order_id"] = "KR-002"

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=orders, market="kr"),
        ):
            result = await fetch_pending_orders(market="kr", include_current_price=False)

        summary = result["summary"]
        assert summary["total"] == 2
        assert summary["buy_count"] == 1
        assert summary["sell_count"] == 1
        assert summary["total_buy_krw"] == 50000.0
        assert summary["total_sell_krw"] == 60000.0

    @pytest.mark.asyncio
    async def test_side_filter_passthrough(self):
        """side parameter should be passed through to get_order_history_impl."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[]),
        ) as mock_impl:
            await fetch_pending_orders(market="crypto", side="buy", include_current_price=False)

        assert mock_impl.call_args.kwargs["side"] == "buy"

    @pytest.mark.asyncio
    async def test_partial_status_preserved(self):
        """Orders with status='partial' from upstream should keep that status."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        order = _make_crypto_order(status="partial")

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[order]),
        ):
            result = await fetch_pending_orders(market="crypto", include_current_price=False)

        assert result["orders"][0]["status"] == "partial"

    @pytest.mark.asyncio
    async def test_market_field_on_each_order(self):
        """Each order should have a market field set by the batch tag."""
        from app.services.n8n_pending_orders_service import fetch_pending_orders

        with patch(
            "app.services.n8n_pending_orders_service.get_order_history_impl",
            new_callable=AsyncMock,
            return_value=_impl_result(orders=[_make_crypto_order()]),
        ):
            result = await fetch_pending_orders(market="crypto", include_current_price=False)

        assert result["orders"][0]["market"] == "crypto"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_n8n_api.py::TestN8nPendingOrdersService -v`
Expected: FAIL — module not found

**Step 3: Write the service implementation**

Create `app/services/n8n_pending_orders_service.py`. This is the core service that:

1. Calls `get_order_history_impl()` per market (fan-out for "all")
2. Tags each order with `_market`
3. Normalizes `created_at` to KST ISO8601
4. Enriches with current prices (crypto=batch, KR/US=`get_quote()` + Semaphore)
5. Computes `amount_krw`, `gap_pct`, `age_hours`, `age_days`
6. Filters by `min_amount`
7. Sorts by `created_at` ascending
8. Builds summary

Key imports:
- `app.mcp_server.tooling.orders_history.get_order_history_impl`
- `app.services.market_data.get_quote`
- `app.services.brokers.upbit.client.fetch_multiple_current_prices_cached`
- `app.services.exchange_rate_service.get_usd_krw_rate`
- `app.core.timezone.now_kst, KST`

Key implementation details:
- Crypto symbol → detect by `KRW-`/`USDT-` prefix → strip for `symbol`, keep for `raw_symbol`
- KIS timestamp `"YYYYMMDD HHMMSS"` → `datetime.strptime(val, "%Y%m%d %H%M%S").replace(tzinfo=KST).isoformat()`
- Upbit ISO timestamp → `datetime.fromisoformat(val).astimezone(KST).isoformat()`
- `get_order_history_impl` errors have `market` in internal form (`equity_kr`) → map to external (`kr`)
- US `amount_krw = order_price * remaining_qty * usd_krw_rate`
- Call `get_usd_krw_rate()` once at the top if any US orders exist
- Semaphore(5) for KR/US quote calls via asyncio.gather

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_n8n_api.py::TestN8nPendingOrdersService -v`
Expected: PASS (all service tests)

**Step 5: Commit**

```bash
git add app/services/n8n_pending_orders_service.py tests/test_n8n_api.py
git commit -m "feat(n8n): add pending orders service with fan-out, enrichment, and KRW conversion"
```

---

## Task 4: Router and App Wiring

**Files:**
- Create: `app/routers/n8n.py`
- Modify: `app/main.py` (lines 20-36 imports, lines 128-148 include_router)
- Modify: `app/middleware/auth.py` (line 44-47 PUBLIC_API_PATHS)

**Step 1: Write failing endpoint test**

Add to `tests/test_n8n_api.py`:

```python
from fastapi.testclient import TestClient


@pytest.mark.unit
class TestN8nPendingOrdersEndpoint:
    """Integration tests for GET /api/n8n/pending-orders."""

    @pytest.fixture
    def client(self):
        """Create test client with n8n router."""
        from fastapi import FastAPI
        from app.routers.n8n import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    @pytest.mark.asyncio
    async def test_default_params_returns_valid_schema(self, client):
        with patch(
            "app.routers.n8n.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={
                "orders": [],
                "summary": {"total": 0, "buy_count": 0, "sell_count": 0, "total_buy_krw": 0.0, "total_sell_krw": 0.0},
                "errors": [],
            },
        ):
            response = client.get("/api/n8n/pending-orders")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "as_of" in data
        assert data["market"] == "all"
        assert isinstance(data["orders"], list)
        assert isinstance(data["summary"], dict)
        assert isinstance(data["errors"], list)

    @pytest.mark.asyncio
    async def test_market_param_passed_to_service(self, client):
        with patch(
            "app.routers.n8n.fetch_pending_orders",
            new_callable=AsyncMock,
            return_value={"orders": [], "summary": {"total": 0, "buy_count": 0, "sell_count": 0, "total_buy_krw": 0.0, "total_sell_krw": 0.0}, "errors": []},
        ) as mock_svc:
            client.get("/api/n8n/pending-orders?market=crypto")

        assert mock_svc.call_args.kwargs["market"] == "crypto"

    @pytest.mark.asyncio
    async def test_service_exception_returns_500(self, client):
        with patch(
            "app.routers.n8n.fetch_pending_orders",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            response = client.get("/api/n8n/pending-orders")

        assert response.status_code == 500
        data = response.json()
        assert data["success"] is False
        assert "boom" in data["errors"][0]["error"]
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_n8n_api.py::TestN8nPendingOrdersEndpoint -v`
Expected: FAIL — router not found

**Step 3: Create router**

Create `app/routers/n8n.py`:

```python
"""n8n integration endpoints."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query

from app.core.timezone import now_kst
from app.schemas.n8n import N8nPendingOrdersResponse, N8nPendingOrderSummary
from app.services.n8n_pending_orders_service import fetch_pending_orders

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/n8n", tags=["n8n"])


@router.get("/pending-orders", response_model=N8nPendingOrdersResponse)
async def get_pending_orders(
    market: Literal["crypto", "kr", "us", "all"] = Query("all", description="Market filter"),
    min_amount: float = Query(0, ge=0, description="Minimum KRW amount filter"),
    include_current_price: bool = Query(True, description="Fetch current prices and compute gap_pct"),
    side: Literal["buy", "sell"] | None = Query(None, description="Order side filter"),
) -> N8nPendingOrdersResponse:
    """Return pending orders enriched with current prices for n8n consumption."""
    as_of = now_kst().replace(microsecond=0).isoformat()

    try:
        result = await fetch_pending_orders(
            market=market,
            min_amount=min_amount,
            include_current_price=include_current_price,
            side=side,
        )
        return N8nPendingOrdersResponse(
            success=True,
            as_of=as_of,
            market=market,
            orders=result["orders"],
            summary=N8nPendingOrderSummary(**result["summary"]),
            errors=result["errors"],
        )
    except Exception:
        logger.exception("n8n pending-orders failed")
        return N8nPendingOrdersResponse(
            success=False,
            as_of=as_of,
            market=market,
            orders=[],
            summary=N8nPendingOrderSummary(
                total=0, buy_count=0, sell_count=0, total_buy_krw=0.0, total_sell_krw=0.0,
            ),
            errors=[{"market": market, "error": str(Exception)}],
        )
```

**Step 4: Wire into app**

Modify `app/main.py`:
- Add `n8n,` to the `from app.routers import (...)` block (after `news_analysis,`)
- Add `app.include_router(n8n.router)` after the `kospi200.router` line

Modify `app/middleware/auth.py`:
- Add `"/api/n8n/pending-orders",` to `PUBLIC_API_PATHS` list

**Step 5: Run tests**

Run: `uv run pytest tests/test_n8n_api.py -v`
Expected: PASS (all tests)

**Step 6: Commit**

```bash
git add app/routers/n8n.py app/main.py app/middleware/auth.py
git commit -m "feat(n8n): add pending-orders router with app wiring and auth exception"
```

---

## Task 5: Auth Middleware Tests

**Files:**
- Modify: `tests/test_auth_middleware.py`

**Step 1: Add auth allowlist tests**

Add to `tests/test_auth_middleware.py`:

```python
def test_n8n_pending_orders_is_public(client, mock_session_local):
    """The n8n pending-orders endpoint should be accessible without auth."""
    # We register a dummy route on the test app to simulate the n8n endpoint
    from app.middleware.auth import AuthMiddleware

    assert "/api/n8n/pending-orders" in AuthMiddleware.PUBLIC_API_PATHS
```

**Step 2: Run to verify pass**

Run: `uv run pytest tests/test_auth_middleware.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_auth_middleware.py
git commit -m "test(n8n): add auth middleware allowlist verification for pending-orders"
```

---

## Task 6: Full Integration Smoke Test

**Step 1: Run the complete test suite**

Run: `uv run pytest tests/test_n8n_api.py -v`
Expected: All tests PASS

**Step 2: Run lint**

Run: `make lint`
Expected: PASS

**Step 3: Run existing tests to check for regressions**

Run: `make test`
Expected: PASS (no regressions)

**Step 4: Final commit if any fixes needed**

---

## Reference: Key Source Files

| Function | File | Usage |
|----------|------|-------|
| `get_order_history_impl()` | `app/mcp_server/tooling/orders_history.py:56` | Accepts `market="crypto"\|"kr"\|"us"`, `status="pending"`, `side`, `limit=-1` |
| `get_quote()` | `app/services/market_data/service.py:298` | `get_quote(symbol, "kr"\|"us"\|"crypto")` → `Quote` dataclass |
| `fetch_multiple_current_prices_cached()` | `app/services/brokers/upbit/client.py:688` | Batch crypto prices with TTL cache |
| `now_kst()` / `KST` | `app/core/timezone.py:14` / `:11` | KST-aware datetime |
| `normalize_market()` (MCP) | `app/mcp_server/tooling/shared.py:94` | Maps `"kr"→"equity_kr"`, `"us"→"equity_us"`, `"crypto"→"crypto"` |
| Internal→external market | `app/mcp_server/tooling/order_execution.py:44` | `"equity_kr"→"kr"`, `"equity_us"→"us"` |
| Upbit order normalization | `app/mcp_server/tooling/orders_modify_cancel.py:36` | Returns `ordered_at` as ISO string |
| KIS order normalization | `app/mcp_server/tooling/orders_modify_cancel.py:116` | Returns `ordered_at` as `"YYYYMMDD HHMMSS"` |
| Semaphore pattern | `app/mcp_server/tooling/analysis_recommend.py:120` | `asyncio.Semaphore(5)` + `asyncio.gather()` |
| Loop-safe Lock | `app/services/brokers/upbit/client.py:44` | `_get_ticker_cache_lock()` pattern |
