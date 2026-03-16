# Intraday Order Review (장중 미체결 리뷰) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add intraday order review capability to detect significant changes in pending order fill probability and selectively notify only when orders need attention.

**Architecture:** Extend the existing `/api/n8n/pending-orders` endpoint with new fields (`fill_proximity`, `needs_attention`, `attention_reason`) and parameters (`attention_only`, `near_fill_pct`). Integrate with market-context service to fetch RSI and 24h change data for attention detection. Use background TaskIQ tasks for scheduled intraday checks per market schedule.

**Tech Stack:** FastAPI, Pydantic, TaskIQ, Python 3.13+

---

## Current State Analysis

**Existing Files:**
- `app/routers/n8n.py` — Router with `/pending-orders` and `/market-context` endpoints
- `app/services/n8n_pending_orders_service.py` — `fetch_pending_orders()` service function
- `app/services/n8n_market_context_service.py` — `fetch_market_context()` with RSI/24h data
- `app/services/n8n_formatting.py` — Formatting utilities
- `app/schemas/n8n.py` — Pydantic schemas for responses

**Key Integration Points:**
1. Pending orders service already fetches current prices and calculates `gap_pct`
2. Market context service already provides RSI and 24h change data via `N8nSymbolContext`
3. The router already wires everything together

---

## Task 1: Update Pydantic Schemas

**Files:**
- Modify: `app/schemas/n8n.py`

**Step 1: Add new fields to `N8nPendingOrderItem`**

```python
# After line 52 (after age_fmt field), add:
fill_proximity: str | None = Field(
    None,
    description="Fill proximity classification: near, moderate, far, very_far"
)
fill_proximity_fmt: str | None = Field(
    None,
    description="Formatted fill proximity, e.g. '체결 임박 ⚡'"
)
needs_attention: bool = Field(
    False,
    description="Whether this order needs user attention"
)
attention_reason: str | None = Field(
    None,
    description="Human-readable reason for attention"
)
```

**Step 2: Add new fields to `N8nPendingOrderSummary`**

```python
# After line 100 (after title field), add:
near_fill_count: int = Field(
    0,
    description="Number of orders near fill (within near_fill_pct)"
)
needs_attention_count: int = Field(
    0,
    description="Number of orders needing attention"
)
attention_orders_only: list[N8nPendingOrderItem] = Field(
    default_factory=list,
    description="Orders that need attention (populated when attention_only=true)"
)
```

**Step 3: Update examples in model_config**

Update the `json_schema_extra` examples to include the new fields.

**Step 4: Run tests to ensure schema is valid**

Run: `uv run pytest tests/test_n8n*.py -v -k "not live"`
Expected: Pass (schemas load correctly)

**Step 5: Commit**

```bash
git add app/schemas/n8n.py
git commit -m "feat(schemas): add fill_proximity and needs_attention fields to n8n pending orders"
```

---

## Task 2: Add Classification Logic Module

**Files:**
- Create: `app/services/intraday_order_review.py`

**Step 1: Create the classification module**

```python
"""Intraday order review classification logic."""

from __future__ import annotations

from typing import Any


def classify_fill_proximity(gap_pct: float | None, thresholds: dict[str, float] | None = None) -> str:
    """Classify order fill proximity based on gap percentage.
    
    Args:
        gap_pct: Gap between current price and order price in percent
        thresholds: Optional custom thresholds with keys: near, moderate, far
        
    Returns:
        Classification: "near", "moderate", "far", or "very_far"
    """
    if gap_pct is None:
        return "unknown"
    
    defaults = {"near": 2.0, "moderate": 5.0, "far": 10.0}
    t = {**defaults, **(thresholds or {})}
    
    abs_gap = abs(gap_pct)
    if abs_gap <= t["near"]:
        return "near"
    elif abs_gap <= t["moderate"]:
        return "moderate"
    elif abs_gap <= t["far"]:
        return "far"
    else:
        return "very_far"


def format_fill_proximity(proximity: str, gap_pct: float | None = None) -> str:
    """Format fill proximity for display."""
    labels = {
        "near": "체결 임박 ⚡",
        "moderate": "체결 근접",
        "far": "체결 거리",
        "very_far": "체결 멈",
        "unknown": "알 수 없음",
    }
    return labels.get(proximity, proximity)


def check_needs_attention(
    order: dict[str, Any],
    indicators: dict[str, Any] | None,
    thresholds: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Check if an order needs attention based on market conditions.
    
    Args:
        order: Normalized order dict with gap_pct, side fields
        indicators: Market indicators dict with rsi_14, change_24h_pct
        thresholds: Optional custom thresholds
        
    Returns:
        Tuple of (needs_attention, reason_string)
    """
    defaults = {
        "near_fill_pct": 2.0,
        "market_volatility_pct": 5.0,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "far_order_pct": 15.0,
    }
    t = {**defaults, **(thresholds or {})}
    
    reasons = []
    gap_pct = order.get("gap_pct")
    side = order.get("side", "").lower()
    rsi = indicators.get("rsi_14") if indicators else None
    change_24h = indicators.get("change_24h_pct", 0) if indicators else 0
    
    # Near fill (any side)
    if gap_pct is not None and abs(gap_pct) <= t["near_fill_pct"]:
        reasons.append(f"체결 임박 ({gap_pct:+.1f}%)")
    
    # Market volatility
    if abs(change_24h) >= t["market_volatility_pct"]:
        reasons.append(f"24h {change_24h:+.1f}% 급변")
    
    # RSI extremes - different for buy vs sell
    if rsi is not None:
        if side == "buy" and rsi >= t["rsi_overbought"]:
            reasons.append(f"RSI {rsi:.0f} 과매수 (매수 재검토)")
        if side == "sell" and rsi <= t["rsi_oversold"]:
            reasons.append(f"RSI {rsi:.0f} 과매도 (매도 재검토)")
    
    # Very far order (capital locked)
    if gap_pct is not None and abs(gap_pct) >= t["far_order_pct"]:
        reasons.append(f"현재가 대비 {abs(gap_pct):.0f}% 이탈 (자금 묶임)")
    
    if reasons:
        return True, " / ".join(reasons)
    return False, None


__all__ = [
    "classify_fill_proximity",
    "format_fill_proximity", 
    "check_needs_attention",
]
```

**Step 2: Write failing test**

Create `tests/test_intraday_order_review.py`:

```python
"""Tests for intraday order review classification logic."""

import pytest

from app.services.intraday_order_review import (
    classify_fill_proximity,
    format_fill_proximity,
    check_needs_attention,
)


class TestClassifyFillProximity:
    def test_near_for_small_gap(self):
        assert classify_fill_proximity(1.5) == "near"
        assert classify_fill_proximity(-1.5) == "near"
        
    def test_moderate_for_medium_gap(self):
        assert classify_fill_proximity(3.0) == "moderate"
        assert classify_fill_proximity(-4.5) == "moderate"
        
    def test_far_for_large_gap(self):
        assert classify_fill_proximity(7.0) == "far"
        assert classify_fill_proximity(-8.5) == "far"
        
    def test_very_far_for_extreme_gap(self):
        assert classify_fill_proximity(15.0) == "very_far"
        assert classify_fill_proximity(-20.0) == "very_far"
        
    def test_unknown_for_none(self):
        assert classify_fill_proximity(None) == "unknown"
        
    def test_custom_thresholds(self):
        thresholds = {"near": 3.0, "moderate": 6.0, "far": 12.0}
        assert classify_fill_proximity(2.5, thresholds) == "near"
        assert classify_fill_proximity(4.0, thresholds) == "moderate"


class TestFormatFillProximity:
    def test_format_near(self):
        assert "체결 임박" in format_fill_proximity("near")
        
    def test_format_unknown(self):
        assert format_fill_proximity("unknown") == "알 수 없음"


class TestCheckNeedsAttention:
    def test_near_fill_triggers_attention(self):
        order = {"gap_pct": 1.5, "side": "buy"}
        needs_attention, reason = check_needs_attention(order, {})
        assert needs_attention is True
        assert "체결 임박" in reason
        
    def test_market_volatility_triggers_attention(self):
        order = {"gap_pct": 10.0, "side": "buy"}
        indicators = {"change_24h_pct": 6.0}
        needs_attention, reason = check_needs_attention(order, indicators)
        assert needs_attention is True
        assert "급변" in reason
        
    def test_rsi_overbought_buy_order(self):
        order = {"gap_pct": 5.0, "side": "buy"}
        indicators = {"rsi_14": 75}
        needs_attention, reason = check_needs_attention(order, indicators)
        assert needs_attention is True
        assert "과매수" in reason
        
    def test_rsi_oversold_sell_order(self):
        order = {"gap_pct": 5.0, "side": "sell"}
        indicators = {"rsi_14": 25}
        needs_attention, reason = check_needs_attention(order, indicators)
        assert needs_attention is True
        assert "과매도" in reason
        
    def test_far_order_triggers_attention(self):
        order = {"gap_pct": -20.0, "side": "buy"}
        needs_attention, reason = check_needs_attention(order, {})
        assert needs_attention is True
        assert "자금 묶임" in reason
        
    def test_no_attention_needed(self):
        order = {"gap_pct": 8.0, "side": "buy"}
        indicators = {"change_24h_pct": 2.0, "rsi_14": 55}
        needs_attention, reason = check_needs_attention(order, indicators)
        assert needs_attention is False
        assert reason is None
```

**Step 3: Run tests to verify they fail (module doesn't exist yet)**

Run: `uv run pytest tests/test_intraday_order_review.py -v`
Expected: ImportError or ModuleNotFoundError

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_intraday_order_review.py -v`
Expected: All 11 tests pass

**Step 5: Commit**

```bash
git add app/services/intraday_order_review.py tests/test_intraday_order_review.py
git commit -m "feat(intraday): add order classification logic with tests"
```

---

## Task 3: Integrate Classification into Pending Orders Service

**Files:**
- Modify: `app/services/n8n_pending_orders_service.py`
- Modify: `app/routers/n8n.py`

**Step 1: Add imports to the service**

```python
# Add to imports at top of n8n_pending_orders_service.py:
from app.services.intraday_order_review import (
    classify_fill_proximity,
    format_fill_proximity,
    check_needs_attention,
)
from app.services.n8n_market_context_service import fetch_market_context
```

**Step 2: Add enrichment function**

```python
# Add before fetch_pending_orders function:
async def _enrich_orders_with_market_context(
    orders: list[dict[str, Any]],
    market: str,
    near_fill_pct: float = 2.0,
) -> dict[str, Any]:
    """Enrich orders with fill proximity and attention status using market context.
    
    Returns:
        Dict with enriched orders and attention counts
    """
    # Fetch market context for symbols in these orders
    symbols = [order["symbol"] for order in orders if order.get("symbol")]
    
    indicators_map: dict[str, dict[str, Any]] = {}
    if symbols:
        try:
            market_ctx = await fetch_market_context(
                market=market,
                symbols=symbols,
                include_fear_greed=False,
                include_economic_calendar=False,
            )
            for ctx in market_ctx.get("symbols", []):
                indicators_map[ctx.symbol] = {
                    "rsi_14": ctx.rsi_14,
                    "change_24h_pct": ctx.change_24h_pct,
                }
        except Exception:
            # Non-fatal: continue without market context
            pass
    
    enriched_orders = []
    near_fill_count = 0
    needs_attention_count = 0
    attention_orders = []
    
    for order in orders:
        gap_pct = order.get("gap_pct")
        symbol = order.get("symbol", "")
        
        # Classify fill proximity
        proximity = classify_fill_proximity(gap_pct, {"near": near_fill_pct})
        order["fill_proximity"] = proximity
        order["fill_proximity_fmt"] = format_fill_proximity(proximity, gap_pct)
        
        if proximity == "near":
            near_fill_count += 1
        
        # Check attention needs
        indicators = indicators_map.get(symbol, {})
        needs_attention, attention_reason = check_needs_attention(
            order,
            indicators,
            {"near_fill_pct": near_fill_pct},
        )
        
        order["needs_attention"] = needs_attention
        order["attention_reason"] = attention_reason
        
        if needs_attention:
            needs_attention_count += 1
            attention_orders.append(order)
        
        enriched_orders.append(order)
    
    return {
        "orders": enriched_orders,
        "near_fill_count": near_fill_count,
        "needs_attention_count": needs_attention_count,
        "attention_orders": attention_orders,
    }
```

**Step 3: Update fetch_pending_orders signature and logic**

```python
# Change signature from line 207:
async def fetch_pending_orders(
    *,
    market: Literal["crypto", "kr", "us", "all"] = "all",
    min_amount: float = 0,
    include_current_price: bool = True,
    side: Literal["buy", "sell"] | None = None,
    as_of: datetime | None = None,
    # Add new parameters:
    attention_only: bool = False,
    near_fill_pct: float = 2.0,
) -> dict[str, Any]:
```

**Step 4: Add enrichment call after price fetching**

```python
# After line 300 (after enrich_order_fmt call), add:
# Enrich with fill proximity and attention status if current price is included
if include_current_price:
    enrichment = await _enrich_orders_with_market_context(
        filtered_orders,
        market,
        near_fill_pct=near_fill_pct,
    )
    filtered_orders = enrichment["orders"]
else:
    enrichment = {
        "near_fill_count": 0,
        "needs_attention_count": 0,
        "attention_orders": [],
    }

# Filter to attention-only if requested
if attention_only:
    filtered_orders = enrichment["attention_orders"]

summary = _build_summary(filtered_orders)
summary["near_fill_count"] = enrichment["near_fill_count"]
summary["needs_attention_count"] = enrichment["needs_attention_count"]
summary["attention_orders_only"] = enrichment["attention_orders"] if attention_only else []
```

**Step 5: Update router endpoint to accept new parameters**

```python
# In app/routers/n8n.py, modify the get_pending_orders function:
@router.get("/pending-orders", response_model=N8nPendingOrdersResponse)
async def get_pending_orders(
    market: Literal["crypto", "kr", "us", "all"] = Query(
        "all", description="Market filter"
    ),
    min_amount: float = Query(0, ge=0, description="Minimum KRW amount filter"),
    include_current_price: bool = Query(
        True, description="Fetch current prices and compute gap percentage"
    ),
    side: Literal["buy", "sell"] | None = Query(None, description="Order side filter"),
    # Add new parameters:
    attention_only: bool = Query(
        False, description="Return only orders that need attention"
    ),
    near_fill_pct: float = Query(
        2.0, ge=0.1, le=50.0, description="Near fill threshold percentage"
    ),
) -> N8nPendingOrdersResponse | JSONResponse:
```

**Step 6: Pass new parameters to service call**

```python
# In the try block, update the fetch_pending_orders call:
result = await fetch_pending_orders(
    market=market,
    min_amount=min_amount,
    include_current_price=include_current_price,
    side=side,
    as_of=as_of_dt,
    # Add:
    attention_only=attention_only,
    near_fill_pct=near_fill_pct,
)
```

**Step 7: Run integration tests**

Run: `uv run pytest tests/test_n8n*.py -v -k "not live"`
Expected: All tests pass

**Step 8: Manual verification with curl**

```bash
# Test without attention filter (existing behavior)
curl "http://localhost:8000/api/n8n/pending-orders?market=crypto"

# Test with attention_only filter
curl "http://localhost:8000/api/n8n/pending-orders?market=crypto&attention_only=true"

# Test with custom near_fill_pct
curl "http://localhost:8000/api/n8n/pending-orders?market=crypto&attention_only=true&near_fill_pct=3.0"
```

**Step 9: Commit**

```bash
git add app/services/n8n_pending_orders_service.py app/routers/n8n.py
git commit -m "feat(intraday): integrate fill proximity and attention detection into pending orders"
```

---

## Task 4: Add Background Task for Intraday Checks

**Files:**
- Create: `app/tasks/intraday_order_review_tasks.py`

**Step 1: Create the task module**

```python
"""Background tasks for intraday order review."""

from __future__ import annotations

import logging
from datetime import datetime

from app.core.taskiq_dependencies import get_broker
from app.core.timezone import now_kst
from app.services.n8n_pending_orders_service import fetch_pending_orders

logger = logging.getLogger(__name__)
broker = get_broker()


@broker.task(schedule=[
    # Crypto market: 24/7 trading
    {"cron": "0 14 * * *"},   # 14:00 KST
    {"cron": "0 21 * * *"},   # 21:00 KST
])
async def intraday_crypto_order_review() -> dict[str, object]:
    """Intraday order review for crypto market."""
    as_of = now_kst()
    logger.info(f"Starting intraday crypto order review at {as_of}")
    
    result = await fetch_pending_orders(
        market="crypto",
        attention_only=True,
        include_current_price=True,
        as_of=as_of,
    )
    
    attention_count = result.get("summary", {}).get("needs_attention_count", 0)
    logger.info(f"Crypto intraday review complete: {attention_count} orders need attention")
    
    return {
        "market": "crypto",
        "as_of": as_of.isoformat(),
        "attention_count": attention_count,
        "attention_orders": [
            {
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "attention_reason": order.get("attention_reason"),
            }
            for order in result.get("orders", [])
        ],
    }


@broker.task(schedule=[
    # KR market: 09:00-15:30 KST
    {"cron": "0 10 * * 1-5"},   # 10:00 KST, Mon-Fri
    {"cron": "0 14 * * 1-5"},   # 14:00 KST, Mon-Fri
])
async def intraday_kr_order_review() -> dict[str, object]:
    """Intraday order review for Korean stock market."""
    as_of = now_kst()
    
    # Skip if outside trading hours (safety check)
    if not _is_kr_trading_hours(as_of):
        logger.info(f"Skipping KR intraday review: outside trading hours")
        return {"market": "kr", "skipped": True, "reason": "outside_trading_hours"}
    
    logger.info(f"Starting intraday KR order review at {as_of}")
    
    result = await fetch_pending_orders(
        market="kr",
        attention_only=True,
        include_current_price=True,
        as_of=as_of,
    )
    
    attention_count = result.get("summary", {}).get("needs_attention_count", 0)
    logger.info(f"KR intraday review complete: {attention_count} orders need attention")
    
    return {
        "market": "kr",
        "as_of": as_of.isoformat(),
        "attention_count": attention_count,
    }


@broker.task(schedule=[
    # US market: 23:30-06:00 KST (corresponds to 09:30-16:00 EST)
    {"cron": "30 0 * * 1-5"},   # 00:30 KST, Mon-Fri
    {"cron": "0 4 * * 1-5"},    # 04:00 KST, Mon-Fri
])
async def intraday_us_order_review() -> dict[str, object]:
    """Intraday order review for US stock market."""
    as_of = now_kst()
    
    # Skip if outside trading hours (safety check)
    if not _is_us_trading_hours(as_of):
        logger.info(f"Skipping US intraday review: outside trading hours")
        return {"market": "us", "skipped": True, "reason": "outside_trading_hours"}
    
    logger.info(f"Starting intraday US order review at {as_of}")
    
    result = await fetch_pending_orders(
        market="us",
        attention_only=True,
        include_current_price=True,
        as_of=as_of,
    )
    
    attention_count = result.get("summary", {}).get("needs_attention_count", 0)
    logger.info(f"US intraday review complete: {attention_count} orders need attention")
    
    return {
        "market": "us",
        "as_of": as_of.isoformat(),
        "attention_count": attention_count,
    }


def _is_kr_trading_hours(dt: datetime) -> bool:
    """Check if current time is within KR market hours (09:00-15:30 KST)."""
    # Simplified check: weekday 09:00-15:30
    if dt.weekday() >= 5:  # Sat, Sun
        return False
    hour = dt.hour
    minute = dt.minute
    time_val = hour * 100 + minute
    return 900 <= time_val <= 1530


def _is_us_trading_hours(dt: datetime) -> bool:
    """Check if current time is within US market hours (23:30-06:00 KST)."""
    if dt.weekday() >= 5:
        return False
    hour = dt.hour
    # US market in KST: 23:30-06:00
    return hour >= 23 or hour < 6


__all__ = [
    "intraday_crypto_order_review",
    "intraday_kr_order_review",
    "intraday_us_order_review",
]
```

**Step 2: Register tasks in the tasks module init**

Read and update `app/tasks/__init__.py`:

```python
# Add import:
from app.tasks.intraday_order_review_tasks import (
    intraday_crypto_order_review,
    intraday_kr_order_review,
    intraday_us_order_review,
)

# Add to __all__ if it exists, or ensure imports are not removed
```

**Step 3: Write tests for tasks**

Create `tests/test_intraday_order_review_tasks.py`:

```python
"""Tests for intraday order review background tasks."""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.tasks.intraday_order_review_tasks import (
    intraday_crypto_order_review,
    _is_kr_trading_hours,
    _is_us_trading_hours,
)


class TestTradingHoursCheck:
    def test_kr_trading_hours_weekday(self):
        dt = datetime(2026, 3, 16, 10, 0)  # Monday 10:00
        assert _is_kr_trading_hours(dt) is True
        
    def test_kr_trading_hours_weekend(self):
        dt = datetime(2026, 3, 15, 10, 0)  # Sunday
        assert _is_kr_trading_hours(dt) is False
        
    def test_kr_trading_hours_before_open(self):
        dt = datetime(2026, 3, 16, 8, 0)  # Before 09:00
        assert _is_kr_trading_hours(dt) is False
        
    def test_us_trading_hours_late_night(self):
        dt = datetime(2026, 3, 16, 0, 30)  # 00:30
        assert _is_us_trading_hours(dt) is True
        
    def test_us_trading_hours_early_morning(self):
        dt = datetime(2026, 3, 16, 4, 0)   # 04:00
        assert _is_us_trading_hours(dt) is True
        
    def test_us_trading_hours_daytime(self):
        dt = datetime(2026, 3, 16, 12, 0)  # 12:00
        assert _is_us_trading_hours(dt) is False


class TestIntradayCryptoReview:
    @pytest.mark.asyncio
    async def test_returns_attention_count(self):
        mock_result = {
            "summary": {"needs_attention_count": 2},
            "orders": [
                {"symbol": "BTC", "side": "buy", "attention_reason": "test"}
            ]
        }
        
        with patch("app.tasks.intraday_order_review_tasks.fetch_pending_orders", 
                   AsyncMock(return_value=mock_result)):
            result = await intraday_crypto_order_review()
            
        assert result["market"] == "crypto"
        assert result["attention_count"] == 2
        assert len(result["attention_orders"]) == 1
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_intraday_order_review_tasks.py -v`
Expected: All tests pass

**Step 5: Verify task registration**

Run: `make taskiq-scheduler` and check logs for scheduled tasks
Expected: See intraday_crypto_order_review, intraday_kr_order_review, intraday_us_order_review in task list

**Step 6: Commit**

```bash
git add app/tasks/intraday_order_review_tasks.py app/tasks/__init__.py tests/test_intraday_order_review_tasks.py
git commit -m "feat(intraday): add scheduled background tasks for order reviews"
```

---

## Task 5: Update API Documentation and Tests

**Files:**
- Modify: `tests/test_n8n_pending_orders.py` (or create if not exists)

**Step 1: Add integration tests for new parameters**

```python
"""Additional tests for pending orders with attention detection."""

import pytest
from unittest.mock import AsyncMock, patch


class TestPendingOrdersAttentionOnly:
    @pytest.mark.asyncio
    async def test_attention_only_returns_filtered_orders(self, client):
        """Test that attention_only=true returns only orders needing attention."""
        with patch("app.routers.n8n.fetch_pending_orders") as mock_fetch:
            mock_fetch.return_value = {
                "success": True,
                "market": "crypto",
                "orders": [
                    {
                        "order_id": "1",
                        "symbol": "BTC",
                        "needs_attention": True,
                        "attention_reason": "체결 임박",
                    }
                ],
                "summary": {
                    "total": 1,
                    "needs_attention_count": 1,
                    "near_fill_count": 1,
                    "attention_orders_only": [],  # Not populated in summary
                },
                "errors": [],
            }
            
            response = client.get("/api/n8n/pending-orders?market=crypto&attention_only=true")
            
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["orders"]) == 1
        assert data["summary"]["needs_attention_count"] == 1

    @pytest.mark.asyncio
    async def test_near_fill_pct_parameter(self, client):
        """Test that near_fill_pct parameter is passed to service."""
        with patch("app.routers.n8n.fetch_pending_orders") as mock_fetch:
            mock_fetch.return_value = {
                "success": True,
                "market": "crypto",
                "orders": [],
                "summary": {"total": 0, "needs_attention_count": 0, "near_fill_count": 0},
                "errors": [],
            }
            
            response = client.get("/api/n8n/pending-orders?market=crypto&near_fill_pct=3.5")
            
        assert response.status_code == 200
        # Verify the service was called with the parameter
        call_kwargs = mock_fetch.call_args.kwargs
        assert call_kwargs["near_fill_pct"] == 3.5
```

**Step 2: Run full test suite**

Run: `uv run pytest tests/test_n8n*.py -v -k "not live"`
Expected: All tests pass

**Step 3: Commit**

```bash
git add tests/test_n8n_pending_orders.py
git commit -m "test(intraday): add tests for attention_only and near_fill_pct parameters"
```

---

## Task 6: Verify End-to-End

**Step 1: Start the development server**

```bash
make dev
```

**Step 2: Test all scenarios with curl**

```bash
# 1. Get all pending orders (existing behavior)
curl -s "http://localhost:8000/api/n8n/pending-orders?market=crypto" | jq '.orders[0] | {symbol, fill_proximity, needs_attention, attention_reason}'

# 2. Get only attention-needed orders
curl -s "http://localhost:8000/api/n8n/pending-orders?market=crypto&attention_only=true" | jq '{count: .summary.needs_attention_count, orders: [.orders[].symbol]}'

# 3. Check summary fields
curl -s "http://localhost:8000/api/n8n/pending-orders?market=crypto" | jq '.summary | {total, near_fill_count, needs_attention_count}'

# 4. Test with custom near_fill_pct
curl -s "http://localhost:8000/api/n8n/pending-orders?market=crypto&near_fill_pct=1.0&attention_only=true" | jq '.summary'
```

**Step 3: Verify market-context integration**

```bash
# Ensure market-context still works (used for RSI/24h data)
curl -s "http://localhost:8000/api/n8n/market-context?market=crypto&symbols=BTC,ETH" | jq '.symbols[] | {symbol, rsi_14, change_24h_pct}'
```

**Step 4: Run linting**

```bash
make lint
```
Expected: No errors in modified files

**Step 5: Final commit**

```bash
git commit --amend -m "feat(intraday): complete intraday order review feature with attention detection

- Add fill_proximity and needs_attention fields to pending orders
- Add attention_only and near_fill_pct query parameters
- Integrate market-context for RSI and 24h volatility detection
- Add background tasks for scheduled intraday reviews
- Add comprehensive tests for classification logic

Closes: #intraday-order-review"
```

---

## Implementation Notes

### Data Flow

```
GET /api/n8n/pending-orders?attention_only=true
  ↓
Router validates parameters
  ↓
fetch_pending_orders() service
  ├─ Fetch orders from brokers (existing)
  ├─ Fetch current prices (existing)
  ├─ Calculate gap_pct (existing)
  ├─ _enrich_orders_with_market_context() [NEW]
  │   ├─ Fetch market context (RSI, 24h change)
  │   ├─ classify_fill_proximity()
  │   ├─ check_needs_attention()
  │   └─ Returns enriched orders + counts
  ├─ Filter by attention_only if requested
  └─ Build summary with new fields
  ↓
Return JSON response with new fields
```

### n8n Workflow Recommendation

The n8n workflow should be configured as:

```
Schedule Trigger (14:00, 21:00 for crypto)
  ↓
HTTP Request → GET /api/n8n/pending-orders?market=crypto&attention_only=true
  ↓
If needs_attention_count > 0
  ↓
HTTP Request → GET /api/n8n/market-context?symbols=SYMBOLS_FROM_ATTENTION_ORDERS
  ↓
Telegram/Discord notification with order details
```

### Future Enhancements (Not in this plan)

1. **Cooldown mechanism**: Store last notification time per order to prevent duplicate alerts
2. **WebSocket integration**: Real-time notifications for near-fill orders instead of polling
3. **Configurable thresholds**: Store thresholds in database per user/market
4. **Order action API**: Add endpoints to cancel/modify orders directly from notifications

---

## Checklist Summary

- [ ] Task 1: Update Pydantic Schemas (`N8nPendingOrderItem`, `N8nPendingOrderSummary`)
- [ ] Task 2: Create classification module (`app/services/intraday_order_review.py`)
- [ ] Task 3: Integrate into pending orders service and router
- [ ] Task 4: Add background tasks for scheduled reviews
- [ ] Task 5: Update tests and documentation
- [ ] Task 6: End-to-end verification

---

**Plan saved to:** `docs/plans/2026-03-16-intraday-order-review.md`

**Ready for execution.** Use `superpowers:executing-plans` skill to implement task-by-task.
