# Trade Review System — API Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build 6 n8n-facing API endpoints for trade review: filled-order collection, post-trade evaluation storage, review statistics, extended pending-order monitoring, pending-snapshot recording, and pending-snapshot resolution.

**Architecture:** Thin router handlers in `app/routers/n8n.py` delegate to dedicated service modules per feature. Filled-orders calls broker APIs directly (Upbit/KIS); review endpoints write to the existing `review` schema via async SQLAlchemy. All schemas live in `app/schemas/n8n.py` alongside existing n8n schemas.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, PostgreSQL (`review` schema), Pydantic v2

---

## Pre-Existing (DO NOT recreate)

| What | Where | Status |
|------|-------|--------|
| 4 DB models (Trade, TradeSnapshot, TradeReview, PendingSnapshot) | `app/models/review.py` | ✅ Done |
| Alembic migration for `review` schema | `alembic/versions/672f39265fed` | ✅ Done |
| Model imports in `__init__.py` | `app/models/__init__.py:20` | ✅ Done |

---

## Design Decisions (from pre-planning analysis)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Call broker APIs directly for filled-orders — NOT `get_order_history_impl` | `get_order_history_impl` requires `symbol` when `status != "pending"`. Broker APIs support symbol-free queries. |
| D2 | Filter Upbit closed orders to `state == "done"` only | `fetch_closed_orders` returns both done AND cancelled. Filled-orders should exclude cancellations. |
| D3 | Account identifiers: `"upbit"`, `"kis"`, `"kis_overseas"` string literals | Consistent with how `n8n_pending_orders_service` maps markets. No real account numbers in DB. |
| D4 | No auth on new endpoints — match existing n8n pattern | All n8n endpoints are unauthenticated today. Global AuthMiddleware applies. |
| D5 | Trade UPSERT uses `INSERT ... ON CONFLICT (account, order_id) DO NOTHING` | Spec says "skip on duplicate". Retrieve existing `trade_id` via separate SELECT after conflict. |
| D6 | Validate `order_id is not None` before UPSERT | Null order_id bypasses unique constraint (PostgreSQL allows unlimited NULL duplicates). |
| D7 | `trade_date` source: Upbit `created_at`, KIS `ord_dt + ord_tmd` | Best available execution timestamps from each broker. |
| D8 | `fill_probability` is a computed response field, NOT stored in DB | PendingSnapshot model has no such column. Compute from `gap_pct` + `days_pending`. |
| D9 | Stats queries use `AT TIME ZONE 'Asia/Seoul'` for date grouping | App convention is KST. PostgreSQL stores UTC. Without TZ conversion, date boundaries are wrong. |
| D10 | Idempotent reviews: check for existing `(trade_id, review_type)` before INSERT, skip if exists | Prevents duplicate reviews on n8n retries. |
| D11 | Pending-review wraps existing `fetch_pending_orders` with computed fields | Avoids duplicating complex pending-orders logic. |

---

## Reference Files (read before implementing)

| Purpose | File |
|---------|------|
| n8n router pattern (thin handlers, service delegation) | `app/routers/n8n.py` |
| n8n schema pattern (Pydantic + ConfigDict + Field) | `app/schemas/n8n.py` |
| n8n service pattern (async, broker calls) | `app/services/n8n_pending_orders_service.py` |
| DB session factory | `app/core/db.py` (get_db, AsyncSessionLocal) |
| Review models (Trade, TradeSnapshot, etc.) | `app/models/review.py` |
| Order normalization (Upbit/KIS) | `app/mcp_server/tooling/orders_modify_cancel.py` |
| Upbit broker client | `app/services/brokers/upbit/client.py` |
| KIS broker client | `app/services/brokers/kis/client.py` |
| Current price service | `app/services/market_data/service.py` (get_quote) |
| KST timezone helper | `app/core/timezone.py` |
| Existing test fixtures | `tests/conftest.py` |

---

## File Plan

```
Modified:
  app/routers/n8n.py                         ← Add 6 endpoints
  app/schemas/n8n.py                          ← Add review request/response schemas

New:
  app/services/n8n_filled_orders_service.py   ← Filled-orders broker integration
  app/services/n8n_trade_review_service.py    ← Trade UPSERT + review writes + stats
  app/services/n8n_pending_review_service.py  ← Pending-review wrapper
  app/services/n8n_pending_snapshot_service.py← Pending-snapshot save + resolve
  tests/test_n8n_trade_review.py              ← All review endpoint tests
```

---

### Task 1: Pydantic Schemas for All 6 Endpoints

**Files:**
- Modify: `app/schemas/n8n.py` (append after existing schemas)

**Step 1: Add filled-orders response schemas**

Append to `app/schemas/n8n.py`:

```python
# ---------------------------------------------------------------------------
# Filled Orders
# ---------------------------------------------------------------------------
class N8nFilledOrderItem(BaseModel):
    symbol: str = Field(..., description="Normalized symbol (e.g. BTC, 005930, NVDA)")
    raw_symbol: str = Field(..., description="Original broker symbol (e.g. KRW-BTC)")
    instrument_type: str = Field(..., description="crypto, equity_kr, equity_us")
    side: str = Field(..., description="buy or sell")
    price: float = Field(..., description="Execution price")
    quantity: float = Field(..., description="Filled quantity")
    total_amount: float = Field(..., description="Total filled amount (price * quantity)")
    fee: float = Field(0, description="Trading fee")
    currency: str = Field(..., description="KRW or USD")
    account: str = Field(..., description="Account identifier: upbit, kis, kis_overseas")
    order_id: str = Field(..., description="Unique order identifier from broker")
    filled_at: str = Field(..., description="Execution timestamp in KST ISO8601")
    current_price: float | None = Field(None, description="Current market price")
    pnl_pct: float | None = Field(None, description="Unrealized P&L percentage")
    pnl_pct_fmt: str | None = Field(None, description="Formatted P&L, e.g. +3.27%")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "symbol": "BTC",
                "raw_symbol": "KRW-BTC",
                "instrument_type": "crypto",
                "side": "buy",
                "price": 98000000,
                "quantity": 0.015,
                "total_amount": 1470000,
                "fee": 735,
                "currency": "KRW",
                "account": "upbit",
                "order_id": "abc-123-def",
                "filled_at": "2026-03-17T14:30:00+09:00",
                "current_price": 101200000,
                "pnl_pct": 3.27,
                "pnl_pct_fmt": "+3.27%",
            }
        }
    )


class N8nFilledOrdersResponse(BaseModel):
    success: bool = Field(..., description="Whether the request completed successfully")
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    total_count: int = Field(..., description="Total number of filled orders returned")
    orders: list[N8nFilledOrderItem] = Field(
        default_factory=list, description="Filled order items"
    )
    errors: list[dict[str, object]] = Field(
        default_factory=list, description="Non-fatal errors from individual market fetches"
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "as_of": "2026-03-17T20:00:00+09:00",
                "total_count": 1,
                "orders": [],
                "errors": [],
            }
        }
    )
```

**Step 2: Add trade-reviews request/response schemas**

```python
# ---------------------------------------------------------------------------
# Trade Reviews (POST + GET stats)
# ---------------------------------------------------------------------------
class N8nTradeReviewIndicators(BaseModel):
    rsi_14: float | None = Field(None, description="RSI 14-period")
    rsi_7: float | None = Field(None, description="RSI 7-period")
    ema_20: float | None = Field(None, description="EMA 20")
    ema_200: float | None = Field(None, description="EMA 200")
    macd: float | None = Field(None, description="MACD value")
    macd_signal: float | None = Field(None, description="MACD signal line")
    adx: float | None = Field(None, description="ADX value")
    stoch_rsi_k: float | None = Field(None, description="Stochastic RSI K")
    volume_ratio: float | None = Field(None, description="Volume ratio vs 20d avg")
    fear_greed: int | None = Field(None, description="Fear & Greed Index 0-100")


class N8nTradeReviewItem(BaseModel):
    order_id: str = Field(..., description="Broker order ID (required, non-null)")
    account: str = Field(..., description="Account: upbit, kis, kis_overseas")
    symbol: str = Field(..., description="Normalized symbol")
    instrument_type: str = Field(..., description="crypto, equity_kr, equity_us")
    side: str = Field(..., description="buy or sell")
    price: float = Field(..., description="Execution price")
    quantity: float = Field(..., description="Filled quantity")
    total_amount: float = Field(..., description="Total amount")
    fee: float = Field(0, description="Trading fee")
    currency: str = Field("KRW", description="KRW or USD")
    filled_at: str = Field(..., description="Execution timestamp ISO8601")
    price_at_review: float | None = Field(None, description="Current price at review time")
    pnl_pct: float | None = Field(None, description="P&L percentage")
    verdict: str = Field(..., description="good, neutral, or bad")
    comment: str | None = Field(None, description="Review commentary")
    review_type: str = Field("daily", description="daily, weekly, monthly, manual")
    indicators: N8nTradeReviewIndicators | None = Field(
        None, description="Technical indicator snapshot at execution time"
    )


class N8nTradeReviewsRequest(BaseModel):
    reviews: list[N8nTradeReviewItem] = Field(
        ..., description="List of trade reviews to save", min_length=1
    )


class N8nTradeReviewsResponse(BaseModel):
    success: bool = Field(...)
    saved_count: int = Field(..., description="Number of reviews saved")
    skipped_count: int = Field(
        0, description="Number skipped (duplicate trade or existing review)"
    )
    errors: list[dict[str, object]] = Field(default_factory=list)


class N8nRsiZoneStats(BaseModel):
    count: int = Field(...)
    avg_pnl: float | None = Field(None)
    win_rate: float | None = Field(None)


class N8nTradeReviewStats(BaseModel):
    period: str = Field(..., description="Period label, e.g. 2026-03-10 ~ 2026-03-17")
    total_trades: int = Field(0)
    buy_count: int = Field(0)
    sell_count: int = Field(0)
    win_rate: float | None = Field(None, description="Percentage of trades with pnl > 0")
    avg_pnl_pct: float | None = Field(None)
    best_trade: dict[str, object] | None = Field(None)
    worst_trade: dict[str, object] | None = Field(None)
    by_verdict: dict[str, int] = Field(default_factory=dict)
    by_rsi_zone: dict[str, N8nRsiZoneStats] = Field(default_factory=dict)


class N8nTradeReviewStatsResponse(BaseModel):
    success: bool = Field(...)
    stats: N8nTradeReviewStats = Field(...)
    errors: list[dict[str, object]] = Field(default_factory=list)
```

**Step 3: Add pending-review response schemas**

```python
# ---------------------------------------------------------------------------
# Pending Review (extended pending-orders)
# ---------------------------------------------------------------------------
class N8nPendingReviewItem(BaseModel):
    """Extends N8nPendingOrderItem with review-specific fields."""

    order_id: str = Field(...)
    symbol: str = Field(...)
    raw_symbol: str = Field(...)
    market: str = Field(...)
    side: str = Field(...)
    order_price: float = Field(...)
    current_price: float | None = Field(None)
    gap_pct: float | None = Field(None)
    gap_pct_fmt: str | None = Field(None)
    amount_krw: float | None = Field(None)
    quantity: float = Field(...)
    remaining_qty: float = Field(...)
    created_at: str = Field(...)
    age_days: int = Field(...)
    currency: str = Field(...)
    days_pending: int = Field(..., description="Days since order creation")
    fill_probability: str = Field(
        ..., description="high, medium, low, or stale"
    )
    suggestion: str | None = Field(None, description="Action suggestion in Korean")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "order_id": "xyz-456",
                "symbol": "BTC",
                "raw_symbol": "KRW-BTC",
                "market": "crypto",
                "side": "buy",
                "order_price": 96500000,
                "current_price": 101200000,
                "gap_pct": -4.6,
                "gap_pct_fmt": "-4.6%",
                "amount_krw": 965000,
                "quantity": 0.01,
                "remaining_qty": 0.01,
                "created_at": "2026-03-14T10:00:00+09:00",
                "age_days": 3,
                "currency": "KRW",
                "days_pending": 3,
                "fill_probability": "medium",
                "suggestion": "가격 조정 검토",
            }
        }
    )


class N8nPendingReviewResponse(BaseModel):
    success: bool = Field(...)
    as_of: str = Field(...)
    total_count: int = Field(...)
    orders: list[N8nPendingReviewItem] = Field(default_factory=list)
    errors: list[dict[str, object]] = Field(default_factory=list)
```

**Step 4: Add pending-snapshots request/response schemas**

```python
# ---------------------------------------------------------------------------
# Pending Snapshots (POST + PATCH resolve)
# ---------------------------------------------------------------------------
class N8nPendingSnapshotItem(BaseModel):
    symbol: str = Field(...)
    instrument_type: str = Field(..., description="crypto, equity_kr, equity_us")
    side: str = Field(...)
    order_price: float = Field(...)
    quantity: float = Field(...)
    current_price: float | None = Field(None)
    gap_pct: float | None = Field(None)
    days_pending: int | None = Field(None)
    account: str = Field(...)
    order_id: str | None = Field(None)


class N8nPendingSnapshotsRequest(BaseModel):
    snapshots: list[N8nPendingSnapshotItem] = Field(
        ..., min_length=1, description="Pending order snapshots to save"
    )


class N8nPendingSnapshotsResponse(BaseModel):
    success: bool = Field(...)
    saved_count: int = Field(...)
    errors: list[dict[str, object]] = Field(default_factory=list)


class N8nPendingResolutionItem(BaseModel):
    order_id: str = Field(...)
    account: str = Field(...)
    resolved_as: str = Field(
        ..., description="filled, cancelled, or expired"
    )


class N8nPendingResolveRequest(BaseModel):
    resolutions: list[N8nPendingResolutionItem] = Field(
        ..., min_length=1, description="Resolutions to apply"
    )


class N8nPendingResolveResponse(BaseModel):
    success: bool = Field(...)
    resolved_count: int = Field(...)
    not_found_count: int = Field(0)
    errors: list[dict[str, object]] = Field(default_factory=list)
```

**Step 5: Verify schemas parse correctly**

Run: `uv run python -c "from app.schemas.n8n import N8nFilledOrdersResponse, N8nTradeReviewsRequest, N8nTradeReviewStatsResponse, N8nPendingReviewResponse, N8nPendingSnapshotsResponse, N8nPendingResolveResponse; print('All schemas imported OK')"`

Expected: `All schemas imported OK`

**Step 6: Commit**

```bash
git add app/schemas/n8n.py
git commit -m "feat(n8n): add Pydantic schemas for trade review endpoints"
```

---

### Task 2: Filled-Orders Service

**Files:**
- Create: `app/services/n8n_filled_orders_service.py`
- Create: `tests/test_n8n_trade_review.py`

**Step 1: Write test for filled-orders service**

Create `tests/test_n8n_trade_review.py`:

```python
"""Tests for n8n trade review endpoints."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.unit
class TestFilledOrdersService:
    """Tests for fetch_filled_orders service function."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_orders(self):
        """No filled orders across all markets → empty list."""
        from app.services.n8n_filled_orders_service import fetch_filled_orders

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_domestic_filled",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_overseas_filled",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await fetch_filled_orders(days=1, markets="crypto,kr,us")

        assert result["orders"] == []
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_filters_upbit_cancelled_orders(self):
        """Upbit cancelled orders (state != done) are excluded."""
        from app.services.n8n_filled_orders_service import fetch_filled_orders

        mock_closed = [
            {
                "uuid": "aaa-111",
                "side": "bid",
                "ord_type": "limit",
                "price": "98000000",
                "state": "done",
                "market": "KRW-BTC",
                "volume": "0.015",
                "executed_volume": "0.015",
                "paid_fee": "735",
                "created_at": "2026-03-17T14:30:00+09:00",
            },
            {
                "uuid": "bbb-222",
                "side": "bid",
                "ord_type": "limit",
                "price": "100000000",
                "state": "cancel",
                "market": "KRW-BTC",
                "volume": "0.01",
                "executed_volume": "0",
                "paid_fee": "0",
                "created_at": "2026-03-17T15:00:00+09:00",
            },
        ]

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                return_value=mock_closed,
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_domestic_filled",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_overseas_filled",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.services.n8n_filled_orders_service._enrich_with_current_prices",
                new_callable=AsyncMock,
                side_effect=lambda orders: orders,
            ),
        ):
            result = await fetch_filled_orders(days=1, markets="crypto")

        assert len(result["orders"]) == 1
        assert result["orders"][0]["order_id"] == "aaa-111"
        assert result["orders"][0]["side"] == "buy"

    @pytest.mark.asyncio
    async def test_min_amount_filter(self):
        """Orders below min_amount are excluded."""
        from app.services.n8n_filled_orders_service import fetch_filled_orders

        mock_closed = [
            {
                "uuid": "aaa-111",
                "side": "bid",
                "ord_type": "limit",
                "price": "1000",
                "state": "done",
                "market": "KRW-XRP",
                "volume": "5",
                "executed_volume": "5",
                "paid_fee": "2.5",
                "created_at": "2026-03-17T14:30:00+09:00",
            },
        ]

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                return_value=mock_closed,
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_domestic_filled",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_overseas_filled",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await fetch_filled_orders(
                days=1, markets="crypto", min_amount=10000
            )

        assert len(result["orders"]) == 0
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_n8n_trade_review.py::TestFilledOrdersService -v`

Expected: FAIL (module not found)

**Step 3: Implement filled-orders service**

Create `app/services/n8n_filled_orders_service.py`:

```python
"""Filled-orders service — fetches recent fills across all markets."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import app.services.brokers.upbit.client as upbit_service
from app.core.timezone import KST, now_kst
from app.services.brokers.kis.client import KISClient
from app.services.market_data import get_quote

logger = logging.getLogger(__name__)

_EQUITY_QUOTE_CONCURRENCY = 5


def _strip_crypto_prefix(symbol: str) -> str:
    upper = str(symbol or "").strip().upper()
    for prefix in ("KRW-", "USDT-"):
        if upper.startswith(prefix):
            return upper[len(prefix):]
    return upper


def _normalize_upbit_filled(order: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a single Upbit closed order into our schema. Returns None if not 'done'."""
    if order.get("state") != "done":
        return None

    executed_vol = float(order.get("executed_volume") or 0)
    if executed_vol <= 0:
        return None

    price = float(order.get("price") or 0)
    total = price * executed_vol
    raw_symbol = str(order.get("market", ""))
    side_raw = str(order.get("side", "")).lower()

    return {
        "symbol": _strip_crypto_prefix(raw_symbol),
        "raw_symbol": raw_symbol,
        "instrument_type": "crypto",
        "side": "buy" if side_raw == "bid" else "sell",
        "price": price,
        "quantity": executed_vol,
        "total_amount": total,
        "fee": float(order.get("paid_fee") or 0),
        "currency": "KRW",
        "account": "upbit",
        "order_id": str(order.get("uuid", "")),
        "filled_at": str(order.get("created_at", "")),
    }


def _normalize_kis_domestic_filled(order: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a KIS domestic filled order."""
    qty = float(order.get("ccld_qty") or order.get("tot_ccld_qty") or 0)
    if qty <= 0:
        return None

    price = float(order.get("ccld_unpr") or order.get("avg_prvs") or 0)
    total = float(order.get("ccld_amt") or order.get("tot_ccld_amt") or price * qty)
    ord_dt = str(order.get("ord_dt", ""))
    ord_tmd = str(order.get("ord_tmd") or order.get("ccld_tmd") or "000000")

    filled_at_str = ""
    if len(ord_dt) == 8 and len(ord_tmd) >= 6:
        try:
            dt = datetime.strptime(f"{ord_dt} {ord_tmd[:6]}", "%Y%m%d %H%M%S")
            filled_at_str = dt.replace(tzinfo=KST).isoformat()
        except ValueError:
            filled_at_str = ord_dt

    symbol = str(order.get("pdno") or order.get("stck_code") or "").strip()
    side_code = str(order.get("sll_buy_dvsn_cd") or "")

    return {
        "symbol": symbol,
        "raw_symbol": symbol,
        "instrument_type": "equity_kr",
        "side": "sell" if side_code == "01" else "buy",
        "price": price,
        "quantity": qty,
        "total_amount": total,
        "fee": 0,  # KIS doesn't return fee in daily order response
        "currency": "KRW",
        "account": "kis",
        "order_id": str(order.get("ord_no") or order.get("odno") or ""),
        "filled_at": filled_at_str,
    }


def _normalize_kis_overseas_filled(order: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a KIS overseas filled order."""
    qty = float(order.get("ft_ccld_qty") or order.get("ccld_qty") or 0)
    if qty <= 0:
        return None

    price = float(order.get("ft_ccld_unpr3") or order.get("ccld_unpr") or 0)
    total = float(order.get("ft_ccld_amt3") or order.get("ccld_amt") or price * qty)
    ord_dt = str(order.get("ord_dt", ""))
    ord_tmd = str(order.get("ord_tmd") or "000000")

    filled_at_str = ""
    if len(ord_dt) == 8 and len(ord_tmd) >= 6:
        try:
            dt = datetime.strptime(f"{ord_dt} {ord_tmd[:6]}", "%Y%m%d %H%M%S")
            filled_at_str = dt.replace(tzinfo=KST).isoformat()
        except ValueError:
            filled_at_str = ord_dt

    symbol = str(order.get("pdno") or order.get("symb") or "").strip()

    return {
        "symbol": symbol,
        "raw_symbol": symbol,
        "instrument_type": "equity_us",
        "side": "sell" if str(order.get("sll_buy_dvsn_cd", "")) == "01" else "buy",
        "price": price,
        "quantity": qty,
        "total_amount": total,
        "fee": 0,
        "currency": "USD",
        "account": "kis_overseas",
        "order_id": str(order.get("odno") or order.get("ord_no") or ""),
        "filled_at": filled_at_str,
    }


async def _fetch_upbit_filled(days: int) -> tuple[list[dict], list[dict]]:
    """Fetch filled orders from Upbit. Returns (orders, errors)."""
    try:
        closed = await upbit_service.fetch_closed_orders(market=None, limit=100)
        orders = []
        for raw in closed:
            normalized = _normalize_upbit_filled(raw)
            if normalized:
                orders.append(normalized)
        return orders, []
    except Exception as exc:
        logger.warning("Upbit filled-orders fetch failed: %s", exc)
        return [], [{"market": "crypto", "error": str(exc)}]


async def _fetch_kis_domestic_filled(days: int) -> tuple[list[dict], list[dict]]:
    """Fetch filled orders from KIS domestic."""
    try:
        kis = KISClient()
        end_date = now_kst().strftime("%Y%m%d")
        start_date = (now_kst() - timedelta(days=days)).strftime("%Y%m%d")
        raw_orders = await kis.inquire_daily_order_domestic(
            start_date=start_date, end_date=end_date, stock_code="", side="00"
        )
        orders = []
        for raw in (raw_orders or []):
            normalized = _normalize_kis_domestic_filled(raw)
            if normalized:
                orders.append(normalized)
        return orders, []
    except Exception as exc:
        logger.warning("KIS domestic filled-orders fetch failed: %s", exc)
        return [], [{"market": "kr", "error": str(exc)}]


async def _fetch_kis_overseas_filled(days: int) -> tuple[list[dict], list[dict]]:
    """Fetch filled orders from KIS overseas."""
    try:
        kis = KISClient()
        end_date = now_kst().strftime("%Y%m%d")
        start_date = (now_kst() - timedelta(days=days)).strftime("%Y%m%d")
        raw_orders = await kis.inquire_daily_order_overseas(
            start_date=start_date, end_date=end_date, symbol="", exchange_code=""
        )
        orders = []
        for raw in (raw_orders or []):
            normalized = _normalize_kis_overseas_filled(raw)
            if normalized:
                orders.append(normalized)
        return orders, []
    except Exception as exc:
        logger.warning("KIS overseas filled-orders fetch failed: %s", exc)
        return [], [{"market": "us", "error": str(exc)}]


async def _enrich_with_current_prices(
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add current_price and pnl_pct to each order."""
    # Deduplicate symbols per market for batch fetching
    seen: dict[str, float | None] = {}
    sem = asyncio.Semaphore(_EQUITY_QUOTE_CONCURRENCY)

    # Batch fetch crypto prices
    crypto_symbols = list({
        o["raw_symbol"] for o in orders if o["instrument_type"] == "crypto"
    })
    if crypto_symbols:
        try:
            prices = await upbit_service.fetch_multiple_current_prices_cached(
                crypto_symbols
            )
            for sym, price in prices.items():
                seen[sym] = price
        except Exception as exc:
            logger.warning("Crypto batch price fetch failed: %s", exc)

    # Fetch equity prices with concurrency limit
    equity_symbols = list({
        (o["raw_symbol"], o["instrument_type"])
        for o in orders
        if o["instrument_type"] in ("equity_kr", "equity_us")
    })

    async def _fetch_one(symbol: str, itype: str) -> None:
        if symbol in seen:
            return
        async with sem:
            try:
                market = "kr" if itype == "equity_kr" else "us"
                quote = await get_quote(symbol, market=market)
                seen[symbol] = float(quote.get("price") or quote.get("current_price") or 0) or None
            except Exception as exc:
                logger.warning("Quote fetch failed for %s: %s", symbol, exc)
                seen[symbol] = None

    await asyncio.gather(
        *[_fetch_one(sym, itype) for sym, itype in equity_symbols],
        return_exceptions=True,
    )

    # Enrich orders
    for order in orders:
        raw_sym = order["raw_symbol"]
        cp = seen.get(raw_sym)
        order["current_price"] = cp
        if cp and order["price"] and order["side"] == "buy":
            pnl = ((cp - order["price"]) / order["price"]) * 100
            order["pnl_pct"] = round(pnl, 2)
            sign = "+" if pnl >= 0 else ""
            order["pnl_pct_fmt"] = f"{sign}{pnl:.2f}%"
        elif cp and order["price"] and order["side"] == "sell":
            pnl = ((order["price"] - cp) / order["price"]) * 100
            order["pnl_pct"] = round(pnl, 2)
            sign = "+" if pnl >= 0 else ""
            order["pnl_pct_fmt"] = f"{sign}{pnl:.2f}%"
        else:
            order["pnl_pct"] = None
            order["pnl_pct_fmt"] = None

    return orders


async def fetch_filled_orders(
    days: int = 1,
    markets: str = "crypto,kr,us",
    min_amount: float = 0,
) -> dict[str, Any]:
    """
    Fetch filled orders across specified markets.

    Args:
        days: Lookback period in days.
        markets: Comma-separated market list.
        min_amount: Minimum filled amount in order currency.

    Returns:
        Dict with "orders" and "errors" keys.
    """
    market_set = {m.strip().lower() for m in markets.split(",") if m.strip()}
    all_orders: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []

    # Parallel fetch from all requested markets
    tasks = []
    if "crypto" in market_set:
        tasks.append(_fetch_upbit_filled(days))
    if "kr" in market_set:
        tasks.append(_fetch_kis_domestic_filled(days))
    if "us" in market_set:
        tasks.append(_fetch_kis_overseas_filled(days))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            all_errors.append({"error": str(result)})
        else:
            orders, errors = result
            all_orders.extend(orders)
            all_errors.extend(errors)

    # Filter by min_amount
    if min_amount > 0:
        all_orders = [o for o in all_orders if o.get("total_amount", 0) >= min_amount]

    # Enrich with current prices
    if all_orders:
        all_orders = await _enrich_with_current_prices(all_orders)

    # Sort by filled_at descending
    all_orders.sort(key=lambda o: o.get("filled_at", ""), reverse=True)

    return {"orders": all_orders, "errors": all_errors}
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_n8n_trade_review.py::TestFilledOrdersService -v`

Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add app/services/n8n_filled_orders_service.py tests/test_n8n_trade_review.py
git commit -m "feat(n8n): add filled-orders service with broker integration"
```

---

### Task 3: Trade Review Service (DB Write Operations)

**Files:**
- Create: `app/services/n8n_trade_review_service.py`
- Modify: `tests/test_n8n_trade_review.py` (add new test class)

**Step 1: Write tests for trade-review UPSERT**

Add to `tests/test_n8n_trade_review.py`:

```python
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.core.timezone import KST


@pytest.mark.unit
class TestTradeReviewService:
    """Tests for save_trade_reviews service function."""

    def _make_review_item(self, **overrides):
        base = {
            "order_id": "test-order-001",
            "account": "upbit",
            "symbol": "BTC",
            "instrument_type": "crypto",
            "side": "buy",
            "price": 98000000,
            "quantity": 0.015,
            "total_amount": 1470000,
            "fee": 735,
            "currency": "KRW",
            "filled_at": "2026-03-17T14:30:00+09:00",
            "price_at_review": 101200000,
            "pnl_pct": 3.27,
            "verdict": "good",
            "comment": "RSI 31 매수, 적절한 타이밍",
            "review_type": "daily",
            "indicators": {
                "rsi_14": 31.2,
                "rsi_7": 28.5,
                "ema_200": 95000000,
                "adx": 42.1,
                "volume_ratio": 1.8,
                "fear_greed": 25,
            },
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_rejects_null_order_id(self):
        """Items with null order_id are rejected (not saved)."""
        from app.services.n8n_trade_review_service import save_trade_reviews

        mock_session = AsyncMock()
        item = self._make_review_item(order_id=None)

        result = await save_trade_reviews(mock_session, [item])

        assert result["saved_count"] == 0
        assert result["skipped_count"] == 0
        assert len(result["errors"]) == 1
        assert "order_id" in result["errors"][0]["error"].lower()

    @pytest.mark.asyncio
    async def test_saves_trade_with_snapshot_and_review(self):
        """Valid review item creates trade + snapshot + review rows."""
        from app.services.n8n_trade_review_service import save_trade_reviews

        mock_session = AsyncMock()
        # Simulate no existing trade (INSERT succeeds)
        mock_result = MagicMock()
        mock_result.inserted_primary_key = (42,)
        mock_session.execute = AsyncMock(return_value=mock_result)
        # Simulate scalars().first() for duplicate check
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_session.scalars = AsyncMock(return_value=mock_scalars)

        item = self._make_review_item()
        result = await save_trade_reviews(mock_session, [item])

        assert result["saved_count"] == 1
        assert result["skipped_count"] == 0
        mock_session.commit.assert_awaited_once()
```

**Step 2: Implement trade-review service**

Create `app/services/n8n_trade_review_service.py`:

```python
"""Trade review service — UPSERT trades, INSERT snapshots/reviews, compute stats."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST, now_kst
from app.models.review import PendingSnapshot, Trade, TradeReview, TradeSnapshot

logger = logging.getLogger(__name__)

# InstrumentType string → enum mapping
_INSTRUMENT_MAP = {
    "crypto": "crypto",
    "equity_kr": "equity_kr",
    "equity_us": "equity_us",
    "kr": "equity_kr",
    "us": "equity_us",
}


async def save_trade_reviews(
    session: AsyncSession,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Save trade reviews to DB.

    For each item:
    1. UPSERT into review.trades (skip on conflict)
    2. INSERT into review.trade_snapshots (if indicators provided)
    3. INSERT into review.trade_reviews (skip if same review_type exists)

    Args:
        session: Async DB session (caller must NOT commit — this function commits).
        items: List of review item dicts matching N8nTradeReviewItem schema.

    Returns:
        Dict with saved_count, skipped_count, errors.
    """
    saved = 0
    skipped = 0
    errors: list[dict[str, Any]] = []

    for item in items:
        order_id = item.get("order_id")
        if not order_id:
            errors.append({
                "order_id": order_id,
                "error": "order_id is required (null not allowed)",
            })
            continue

        try:
            # 1. UPSERT trade
            instrument = _INSTRUMENT_MAP.get(
                item.get("instrument_type", ""), item.get("instrument_type", "")
            )

            filled_at_str = item.get("filled_at", "")
            try:
                trade_date = datetime.fromisoformat(filled_at_str.replace("Z", "+00:00"))
                if trade_date.tzinfo is None:
                    trade_date = trade_date.replace(tzinfo=KST)
            except (ValueError, AttributeError):
                trade_date = now_kst()

            stmt = pg_insert(Trade).values(
                trade_date=trade_date,
                symbol=item.get("symbol", ""),
                instrument_type=instrument,
                side=item.get("side", "buy"),
                price=item.get("price", 0),
                quantity=item.get("quantity", 0),
                total_amount=item.get("total_amount", 0),
                fee=item.get("fee", 0),
                currency=item.get("currency", "KRW"),
                account=item.get("account", ""),
                order_id=order_id,
            ).on_conflict_do_nothing(
                constraint="uq_review_trades_account_order",
            )

            result = await session.execute(stmt)
            await session.flush()

            # Get trade_id (either newly inserted or existing)
            trade_id: int | None = None
            if result.inserted_primary_key and result.inserted_primary_key[0]:
                trade_id = result.inserted_primary_key[0]
            else:
                # Trade already existed — look up its ID
                existing = await session.scalars(
                    select(Trade.id).where(
                        Trade.account == item.get("account", ""),
                        Trade.order_id == order_id,
                    )
                )
                trade_id = existing.first()
                if not trade_id:
                    skipped += 1
                    continue

            # 2. INSERT snapshot (if indicators provided)
            indicators = item.get("indicators")
            if indicators and isinstance(indicators, dict):
                # Check if snapshot already exists for this trade
                existing_snap = await session.scalars(
                    select(TradeSnapshot.id).where(TradeSnapshot.trade_id == trade_id)
                )
                if not existing_snap.first():
                    snapshot = TradeSnapshot(
                        trade_id=trade_id,
                        rsi_14=indicators.get("rsi_14"),
                        rsi_7=indicators.get("rsi_7"),
                        ema_20=indicators.get("ema_20"),
                        ema_200=indicators.get("ema_200"),
                        macd=indicators.get("macd"),
                        macd_signal=indicators.get("macd_signal"),
                        adx=indicators.get("adx"),
                        stoch_rsi_k=indicators.get("stoch_rsi_k"),
                        volume_ratio=indicators.get("volume_ratio"),
                        fear_greed=indicators.get("fear_greed"),
                    )
                    session.add(snapshot)

            # 3. INSERT review (skip if same review_type exists for this trade)
            review_type = item.get("review_type", "daily")
            existing_review = await session.scalars(
                select(TradeReview.id).where(
                    TradeReview.trade_id == trade_id,
                    TradeReview.review_type == review_type,
                )
            )
            if not existing_review.first():
                review = TradeReview(
                    trade_id=trade_id,
                    review_date=now_kst(),
                    price_at_review=item.get("price_at_review"),
                    pnl_pct=item.get("pnl_pct"),
                    verdict=item.get("verdict", "neutral"),
                    comment=item.get("comment"),
                    review_type=review_type,
                )
                session.add(review)
                saved += 1
            else:
                skipped += 1

        except Exception as exc:
            logger.warning("Failed to save review for order %s: %s", order_id, exc)
            errors.append({"order_id": order_id, "error": str(exc)})
            continue

    await session.commit()
    return {"saved_count": saved, "skipped_count": skipped, "errors": errors}


async def get_trade_review_stats(
    session: AsyncSession,
    period: str = "week",
    market: str | None = None,
) -> dict[str, Any]:
    """
    Compute aggregate stats for trade reviews.

    Uses AT TIME ZONE 'Asia/Seoul' for correct KST date boundaries.
    """
    # Determine period boundaries in KST
    now = now_kst()
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "quarter":
        quarter_month = ((now.month - 1) // 3) * 3 + 1
        start = now.replace(
            month=quarter_month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        start = (now - timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    period_label = f"{start.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}"

    # Base query: join trades with reviews
    base_filter = [
        Trade.trade_date >= start,
        Trade.trade_date <= now,
    ]
    if market:
        itype = _INSTRUMENT_MAP.get(market, market)
        base_filter.append(Trade.instrument_type == itype)

    # Fetch all reviews in period with their trades
    stmt = (
        select(Trade, TradeReview)
        .join(TradeReview, Trade.id == TradeReview.trade_id)
        .where(*base_filter)
        .order_by(TradeReview.pnl_pct.desc().nulls_last())
    )
    result = await session.execute(stmt)
    rows = result.all()

    if not rows:
        return {
            "period": period_label,
            "total_trades": 0,
            "buy_count": 0,
            "sell_count": 0,
            "win_rate": None,
            "avg_pnl_pct": None,
            "best_trade": None,
            "worst_trade": None,
            "by_verdict": {},
            "by_rsi_zone": {},
        }

    trades_data = []
    for trade, review in rows:
        trades_data.append({
            "symbol": trade.symbol,
            "side": trade.side,
            "pnl_pct": float(review.pnl_pct) if review.pnl_pct is not None else None,
            "verdict": review.verdict,
            "trade_id": trade.id,
        })

    total = len(trades_data)
    buy_count = sum(1 for t in trades_data if t["side"] == "buy")
    sell_count = total - buy_count

    pnl_values = [t["pnl_pct"] for t in trades_data if t["pnl_pct"] is not None]
    wins = sum(1 for p in pnl_values if p > 0)
    win_rate = round((wins / len(pnl_values)) * 100, 1) if pnl_values else None
    avg_pnl = round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else None

    best = max(trades_data, key=lambda t: t.get("pnl_pct") or float("-inf"))
    worst = min(trades_data, key=lambda t: t.get("pnl_pct") or float("inf"))

    by_verdict: dict[str, int] = {}
    for t in trades_data:
        v = t.get("verdict", "neutral")
        by_verdict[v] = by_verdict.get(v, 0) + 1

    # RSI zone analysis — requires trade_snapshots join
    rsi_stmt = (
        select(TradeSnapshot.rsi_14, TradeReview.pnl_pct)
        .join(TradeReview, TradeSnapshot.trade_id == TradeReview.trade_id)
        .join(Trade, Trade.id == TradeSnapshot.trade_id)
        .where(*base_filter)
        .where(TradeSnapshot.rsi_14.is_not(None))
    )
    rsi_result = await session.execute(rsi_stmt)
    rsi_rows = rsi_result.all()

    zones: dict[str, list[float]] = {
        "oversold_lt30": [],
        "neutral_30_50": [],
        "overbought_gt50": [],
    }
    for rsi_val, pnl_val in rsi_rows:
        rsi_f = float(rsi_val)
        pnl_f = float(pnl_val) if pnl_val is not None else 0
        if rsi_f < 30:
            zones["oversold_lt30"].append(pnl_f)
        elif rsi_f <= 50:
            zones["neutral_30_50"].append(pnl_f)
        else:
            zones["overbought_gt50"].append(pnl_f)

    by_rsi_zone = {}
    for zone_name, pnl_list in zones.items():
        if pnl_list:
            zone_wins = sum(1 for p in pnl_list if p > 0)
            by_rsi_zone[zone_name] = {
                "count": len(pnl_list),
                "avg_pnl": round(sum(pnl_list) / len(pnl_list), 2),
                "win_rate": round((zone_wins / len(pnl_list)) * 100, 1),
            }

    return {
        "period": period_label,
        "total_trades": total,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "win_rate": win_rate,
        "avg_pnl_pct": avg_pnl,
        "best_trade": {"symbol": best["symbol"], "pnl_pct": best.get("pnl_pct")},
        "worst_trade": {"symbol": worst["symbol"], "pnl_pct": worst.get("pnl_pct")},
        "by_verdict": by_verdict,
        "by_rsi_zone": by_rsi_zone,
    }
```

**Step 3: Run tests**

Run: `uv run pytest tests/test_n8n_trade_review.py::TestTradeReviewService -v`

Expected: PASS

**Step 4: Commit**

```bash
git add app/services/n8n_trade_review_service.py tests/test_n8n_trade_review.py
git commit -m "feat(n8n): add trade review service with UPSERT and stats"
```

---

### Task 4: Pending Review Service

**Files:**
- Create: `app/services/n8n_pending_review_service.py`
- Modify: `tests/test_n8n_trade_review.py`

**Step 1: Write test**

Add to `tests/test_n8n_trade_review.py`:

```python
@pytest.mark.unit
class TestPendingReviewService:
    """Tests for pending-review fill_probability computation."""

    def test_fill_probability_high(self):
        from app.services.n8n_pending_review_service import compute_fill_probability

        assert compute_fill_probability(gap_pct=0.5, days_pending=1) == "high"

    def test_fill_probability_medium(self):
        from app.services.n8n_pending_review_service import compute_fill_probability

        assert compute_fill_probability(gap_pct=3.0, days_pending=1) == "medium"

    def test_fill_probability_low(self):
        from app.services.n8n_pending_review_service import compute_fill_probability

        assert compute_fill_probability(gap_pct=6.0, days_pending=2) == "low"

    def test_fill_probability_stale(self):
        from app.services.n8n_pending_review_service import compute_fill_probability

        assert compute_fill_probability(gap_pct=4.0, days_pending=6) == "stale"
```

**Step 2: Implement pending-review service**

Create `app/services/n8n_pending_review_service.py`:

```python
"""Pending-review service — wraps pending-orders with fill probability."""

from __future__ import annotations

import logging
from typing import Any

from app.core.timezone import now_kst
from app.services.n8n_pending_orders_service import fetch_pending_orders

logger = logging.getLogger(__name__)


def compute_fill_probability(gap_pct: float | None, days_pending: int) -> str:
    """
    Classify fill probability based on gap and age.

    Rules:
    - |gap| < 1%: high
    - |gap| 1-5%: medium
    - |gap| > 5%: low
    - days_pending > 5 AND |gap| > 3%: stale (overrides above)
    """
    abs_gap = abs(gap_pct or 0)

    # Stale check first (takes priority)
    if days_pending > 5 and abs_gap > 3:
        return "stale"

    if abs_gap < 1:
        return "high"
    elif abs_gap <= 5:
        return "medium"
    else:
        return "low"


def _suggestion_for(probability: str, side: str) -> str | None:
    """Generate Korean action suggestion."""
    suggestions = {
        "high": "곧 체결 예상 — 대기",
        "medium": "가격 조정 검토",
        "low": "체결 가능성 낮음 — 취소 또는 가격 조정",
        "stale": "장기 미체결 — 재검토 필요",
    }
    return suggestions.get(probability)


async def fetch_pending_review(
    market: str = "all",
    min_amount: float = 0,
) -> dict[str, Any]:
    """
    Fetch pending orders with fill probability classification.

    Wraps the existing fetch_pending_orders and adds computed fields.
    """
    as_of_dt = now_kst().replace(microsecond=0)

    result = await fetch_pending_orders(
        market=market,
        min_amount=min_amount,
        include_current_price=True,
        side=None,
        as_of=as_of_dt,
        attention_only=False,
        near_fill_pct=2.0,
    )

    enriched_orders = []
    for order in result.get("orders", []):
        gap_pct = order.get("gap_pct")
        days_pending = order.get("age_days", 0)

        probability = compute_fill_probability(gap_pct, days_pending)
        suggestion = _suggestion_for(probability, order.get("side", "buy"))

        enriched_orders.append({
            "order_id": order.get("order_id", ""),
            "symbol": order.get("symbol", ""),
            "raw_symbol": order.get("raw_symbol", ""),
            "market": order.get("market", ""),
            "side": order.get("side", ""),
            "order_price": order.get("order_price", 0),
            "current_price": order.get("current_price"),
            "gap_pct": gap_pct,
            "gap_pct_fmt": order.get("gap_pct_fmt"),
            "amount_krw": order.get("amount_krw"),
            "quantity": order.get("quantity", 0),
            "remaining_qty": order.get("remaining_qty", 0),
            "created_at": order.get("created_at", ""),
            "age_days": days_pending,
            "currency": order.get("currency", "KRW"),
            "days_pending": days_pending,
            "fill_probability": probability,
            "suggestion": suggestion,
        })

    return {
        "orders": enriched_orders,
        "errors": result.get("errors", []),
    }
```

**Step 3: Run tests**

Run: `uv run pytest tests/test_n8n_trade_review.py::TestPendingReviewService -v`

Expected: PASS

**Step 4: Commit**

```bash
git add app/services/n8n_pending_review_service.py tests/test_n8n_trade_review.py
git commit -m "feat(n8n): add pending-review service with fill probability"
```

---

### Task 5: Pending Snapshot Service (DB Write + Resolve)

**Files:**
- Create: `app/services/n8n_pending_snapshot_service.py`
- Modify: `tests/test_n8n_trade_review.py`

**Step 1: Write tests**

Add to `tests/test_n8n_trade_review.py`:

```python
@pytest.mark.unit
class TestPendingSnapshotService:
    """Tests for pending-snapshot save and resolve."""

    @pytest.mark.asyncio
    async def test_save_snapshots(self):
        from app.services.n8n_pending_snapshot_service import save_pending_snapshots

        mock_session = AsyncMock()

        items = [
            {
                "symbol": "BTC",
                "instrument_type": "crypto",
                "side": "buy",
                "order_price": 96500000,
                "quantity": 0.01,
                "current_price": 101200000,
                "gap_pct": -4.6,
                "days_pending": 3,
                "account": "upbit",
                "order_id": "xyz-456",
            }
        ]

        result = await save_pending_snapshots(mock_session, items)

        assert result["saved_count"] == 1
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolve_updates_matching_snapshots(self):
        from app.services.n8n_pending_snapshot_service import resolve_pending_snapshots

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute = AsyncMock(return_value=mock_result)

        resolutions = [
            {"order_id": "xyz-456", "account": "upbit", "resolved_as": "filled"}
        ]

        result = await resolve_pending_snapshots(mock_session, resolutions)

        assert result["resolved_count"] == 1
        mock_session.commit.assert_awaited_once()
```

**Step 2: Implement pending-snapshot service**

Create `app/services/n8n_pending_snapshot_service.py`:

```python
"""Pending snapshot service — save snapshots and resolve status."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import update

from app.core.timezone import now_kst
from app.models.review import PendingSnapshot

logger = logging.getLogger(__name__)

_INSTRUMENT_MAP = {
    "crypto": "crypto",
    "equity_kr": "equity_kr",
    "equity_us": "equity_us",
    "kr": "equity_kr",
    "us": "equity_us",
}

_VALID_RESOLUTIONS = {"filled", "cancelled", "expired"}


async def save_pending_snapshots(
    session: "AsyncSession",
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Save pending order snapshots to review.pending_snapshots."""
    saved = 0
    errors: list[dict[str, Any]] = []
    snapshot_date = now_kst()

    for item in items:
        try:
            instrument = _INSTRUMENT_MAP.get(
                item.get("instrument_type", ""), item.get("instrument_type", "")
            )
            snapshot = PendingSnapshot(
                snapshot_date=snapshot_date,
                symbol=item.get("symbol", ""),
                instrument_type=instrument,
                side=item.get("side", "buy"),
                order_price=item.get("order_price", 0),
                quantity=item.get("quantity", 0),
                current_price=item.get("current_price"),
                gap_pct=item.get("gap_pct"),
                days_pending=item.get("days_pending"),
                account=item.get("account", ""),
                order_id=item.get("order_id"),
                resolved_as="pending",
            )
            session.add(snapshot)
            saved += 1
        except Exception as exc:
            logger.warning("Failed to save snapshot: %s", exc)
            errors.append({
                "order_id": item.get("order_id"),
                "error": str(exc),
            })

    await session.commit()
    return {"saved_count": saved, "errors": errors}


async def resolve_pending_snapshots(
    session: "AsyncSession",
    resolutions: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Resolve pending snapshots by updating their status.

    Only updates the MOST RECENT unresolved snapshot for each (account, order_id).
    """
    resolved = 0
    not_found = 0
    errors: list[dict[str, Any]] = []
    resolved_at = now_kst()

    for item in resolutions:
        order_id = item.get("order_id")
        account = item.get("account")
        resolved_as = item.get("resolved_as", "")

        if resolved_as not in _VALID_RESOLUTIONS:
            errors.append({
                "order_id": order_id,
                "error": f"Invalid resolved_as: {resolved_as}. Must be one of {_VALID_RESOLUTIONS}",
            })
            continue

        try:
            stmt = (
                update(PendingSnapshot)
                .where(
                    PendingSnapshot.account == account,
                    PendingSnapshot.order_id == order_id,
                    PendingSnapshot.resolved_as == "pending",
                )
                .values(resolved_as=resolved_as, resolved_at=resolved_at)
            )
            result = await session.execute(stmt)

            if result.rowcount > 0:
                resolved += 1
            else:
                not_found += 1

        except Exception as exc:
            logger.warning("Failed to resolve snapshot %s: %s", order_id, exc)
            errors.append({"order_id": order_id, "error": str(exc)})

    await session.commit()
    return {
        "resolved_count": resolved,
        "not_found_count": not_found,
        "errors": errors,
    }
```

**Step 3: Run tests**

Run: `uv run pytest tests/test_n8n_trade_review.py::TestPendingSnapshotService -v`

Expected: PASS

**Step 4: Commit**

```bash
git add app/services/n8n_pending_snapshot_service.py tests/test_n8n_trade_review.py
git commit -m "feat(n8n): add pending-snapshot service with save and resolve"
```

---

### Task 6: Router Wiring (All 6 Endpoints)

**Files:**
- Modify: `app/routers/n8n.py`
- Modify: `tests/test_n8n_trade_review.py`

**Step 1: Write router integration tests**

Add to `tests/test_n8n_trade_review.py`:

```python
from fastapi.testclient import TestClient


@pytest.mark.unit
class TestN8nTradeReviewRoutes:
    """Smoke tests for all 6 new n8n router endpoints."""

    @pytest.fixture
    def client(self):
        from app.main import create_app

        app = create_app()
        return TestClient(app)

    @pytest.mark.asyncio
    async def test_filled_orders_endpoint_returns_200(self, client):
        with patch(
            "app.routers.n8n.fetch_filled_orders",
            new_callable=AsyncMock,
            return_value={"orders": [], "errors": []},
        ):
            resp = client.get("/api/n8n/filled-orders?days=1&markets=crypto")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["orders"] == []

    @pytest.mark.asyncio
    async def test_trade_reviews_post_returns_200(self, client):
        with (
            patch(
                "app.routers.n8n.save_trade_reviews",
                new_callable=AsyncMock,
                return_value={"saved_count": 1, "skipped_count": 0, "errors": []},
            ),
            patch("app.routers.n8n.get_db"),
        ):
            resp = client.post(
                "/api/n8n/trade-reviews",
                json={
                    "reviews": [
                        {
                            "order_id": "test-001",
                            "account": "upbit",
                            "symbol": "BTC",
                            "instrument_type": "crypto",
                            "side": "buy",
                            "price": 98000000,
                            "quantity": 0.015,
                            "total_amount": 1470000,
                            "filled_at": "2026-03-17T14:30:00+09:00",
                            "verdict": "good",
                        }
                    ]
                },
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_trade_reviews_stats_returns_200(self, client):
        with (
            patch(
                "app.routers.n8n.get_trade_review_stats",
                new_callable=AsyncMock,
                return_value={
                    "period": "2026-03-10 ~ 2026-03-17",
                    "total_trades": 0,
                    "buy_count": 0,
                    "sell_count": 0,
                    "win_rate": None,
                    "avg_pnl_pct": None,
                    "best_trade": None,
                    "worst_trade": None,
                    "by_verdict": {},
                    "by_rsi_zone": {},
                },
            ),
            patch("app.routers.n8n.get_db"),
        ):
            resp = client.get("/api/n8n/trade-reviews/stats?period=week")
            assert resp.status_code == 200
```

**Step 2: Add all 6 endpoints to router**

Modify `app/routers/n8n.py` — add these imports at the top:

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.schemas.n8n import (
    # ...existing imports...,
    N8nFilledOrdersResponse,
    N8nPendingResolveRequest,
    N8nPendingResolveResponse,
    N8nPendingReviewResponse,
    N8nPendingSnapshotsRequest,
    N8nPendingSnapshotsResponse,
    N8nTradeReviewsRequest,
    N8nTradeReviewsResponse,
    N8nTradeReviewStats,
    N8nTradeReviewStatsResponse,
)
from app.services.n8n_filled_orders_service import fetch_filled_orders
from app.services.n8n_pending_review_service import fetch_pending_review
from app.services.n8n_pending_snapshot_service import (
    resolve_pending_snapshots,
    save_pending_snapshots,
)
from app.services.n8n_trade_review_service import (
    get_trade_review_stats,
    save_trade_reviews,
)
```

Add these endpoint functions after the existing `get_market_context` handler:

```python
@router.get("/filled-orders", response_model=N8nFilledOrdersResponse)
async def get_filled_orders(
    days: int = Query(1, ge=1, le=90, description="Lookback period in days"),
    markets: str = Query("crypto,kr,us", description="Comma-separated markets"),
    min_amount: float = Query(0, ge=0, description="Minimum filled amount"),
) -> N8nFilledOrdersResponse | JSONResponse:
    as_of = now_kst().replace(microsecond=0).isoformat()
    try:
        result = await fetch_filled_orders(
            days=days, markets=markets, min_amount=min_amount
        )
    except Exception as exc:
        logger.exception("Failed to fetch filled orders")
        payload = N8nFilledOrdersResponse(
            success=False, as_of=as_of, total_count=0, orders=[], errors=[{"error": str(exc)}]
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nFilledOrdersResponse(
        success=True,
        as_of=as_of,
        total_count=len(result["orders"]),
        orders=result["orders"],
        errors=result["errors"],
    )


@router.post("/trade-reviews", response_model=N8nTradeReviewsResponse)
async def post_trade_reviews(
    body: N8nTradeReviewsRequest,
    db: AsyncSession = Depends(get_db),
) -> N8nTradeReviewsResponse | JSONResponse:
    try:
        result = await save_trade_reviews(db, [r.model_dump() for r in body.reviews])
    except Exception as exc:
        logger.exception("Failed to save trade reviews")
        return JSONResponse(
            status_code=500,
            content=N8nTradeReviewsResponse(
                success=False, saved_count=0, errors=[{"error": str(exc)}]
            ).model_dump(),
        )

    return N8nTradeReviewsResponse(
        success=True,
        saved_count=result["saved_count"],
        skipped_count=result["skipped_count"],
        errors=result["errors"],
    )


@router.get("/trade-reviews/stats", response_model=N8nTradeReviewStatsResponse)
async def get_trade_review_stats_endpoint(
    period: str = Query("week", description="week, month, quarter"),
    market: str | None = Query(None, description="Filter by market"),
    db: AsyncSession = Depends(get_db),
) -> N8nTradeReviewStatsResponse | JSONResponse:
    try:
        stats = await get_trade_review_stats(db, period=period, market=market)
    except Exception as exc:
        logger.exception("Failed to get trade review stats")
        return JSONResponse(
            status_code=500,
            content=N8nTradeReviewStatsResponse(
                success=False,
                stats=N8nTradeReviewStats(period="error", total_trades=0),
                errors=[{"error": str(exc)}],
            ).model_dump(),
        )

    return N8nTradeReviewStatsResponse(
        success=True,
        stats=N8nTradeReviewStats(**stats),
        errors=[],
    )


@router.get("/pending-review", response_model=N8nPendingReviewResponse)
async def get_pending_review(
    market: str = Query("all", description="Market filter"),
    min_amount: float = Query(0, ge=0, description="Minimum KRW amount"),
) -> N8nPendingReviewResponse | JSONResponse:
    as_of = now_kst().replace(microsecond=0).isoformat()
    try:
        result = await fetch_pending_review(market=market, min_amount=min_amount)
    except Exception as exc:
        logger.exception("Failed to fetch pending review")
        payload = N8nPendingReviewResponse(
            success=False, as_of=as_of, total_count=0, orders=[], errors=[{"error": str(exc)}]
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nPendingReviewResponse(
        success=True,
        as_of=as_of,
        total_count=len(result["orders"]),
        orders=result["orders"],
        errors=result["errors"],
    )


@router.post("/pending-snapshots", response_model=N8nPendingSnapshotsResponse)
async def post_pending_snapshots(
    body: N8nPendingSnapshotsRequest,
    db: AsyncSession = Depends(get_db),
) -> N8nPendingSnapshotsResponse | JSONResponse:
    try:
        result = await save_pending_snapshots(db, [s.model_dump() for s in body.snapshots])
    except Exception as exc:
        logger.exception("Failed to save pending snapshots")
        return JSONResponse(
            status_code=500,
            content=N8nPendingSnapshotsResponse(
                success=False, saved_count=0, errors=[{"error": str(exc)}]
            ).model_dump(),
        )

    return N8nPendingSnapshotsResponse(
        success=True,
        saved_count=result["saved_count"],
        errors=result["errors"],
    )


@router.patch("/pending-snapshots/resolve", response_model=N8nPendingResolveResponse)
async def patch_pending_resolve(
    body: N8nPendingResolveRequest,
    db: AsyncSession = Depends(get_db),
) -> N8nPendingResolveResponse | JSONResponse:
    try:
        result = await resolve_pending_snapshots(
            db, [r.model_dump() for r in body.resolutions]
        )
    except Exception as exc:
        logger.exception("Failed to resolve pending snapshots")
        return JSONResponse(
            status_code=500,
            content=N8nPendingResolveResponse(
                success=False, resolved_count=0, errors=[{"error": str(exc)}]
            ).model_dump(),
        )

    return N8nPendingResolveResponse(
        success=True,
        resolved_count=result["resolved_count"],
        not_found_count=result["not_found_count"],
        errors=result["errors"],
    )
```

**Step 3: Run all tests**

Run: `uv run pytest tests/test_n8n_trade_review.py -v`

Expected: All tests PASS

**Step 4: Run linter**

Run: `make lint`

Expected: No new errors

**Step 5: Commit**

```bash
git add app/routers/n8n.py tests/test_n8n_trade_review.py
git commit -m "feat(n8n): wire all 6 trade review endpoints into router"
```

---

### Task 7: Final Verification

**Step 1: Run full test suite**

Run: `make test`

Expected: All existing tests pass. No regressions.

**Step 2: Verify lint/type check**

Run: `make lint`

Expected: Clean

**Step 3: Verify API docs render**

Run: `uv run python -c "from app.main import create_app; app = create_app(); print([r.path for r in app.routes if '/n8n/' in getattr(r, 'path', '')])"`

Expected: List includes all 8 n8n endpoints (2 existing + 6 new)

**Step 4: Final commit (if any cleanup needed)**

```bash
git add -A
git commit -m "chore: cleanup and verify trade review endpoints"
```

---

## Endpoint Summary

| # | Method | Path | Service File | DB? |
|---|--------|------|-------------|-----|
| 1 | GET | `/api/n8n/filled-orders` | `n8n_filled_orders_service.py` | No |
| 2 | POST | `/api/n8n/trade-reviews` | `n8n_trade_review_service.py` | Yes (UPSERT + INSERT) |
| 3 | GET | `/api/n8n/trade-reviews/stats` | `n8n_trade_review_service.py` | Yes (SELECT aggregate) |
| 4 | GET | `/api/n8n/pending-review` | `n8n_pending_review_service.py` | No |
| 5 | POST | `/api/n8n/pending-snapshots` | `n8n_pending_snapshot_service.py` | Yes (INSERT) |
| 6 | PATCH | `/api/n8n/pending-snapshots/resolve` | `n8n_pending_snapshot_service.py` | Yes (UPDATE) |

## Known Limitations

1. **KIS daily order API pagination**: Max 10 pages. High-volume traders may not see all fills for long periods.
2. **Upbit `fetch_closed_orders` limit**: Max 100 orders per call. For `days > 7`, some fills may be missed.
3. **KIS fee not in daily order response**: `fee` field defaults to 0 for KIS orders. n8n can enrich if needed.
4. **No n8n workflow JSON in this plan**: n8n workflows are created in the n8n UI. This plan covers the API side only. See the spec document for workflow definitions.
