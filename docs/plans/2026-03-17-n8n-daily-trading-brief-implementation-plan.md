# n8n Daily Trading Brief Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `GET /api/n8n/daily-brief` that returns a unified morning trading briefing — crypto + KR + US pending orders, market context, portfolio summary, yesterday fills, and a pre-formatted `brief_text` for Discord delivery.

**Architecture:** New service `n8n_daily_brief_service.py` orchestrates existing services (`fetch_pending_orders`, `fetch_market_context`, `PortfolioOverviewService`) in parallel via `asyncio.gather`, adds a new `_fetch_yesterday_fills` helper, and generates `brief_text`. Thin router endpoint in `n8n.py`, Pydantic schemas in `schemas/n8n.py`.

**Tech Stack:** FastAPI, Pydantic, asyncio, existing broker clients (Upbit, KIS), existing external services (fear/greed, BTC dominance, economic calendar).

---

## Pre-Implementation Notes

### Design Constraint: Yesterday Fills

`get_order_history_impl` requires `symbol` when `status != "pending"`. For the daily brief we need **all** recent fills across all markets without knowing symbols upfront.

**Approach:** Create `_fetch_yesterday_fills` in the daily brief service that directly queries each broker's filled-order API:
- **Crypto:** `get_order_history_impl(status="pending", ...)` won't work. Instead, use the Upbit client's order listing with `state=done` filter plus date filter.
- **KR/US:** Use KIS client order history APIs with date-range query.

Since the Upbit client exposes `fetch_my_orders(symbol, state)` which DOES require a symbol, and KIS similarly, we'll take a pragmatic approach:
1. Collect known symbols from holdings (portfolio overview)
2. Query filled orders per-symbol with `days=1` filter
3. Parallelize with `asyncio.gather` + semaphore

This is N+1 but bounded by portfolio size (~20-30 symbols max). Acceptable for a once-daily brief.

### Design Constraint: DB Session

`PortfolioOverviewService` requires `AsyncSession`. The n8n router currently has no DB dependency. We need to add it via `app/core/db.AsyncSessionLocal` context manager.

### Performance

All data fetches (`pending_orders`, `market_context`, `portfolio_overview`, `yesterday_fills`) will run in parallel via `asyncio.gather` since they're independent.

---

## Task 1: Add Daily Brief Schema Models

**Files:**
- Modify: `app/schemas/n8n.py`

**Step 1: Add the new Pydantic models at the end of `app/schemas/n8n.py`**

Add these models (before the final `N8nMarketContextResponse` class or at the end of the file):

```python
class N8nDailyBriefPendingMarket(BaseModel):
    """Per-market pending order summary for the daily brief."""
    total: int = Field(0, description="Total pending orders in this market")
    buy_count: int = Field(0, description="Pending buy orders")
    sell_count: int = Field(0, description="Pending sell orders")
    total_buy_fmt: str | None = Field(None, description="Formatted total buy amount")
    total_sell_fmt: str | None = Field(None, description="Formatted total sell amount")
    near_fill_count: int = Field(0, description="Orders near fill threshold")
    needs_attention_count: int = Field(0, description="Orders needing attention")
    orders: list[N8nPendingOrderItem] = Field(default_factory=list)


class N8nDailyBriefPendingOrders(BaseModel):
    """Aggregated pending orders across all markets."""
    crypto: N8nDailyBriefPendingMarket | None = Field(None)
    kr: N8nDailyBriefPendingMarket | None = Field(None)
    us: N8nDailyBriefPendingMarket | None = Field(None)


class N8nPortfolioMarketSummary(BaseModel):
    """Per-market portfolio summary."""
    total_value_krw: float | None = Field(None, description="Total value in KRW")
    total_value_usd: float | None = Field(None, description="Total value in USD (US only)")
    total_value_fmt: str | None = Field(None, description="Formatted total value")
    pnl_pct: float | None = Field(None, description="Overall P&L percentage")
    pnl_fmt: str | None = Field(None, description="Formatted P&L")
    position_count: int = Field(0, description="Number of positions")
    top_gainers: list[dict[str, object]] = Field(default_factory=list)
    top_losers: list[dict[str, object]] = Field(default_factory=list)


class N8nDailyBriefPortfolio(BaseModel):
    """Portfolio summary across all markets."""
    crypto: N8nPortfolioMarketSummary | None = Field(None)
    kr: N8nPortfolioMarketSummary | None = Field(None)
    us: N8nPortfolioMarketSummary | None = Field(None)


class N8nFillItem(BaseModel):
    """Single filled order for the daily brief."""
    symbol: str = Field(..., description="Symbol")
    market: str = Field(..., description="Market: crypto, kr, us")
    side: str = Field(..., description="buy or sell")
    price_fmt: str = Field(..., description="Formatted fill price")
    amount_fmt: str = Field(..., description="Formatted fill amount")
    time: str = Field(..., description="Fill time HH:MM")


class N8nYesterdayFills(BaseModel):
    """Yesterday's filled orders summary."""
    total: int = Field(0, description="Total fills")
    fills: list[N8nFillItem] = Field(default_factory=list)


class N8nDailyBriefResponse(BaseModel):
    """Daily trading brief response."""
    success: bool = Field(..., description="Whether request completed successfully")
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    date_fmt: str = Field(..., description="Date formatted as MM/DD (요일)")

    market_overview: N8nMarketOverview = Field(..., description="Market-wide context")
    pending_orders: N8nDailyBriefPendingOrders = Field(
        ..., description="Per-market pending orders"
    )
    portfolio_summary: N8nDailyBriefPortfolio = Field(
        ..., description="Per-market portfolio"
    )
    yesterday_fills: N8nYesterdayFills = Field(
        ..., description="Yesterday's filled orders"
    )

    brief_text: str = Field(..., description="Pre-formatted briefing text for Discord")
    errors: list[dict[str, object]] = Field(default_factory=list)

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "as_of": "2026-03-17T08:30:00+09:00",
                "date_fmt": "03/17 (월)",
                "brief_text": "📋 Daily Trading Brief — 03/17 (월)\n...",
            }
        }
    )
```

**Step 2: Commit**

```bash
git add app/schemas/n8n.py
git commit -m "feat(n8n): add daily brief schema models"
```

---

## Task 2: Add Formatting Helpers

**Files:**
- Create: `tests/test_n8n_daily_brief_formatting.py`
- Modify: `app/services/n8n_formatting.py`

**Step 1: Write the failing tests**

```python
# tests/test_n8n_daily_brief_formatting.py
from __future__ import annotations

import pytest

from app.services.n8n_formatting import (
    fmt_amount,
    fmt_date_with_weekday,
    fmt_pnl,
    fmt_value,
)


@pytest.mark.unit
class TestDailyBriefFormatting:
    def test_fmt_date_with_weekday(self):
        from datetime import datetime
        from app.core.timezone import KST

        dt = datetime(2026, 3, 17, 8, 30, tzinfo=KST)
        assert fmt_date_with_weekday(dt) == "03/17 (화)"

    def test_fmt_date_with_weekday_sunday(self):
        from datetime import datetime
        from app.core.timezone import KST

        dt = datetime(2026, 3, 15, 8, 30, tzinfo=KST)
        assert fmt_date_with_weekday(dt) == "03/15 (일)"

    def test_fmt_value_krw_man(self):
        assert fmt_value(15_000_000, "KRW") == "1,500만"

    def test_fmt_value_krw_eok(self):
        assert fmt_value(150_000_000, "KRW") == "1.5억"

    def test_fmt_value_usd(self):
        assert fmt_value(42_000, "USD") == "$42,000"

    def test_fmt_value_none(self):
        assert fmt_value(None, "KRW") == "-"

    def test_fmt_pnl_negative(self):
        assert fmt_pnl(-5.2) == "-5.2%"

    def test_fmt_pnl_positive(self):
        assert fmt_pnl(3.1) == "+3.1%"

    def test_fmt_pnl_none(self):
        assert fmt_pnl(None) == "-"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_n8n_daily_brief_formatting.py -v`
Expected: FAIL — `ImportError: cannot import name 'fmt_date_with_weekday'`

**Step 3: Implement the formatting helpers in `app/services/n8n_formatting.py`**

Add these functions (before the `__all__` list):

```python
_WEEKDAY_KR = ("월", "화", "수", "목", "금", "토", "일")


def fmt_date_with_weekday(dt: datetime) -> str:
    """Format datetime as 'MM/DD (요일)' in Korean."""
    return f"{dt.strftime('%m/%d')} ({_WEEKDAY_KR[dt.weekday()]})"


def fmt_value(value: float | None, currency: str = "KRW") -> str:
    """Format portfolio value. KRW: 억/만 units. USD: $-prefixed."""
    if value is None:
        return "-"
    if currency == "USD":
        if value >= 1000:
            return f"${value:,.0f}"
        return f"${value:,.2f}"
    # KRW
    if value >= 100_000_000:
        eok = value / 100_000_000
        return f"{eok:,.1f}억"
    if value >= 10_000:
        man = value / 10_000
        return f"{man:,.0f}만"
    return f"{value:,.0f}"


def fmt_pnl(pct: float | None) -> str:
    """Format P&L percentage with sign."""
    if pct is None:
        return "-"
    if pct > 0:
        return f"+{pct:.1f}%"
    return f"{pct:.1f}%"
```

Update `__all__` to include the new functions:

```python
__all__ = [
    "fmt_price",
    "fmt_gap",
    "fmt_amount",
    "fmt_age",
    "fmt_date_with_weekday",
    "fmt_value",
    "fmt_pnl",
    "build_summary_line",
    "build_summary_title",
    "enrich_order_fmt",
    "enrich_summary_fmt",
]
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_n8n_daily_brief_formatting.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/n8n_formatting.py tests/test_n8n_daily_brief_formatting.py
git commit -m "feat(n8n): add daily brief formatting helpers"
```

---

## Task 3: Create Daily Brief Service

**Files:**
- Create: `app/services/n8n_daily_brief_service.py`
- Create: `tests/test_n8n_daily_brief_service.py`

### Step 1: Write the failing service test

```python
# tests/test_n8n_daily_brief_service.py
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.timezone import KST


def _fake_pending_result(market: str, orders: list | None = None) -> dict:
    return {
        "success": True,
        "market": market,
        "orders": orders or [],
        "summary": {
            "total": len(orders or []),
            "buy_count": 0,
            "sell_count": 0,
            "total_buy_krw": 0,
            "total_sell_krw": 0,
            "total_buy_fmt": None,
            "total_sell_fmt": None,
            "title": None,
            "near_fill_count": 0,
            "needs_attention_count": 0,
            "attention_orders_only": [],
        },
        "errors": [],
    }


def _fake_market_context() -> dict:
    from app.schemas.n8n import (
        N8nFearGreedData,
        N8nMarketContextSummary,
        N8nMarketOverview,
    )

    return {
        "market_overview": N8nMarketOverview(
            fear_greed=N8nFearGreedData(
                value=23, label="Fear", previous=20, trend="improving"
            ),
            btc_dominance=56.64,
            total_market_cap_change_24h=3.86,
            economic_events_today=[],
        ),
        "symbols": [],
        "summary": N8nMarketContextSummary(
            total_symbols=0,
            bullish_count=0,
            bearish_count=0,
            neutral_count=0,
            avg_rsi=None,
            market_sentiment="neutral",
        ),
        "errors": [],
    }


def _fake_portfolio_overview() -> dict:
    return {
        "success": True,
        "positions": [
            {
                "market_type": "CRYPTO",
                "symbol": "KRW-BTC",
                "name": "비트코인",
                "quantity": 0.1,
                "avg_price": 100_000_000,
                "current_price": 105_000_000,
                "evaluation": 10_500_000,
                "profit_loss": 500_000,
                "profit_rate": 0.05,
                "components": [],
            },
        ],
        "warnings": [],
    }


@pytest.mark.unit
class TestFetchDailyBrief:
    @pytest.mark.asyncio
    async def test_returns_success_structure(self):
        as_of = datetime(2026, 3, 17, 8, 30, tzinfo=KST)

        with (
            patch(
                "app.services.n8n_daily_brief_service.fetch_pending_orders",
                new_callable=AsyncMock,
                return_value=_fake_pending_result("crypto"),
            ),
            patch(
                "app.services.n8n_daily_brief_service.fetch_market_context",
                new_callable=AsyncMock,
                return_value=_fake_market_context(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._get_portfolio_overview",
                new_callable=AsyncMock,
                return_value=_fake_portfolio_overview(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._fetch_yesterday_fills",
                new_callable=AsyncMock,
                return_value={"total": 0, "fills": []},
            ),
        ):
            from app.services.n8n_daily_brief_service import fetch_daily_brief

            result = await fetch_daily_brief(
                markets=["crypto"],
                min_amount=50_000,
                as_of=as_of,
            )

        assert result["success"] is True
        assert result["date_fmt"] == "03/17 (화)"
        assert "market_overview" in result
        assert "pending_orders" in result
        assert "portfolio_summary" in result
        assert "yesterday_fills" in result
        assert "brief_text" in result
        assert isinstance(result["brief_text"], str)
        assert len(result["brief_text"]) > 0

    @pytest.mark.asyncio
    async def test_handles_partial_failures(self):
        as_of = datetime(2026, 3, 17, 8, 30, tzinfo=KST)

        with (
            patch(
                "app.services.n8n_daily_brief_service.fetch_pending_orders",
                new_callable=AsyncMock,
                side_effect=Exception("pending failed"),
            ),
            patch(
                "app.services.n8n_daily_brief_service.fetch_market_context",
                new_callable=AsyncMock,
                return_value=_fake_market_context(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._get_portfolio_overview",
                new_callable=AsyncMock,
                return_value=_fake_portfolio_overview(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._fetch_yesterday_fills",
                new_callable=AsyncMock,
                return_value={"total": 0, "fills": []},
            ),
        ):
            from app.services.n8n_daily_brief_service import fetch_daily_brief

            result = await fetch_daily_brief(
                markets=["crypto"],
                min_amount=50_000,
                as_of=as_of,
            )

        # Should still succeed with partial data
        assert result["success"] is True
        assert len(result["errors"]) > 0
```

### Step 2: Run test to verify it fails

Run: `uv run pytest tests/test_n8n_daily_brief_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.n8n_daily_brief_service'`

### Step 3: Implement the daily brief service

Create `app/services/n8n_daily_brief_service.py`:

```python
"""Daily trading brief service for n8n integration.

Aggregates pending orders, market context, portfolio summary, and yesterday's fills
into a single unified brief with pre-formatted text for Discord delivery.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.schemas.n8n import N8nMarketOverview
from app.services.n8n_formatting import (
    fmt_amount,
    fmt_date_with_weekday,
    fmt_gap,
    fmt_pnl,
    fmt_price,
    fmt_value,
)
from app.services.n8n_market_context_service import fetch_market_context
from app.services.n8n_pending_orders_service import fetch_pending_orders

logger = logging.getLogger(__name__)

_DEFAULT_MARKETS = ("crypto", "kr", "us")


async def _get_portfolio_overview(
    markets: list[str],
) -> dict[str, Any]:
    """Fetch portfolio overview using PortfolioOverviewService."""
    from app.services.portfolio_overview_service import PortfolioOverviewService

    async with AsyncSessionLocal() as session:
        service = PortfolioOverviewService(session)
        return await service.get_overview(user_id=1)


async def _fetch_yesterday_fills(
    markets: list[str],
) -> dict[str, Any]:
    """Fetch yesterday's filled orders across requested markets.

    Since get_order_history_impl requires a symbol for non-pending queries,
    we collect known symbols from holdings and query per-symbol with days=1.
    Falls back gracefully on failure.
    """
    from app.mcp_server.tooling.orders_history import get_order_history_impl

    fills: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    # Collect symbols from pending orders (which doesn't require symbol)
    try:
        pending_result = await fetch_pending_orders(
            market="all",
            min_amount=0,
            include_current_price=False,
            side=None,
        )
        symbols_by_market: dict[str, set[str]] = {}
        for order in pending_result.get("orders", []):
            market = order.get("market", "")
            raw_symbol = order.get("raw_symbol", "")
            if market and raw_symbol:
                symbols_by_market.setdefault(market, set()).add(raw_symbol)
    except Exception as exc:
        logger.warning("Failed to collect symbols for fills: %s", exc)
        symbols_by_market = {}

    # Also get symbols from portfolio
    try:
        portfolio = await _get_portfolio_overview(markets)
        for pos in portfolio.get("positions", []):
            market_type = str(pos.get("market_type", "")).upper()
            symbol = pos.get("symbol", "")
            market_map = {"KR": "kr", "US": "us", "CRYPTO": "crypto"}
            market = market_map.get(market_type, "")
            if market and symbol:
                symbols_by_market.setdefault(market, set()).add(symbol)
    except Exception as exc:
        logger.warning("Failed to collect portfolio symbols for fills: %s", exc)

    # Query filled orders per-symbol
    semaphore = asyncio.Semaphore(5)

    async def _query_fills(symbol: str, market: str) -> list[dict[str, Any]]:
        async with semaphore:
            try:
                result = await get_order_history_impl(
                    symbol=symbol,
                    status="filled",
                    market=market,
                    days=1,
                    limit=20,
                )
                return [
                    {**order, "_market": market}
                    for order in result.get("orders", [])
                ]
            except Exception as exc:
                logger.debug("Failed to fetch fills for %s/%s: %s", market, symbol, exc)
                return []

    tasks = []
    for market in markets:
        for symbol in symbols_by_market.get(market, set()):
            tasks.append(_query_fills(symbol, market))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, list):
                fills.extend(result)
            elif isinstance(result, Exception):
                errors.append({"source": "fills", "error": str(result)})

    # Normalize fills into brief format
    normalized_fills: list[dict[str, str]] = []
    for fill in fills:
        market = fill.get("_market", "")
        currency = fill.get("currency", "KRW")
        symbol = fill.get("symbol", "")
        # Strip crypto prefix for display
        if market == "crypto":
            for prefix in ("KRW-", "USDT-"):
                if symbol.upper().startswith(prefix):
                    symbol = symbol[len(prefix):]
                    break

        price = fill.get("filled_avg_price") or fill.get("ordered_price") or 0
        qty = fill.get("filled_qty") or fill.get("ordered_qty") or 0
        amount = float(price) * float(qty)

        filled_at = fill.get("filled_at", "")
        time_str = ""
        if filled_at:
            try:
                dt = datetime.fromisoformat(str(filled_at).replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                time_str = ""

        normalized_fills.append({
            "symbol": symbol,
            "market": market,
            "side": fill.get("side", ""),
            "price_fmt": fmt_price(float(price), currency),
            "amount_fmt": fmt_amount(amount if currency == "KRW" else None),
            "time": time_str,
        })

    return {
        "total": len(normalized_fills),
        "fills": normalized_fills,
    }


def _group_pending_by_market(
    pending_result: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Group pending orders by market and build per-market summaries."""
    orders = pending_result.get("orders", [])
    by_market: dict[str, list[dict[str, Any]]] = {}

    for order in orders:
        market = order.get("market", "unknown")
        by_market.setdefault(market, []).append(order)

    result: dict[str, dict[str, Any]] = {}
    for market, market_orders in by_market.items():
        buy_orders = [o for o in market_orders if o.get("side") == "buy"]
        sell_orders = [o for o in market_orders if o.get("side") == "sell"]
        result[market] = {
            "total": len(market_orders),
            "buy_count": len(buy_orders),
            "sell_count": len(sell_orders),
            "total_buy_fmt": fmt_amount(
                sum(float(o.get("amount_krw") or 0) for o in buy_orders)
            ),
            "total_sell_fmt": fmt_amount(
                sum(float(o.get("amount_krw") or 0) for o in sell_orders)
            ),
            "near_fill_count": sum(
                1 for o in market_orders if o.get("fill_proximity") == "near"
            ),
            "needs_attention_count": sum(
                1 for o in market_orders if o.get("needs_attention")
            ),
            "orders": market_orders,
        }

    return result


def _build_portfolio_summary(
    overview: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build per-market portfolio summary from PortfolioOverviewService output."""
    positions = overview.get("positions", [])
    by_market: dict[str, list[dict[str, Any]]] = {}

    for pos in positions:
        market_type = str(pos.get("market_type", "")).upper()
        market_map = {"KR": "kr", "US": "us", "CRYPTO": "crypto"}
        market = market_map.get(market_type, "")
        if market:
            by_market.setdefault(market, []).append(pos)

    result: dict[str, dict[str, Any]] = {}
    for market, market_positions in by_market.items():
        total_eval = sum(float(p.get("evaluation") or 0) for p in market_positions)
        total_cost = sum(
            float(p.get("avg_price") or 0) * float(p.get("quantity") or 0)
            for p in market_positions
        )
        pnl_pct = ((total_eval - total_cost) / total_cost * 100) if total_cost > 0 else None

        # Top gainers/losers by profit_rate
        sorted_positions = sorted(
            [p for p in market_positions if p.get("profit_rate") is not None],
            key=lambda p: float(p.get("profit_rate") or 0),
            reverse=True,
        )
        top_gainers = [
            {
                "symbol": p["symbol"],
                "change_pct": fmt_pnl(float(p["profit_rate"]) * 100),
            }
            for p in sorted_positions[:3]
            if float(p.get("profit_rate") or 0) > 0
        ]
        top_losers = [
            {
                "symbol": p["symbol"],
                "change_pct": fmt_pnl(float(p["profit_rate"]) * 100),
            }
            for p in reversed(sorted_positions[-3:])
            if float(p.get("profit_rate") or 0) < 0
        ]

        currency = "USD" if market == "us" else "KRW"
        summary: dict[str, Any] = {
            "total_value_fmt": fmt_value(total_eval, currency),
            "pnl_pct": round(pnl_pct, 1) if pnl_pct is not None else None,
            "pnl_fmt": fmt_pnl(round(pnl_pct, 1) if pnl_pct is not None else None),
            "position_count": len(market_positions),
            "top_gainers": top_gainers,
            "top_losers": top_losers,
        }

        if market == "us":
            summary["total_value_usd"] = total_eval
            summary["total_value_krw"] = None
        else:
            summary["total_value_krw"] = total_eval
            summary["total_value_usd"] = None

        result[market] = summary

    return result


def _build_brief_text(
    *,
    date_fmt: str,
    market_overview: N8nMarketOverview | dict[str, Any],
    pending_by_market: dict[str, dict[str, Any]],
    portfolio_by_market: dict[str, dict[str, Any]],
    yesterday_fills: dict[str, Any],
) -> str:
    """Build the full brief text for Discord delivery."""
    lines: list[str] = []

    # Header
    lines.append(f"📋 Daily Trading Brief — {date_fmt}")
    lines.append("")

    # Market overview
    lines.append("🌍 시장 현황")
    if isinstance(market_overview, dict):
        fg = market_overview.get("fear_greed")
        btc_dom = market_overview.get("btc_dominance")
        mc_change = market_overview.get("total_market_cap_change_24h")
        econ_events = market_overview.get("economic_events_today", [])
    else:
        fg = market_overview.fear_greed
        btc_dom = market_overview.btc_dominance
        mc_change = market_overview.total_market_cap_change_24h
        econ_events = market_overview.economic_events_today or []

    if fg:
        fg_value = fg.value if hasattr(fg, "value") else fg.get("value")
        fg_label = fg.label if hasattr(fg, "label") else fg.get("label")
        fg_trend = fg.trend if hasattr(fg, "trend") else fg.get("trend")
        trend_kr = {"improving": "개선 중", "stable": "유지", "deteriorating": "악화 중"}.get(
            str(fg_trend or ""), str(fg_trend or "")
        )
        lines.append(f"Fear & Greed: {fg_value} ({fg_label}, {trend_kr})")
    if btc_dom is not None:
        lines.append(f"BTC 도미넌스: {btc_dom}%")
    if mc_change is not None:
        sign = "+" if mc_change > 0 else ""
        lines.append(f"전체 시총 24h: {sign}{mc_change}%")
    lines.append("")

    # Economic events
    if econ_events:
        lines.append("📅 오늘 경제 이벤트")
        for event in econ_events:
            time_str = event.time if hasattr(event, "time") else event.get("time", "")
            event_name = event.event if hasattr(event, "event") else event.get("event", "")
            importance = event.importance if hasattr(event, "importance") else event.get("importance", "")
            lines.append(f"• {time_str} {event_name} ({importance})")
        lines.append("")

    # Pending orders
    lines.append("💼 미체결 주문")
    market_labels = {"crypto": "크립토", "kr": "한국", "us": "미국"}
    for market_key in ("crypto", "kr", "us"):
        market_data = pending_by_market.get(market_key)
        if market_data and market_data["total"] > 0:
            label = market_labels[market_key]
            total = market_data["total"]
            buy = market_data["buy_count"]
            sell = market_data["sell_count"]
            line = f"[{label}] {total}건"
            if buy or sell:
                line += f" (매수 {buy} / 매도 {sell})"
            near = market_data.get("near_fill_count", 0)
            if near > 0:
                line += f" — 체결 임박 {near}건 ⚡"
            lines.append(line)
        else:
            lines.append(f"[{market_labels[market_key]}] 없음")
    lines.append("")

    # Portfolio
    lines.append("📊 포트폴리오")
    for market_key in ("crypto", "kr", "us"):
        market_data = portfolio_by_market.get(market_key)
        if market_data:
            label = market_labels[market_key]
            value_fmt = market_data.get("total_value_fmt", "-")
            pnl_fmt = market_data.get("pnl_fmt", "")
            line = f"[{label}] {value_fmt}"
            if pnl_fmt and pnl_fmt != "-":
                line += f" ({pnl_fmt})"
            lines.append(line)
    lines.append("")

    # Yesterday fills
    fills_data = yesterday_fills or {}
    total_fills = fills_data.get("total", 0)
    if total_fills > 0:
        lines.append("✅ 전일 체결")
        for fill in fills_data.get("fills", [])[:10]:  # Limit to 10
            symbol = fill.get("symbol", "")
            side = fill.get("side", "")
            price = fill.get("price_fmt", "")
            amount = fill.get("amount_fmt", "")
            time_str = fill.get("time", "")
            parts = [f"{symbol} {side}"]
            if price:
                parts.append(f"@{price}")
            if amount:
                parts.append(f"({amount})")
            if time_str:
                parts.append(time_str)
            lines.append(" ".join(parts))
        lines.append("")

    # Footer
    lines.append("각 시장별 미체결 상세는 스레드에서 확인 후 리뷰해줘.")

    return "\n".join(lines)


async def fetch_daily_brief(
    *,
    markets: list[str] | None = None,
    min_amount: float = 50_000,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Fetch the unified daily trading brief.

    Orchestrates parallel fetches of:
    - Pending orders (per-market)
    - Market context (fear/greed, BTC dominance, economic calendar)
    - Portfolio overview
    - Yesterday's fills

    Returns dict matching N8nDailyBriefResponse schema.
    """
    effective_markets = list(markets or _DEFAULT_MARKETS)
    effective_as_of = as_of or now_kst().replace(microsecond=0)
    errors: list[dict[str, object]] = []

    date_fmt = fmt_date_with_weekday(effective_as_of)

    # Parallel fetch all data sources
    pending_task = fetch_pending_orders(
        market="all",
        min_amount=min_amount,
        include_current_price=True,
        side=None,
        as_of=effective_as_of,
    )
    context_task = fetch_market_context(
        market="crypto",
        symbols=None,
        include_fear_greed=True,
        include_economic_calendar=True,
        as_of=effective_as_of,
    )
    portfolio_task = _get_portfolio_overview(effective_markets)
    fills_task = _fetch_yesterday_fills(effective_markets)

    results = await asyncio.gather(
        pending_task,
        context_task,
        portfolio_task,
        fills_task,
        return_exceptions=True,
    )

    # Unpack results with fallbacks
    pending_result: dict[str, Any] = {}
    if isinstance(results[0], dict):
        pending_result = results[0]
    elif isinstance(results[0], Exception):
        errors.append({"source": "pending_orders", "error": str(results[0])})

    context_result: dict[str, Any] = {}
    if isinstance(results[1], dict):
        context_result = results[1]
    elif isinstance(results[1], Exception):
        errors.append({"source": "market_context", "error": str(results[1])})

    portfolio_result: dict[str, Any] = {}
    if isinstance(results[2], dict):
        portfolio_result = results[2]
    elif isinstance(results[2], Exception):
        errors.append({"source": "portfolio", "error": str(results[2])})

    fills_result: dict[str, Any] = {"total": 0, "fills": []}
    if isinstance(results[3], dict):
        fills_result = results[3]
    elif isinstance(results[3], Exception):
        errors.append({"source": "fills", "error": str(results[3])})

    # Build per-market breakdowns
    pending_by_market = _group_pending_by_market(pending_result)
    portfolio_by_market = _build_portfolio_summary(portfolio_result)

    # Market overview with fallback
    from app.schemas.n8n import N8nMarketOverview

    market_overview = context_result.get(
        "market_overview",
        N8nMarketOverview(
            fear_greed=None,
            btc_dominance=None,
            total_market_cap_change_24h=None,
            economic_events_today=[],
        ),
    )

    # Generate brief text
    brief_text = _build_brief_text(
        date_fmt=date_fmt,
        market_overview=market_overview,
        pending_by_market=pending_by_market,
        portfolio_by_market=portfolio_by_market,
        yesterday_fills=fills_result,
    )

    # Collect sub-errors
    errors.extend(pending_result.get("errors", []))
    errors.extend(context_result.get("errors", []))
    errors.extend(portfolio_result.get("warnings", []))

    return {
        "success": True,
        "as_of": effective_as_of.isoformat(),
        "date_fmt": date_fmt,
        "market_overview": market_overview,
        "pending_orders": {
            market: pending_by_market.get(market)
            for market in effective_markets
        },
        "portfolio_summary": {
            market: portfolio_by_market.get(market)
            for market in effective_markets
        },
        "yesterday_fills": fills_result,
        "brief_text": brief_text,
        "errors": errors,
    }


__all__ = ["fetch_daily_brief"]
```

### Step 4: Run tests to verify they pass

Run: `uv run pytest tests/test_n8n_daily_brief_service.py -v`
Expected: PASS

### Step 5: Commit

```bash
git add app/services/n8n_daily_brief_service.py tests/test_n8n_daily_brief_service.py
git commit -m "feat(n8n): add daily brief service with parallel data fetching"
```

---

## Task 4: Add Router Endpoint

**Files:**
- Modify: `app/routers/n8n.py`
- Create: `tests/test_n8n_daily_brief_api.py`

### Step 1: Write the failing API test

```python
# tests/test_n8n_daily_brief_api.py
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.timezone import KST


def _make_brief_result() -> dict:
    from app.schemas.n8n import N8nMarketOverview

    return {
        "success": True,
        "as_of": "2026-03-17T08:30:00+09:00",
        "date_fmt": "03/17 (화)",
        "market_overview": N8nMarketOverview(
            fear_greed=None,
            btc_dominance=56.64,
            total_market_cap_change_24h=3.86,
            economic_events_today=[],
        ),
        "pending_orders": {"crypto": None, "kr": None, "us": None},
        "portfolio_summary": {"crypto": None, "kr": None, "us": None},
        "yesterday_fills": {"total": 0, "fills": []},
        "brief_text": "📋 Daily Trading Brief — 03/17 (화)\n...",
        "errors": [],
    }


@pytest.mark.integration
class TestDailyBriefEndpoint:
    def _get_client(self) -> TestClient:
        app = FastAPI()
        from app.routers.n8n import router
        app.include_router(router)
        return TestClient(app)

    def test_daily_brief_default_params(self):
        client = self._get_client()
        with patch(
            "app.routers.n8n.fetch_daily_brief",
            new_callable=AsyncMock,
            return_value=_make_brief_result(),
        ):
            resp = client.get("/api/n8n/daily-brief")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "brief_text" in body
        assert "market_overview" in body

    def test_daily_brief_custom_markets(self):
        client = self._get_client()
        with patch(
            "app.routers.n8n.fetch_daily_brief",
            new_callable=AsyncMock,
            return_value=_make_brief_result(),
        ) as mock_fetch:
            resp = client.get("/api/n8n/daily-brief?markets=crypto,kr")
        assert resp.status_code == 200
        call_kwargs = mock_fetch.call_args.kwargs
        assert call_kwargs["markets"] == ["crypto", "kr"]

    def test_daily_brief_error_returns_500(self):
        client = self._get_client()
        with patch(
            "app.routers.n8n.fetch_daily_brief",
            new_callable=AsyncMock,
            side_effect=Exception("total failure"),
        ):
            resp = client.get("/api/n8n/daily-brief")
        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
```

### Step 2: Run test to verify it fails

Run: `uv run pytest tests/test_n8n_daily_brief_api.py -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_daily_brief'`

### Step 3: Add the endpoint to `app/routers/n8n.py`

Add the import at the top of `app/routers/n8n.py`:

```python
from app.schemas.n8n import (
    ...,  # existing imports
    N8nDailyBriefResponse,
)
from app.services.n8n_daily_brief_service import fetch_daily_brief
```

Add the endpoint (before or after existing endpoints):

```python
@router.get("/daily-brief", response_model=N8nDailyBriefResponse)
async def get_daily_brief(
    markets: str = Query(
        "crypto,kr,us",
        description="Comma-separated market list: crypto,kr,us",
    ),
    min_amount: float = Query(
        50_000, ge=0, description="Minimum order amount filter in KRW"
    ),
) -> N8nDailyBriefResponse | JSONResponse:
    """
    Get unified daily trading brief.

    Combines pending orders, market context, portfolio summary, and yesterday's fills
    into a single response with pre-formatted brief text for Discord delivery.
    """
    as_of_dt = now_kst().replace(microsecond=0)

    market_list = [m.strip().lower() for m in markets.split(",") if m.strip()]
    valid_markets = [m for m in market_list if m in ("crypto", "kr", "us")]
    if not valid_markets:
        valid_markets = ["crypto", "kr", "us"]

    try:
        result = await fetch_daily_brief(
            markets=valid_markets,
            min_amount=min_amount,
            as_of=as_of_dt,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build daily brief")
        from app.schemas.n8n import (
            N8nDailyBriefPendingOrders,
            N8nDailyBriefPortfolio,
            N8nMarketOverview,
            N8nYesterdayFills,
        )

        payload = N8nDailyBriefResponse(
            success=False,
            as_of=as_of_dt.isoformat(),
            date_fmt=as_of_dt.strftime("%m/%d"),
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_orders=N8nDailyBriefPendingOrders(),
            portfolio_summary=N8nDailyBriefPortfolio(),
            yesterday_fills=N8nYesterdayFills(),
            brief_text="",
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nDailyBriefResponse(**result)
```

### Step 4: Run tests to verify they pass

Run: `uv run pytest tests/test_n8n_daily_brief_api.py -v`
Expected: PASS

### Step 5: Commit

```bash
git add app/routers/n8n.py tests/test_n8n_daily_brief_api.py
git commit -m "feat(n8n): add GET /api/n8n/daily-brief endpoint"
```

---

## Task 5: Add brief_text Unit Tests

**Files:**
- Modify: `tests/test_n8n_daily_brief_formatting.py`

### Step 1: Add brief_text generation tests

Append to `tests/test_n8n_daily_brief_formatting.py`:

```python
@pytest.mark.unit
class TestBuildBriefText:
    def test_contains_header(self):
        from app.schemas.n8n import N8nMarketOverview
        from app.services.n8n_daily_brief_service import _build_brief_text

        text = _build_brief_text(
            date_fmt="03/17 (화)",
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_by_market={},
            portfolio_by_market={},
            yesterday_fills={"total": 0, "fills": []},
        )

        assert "📋 Daily Trading Brief — 03/17 (화)" in text
        assert "💼 미체결 주문" in text
        assert "📊 포트폴리오" in text

    def test_includes_pending_counts(self):
        from app.schemas.n8n import N8nMarketOverview
        from app.services.n8n_daily_brief_service import _build_brief_text

        text = _build_brief_text(
            date_fmt="03/17 (화)",
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_by_market={
                "crypto": {
                    "total": 11,
                    "buy_count": 4,
                    "sell_count": 7,
                    "near_fill_count": 2,
                    "needs_attention_count": 5,
                    "orders": [],
                },
            },
            portfolio_by_market={},
            yesterday_fills={"total": 0, "fills": []},
        )

        assert "[크립토] 11건 (매수 4 / 매도 7)" in text
        assert "체결 임박 2건 ⚡" in text

    def test_includes_fills(self):
        from app.schemas.n8n import N8nMarketOverview
        from app.services.n8n_daily_brief_service import _build_brief_text

        text = _build_brief_text(
            date_fmt="03/17 (화)",
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_by_market={},
            portfolio_by_market={},
            yesterday_fills={
                "total": 1,
                "fills": [
                    {
                        "symbol": "ETH",
                        "side": "sell",
                        "price_fmt": "3.35M",
                        "amount_fmt": "402만",
                        "time": "14:23",
                    }
                ],
            },
        )

        assert "✅ 전일 체결" in text
        assert "ETH sell" in text
```

### Step 2: Run tests

Run: `uv run pytest tests/test_n8n_daily_brief_formatting.py -v`
Expected: PASS

### Step 3: Commit

```bash
git add tests/test_n8n_daily_brief_formatting.py
git commit -m "test(n8n): add brief_text generation tests"
```

---

## Task 6: Lint, Type Check, and Full Test Gate

**Files:** None (validation only)

### Step 1: Run linter

Run: `make lint`
Expected: PASS — no errors

### Step 2: Run full test suite

Run: `make test`
Expected: PASS — all existing tests + new tests pass

### Step 3: Fix any issues found

Address any lint or test failures. Common expected issues:
- Import ordering
- Unused imports
- Type annotation refinements

### Step 4: Final commit

```bash
git add -A
git commit -m "chore(n8n): fix lint and type issues in daily brief"
```

---

## Task 7: Manual Verification

### Step 1: Start the dev server

Run: `make dev`

### Step 2: Test the endpoint

```bash
# Default — all markets
curl -s "http://localhost:8000/api/n8n/daily-brief" | python -m json.tool

# Crypto only
curl -s "http://localhost:8000/api/n8n/daily-brief?markets=crypto" | python -m json.tool

# Custom min_amount
curl -s "http://localhost:8000/api/n8n/daily-brief?markets=crypto,kr&min_amount=100000" | python -m json.tool
```

### Step 3: Verify response structure

Check:
- [ ] `success: true`
- [ ] `date_fmt` has Korean weekday
- [ ] `market_overview` has fear_greed, btc_dominance, economic_events_today
- [ ] `pending_orders` has per-market breakdowns
- [ ] `portfolio_summary` has per-market values
- [ ] `yesterday_fills` has recent fills (if any)
- [ ] `brief_text` is complete and well-formatted
- [ ] No unhandled errors in server logs

---

## Summary: Files Changed

| Action | File |
|--------|------|
| Modify | `app/schemas/n8n.py` — Add daily brief schema models |
| Modify | `app/services/n8n_formatting.py` — Add `fmt_date_with_weekday`, `fmt_value`, `fmt_pnl` |
| Create | `app/services/n8n_daily_brief_service.py` — Main orchestrator |
| Modify | `app/routers/n8n.py` — Add `GET /daily-brief` endpoint |
| Create | `tests/test_n8n_daily_brief_formatting.py` — Formatting unit tests |
| Create | `tests/test_n8n_daily_brief_service.py` — Service unit tests |
| Create | `tests/test_n8n_daily_brief_api.py` — API integration tests |

## Future Considerations

- **Weekly report**: Extend service with date-range aggregation
- **AI brief analysis**: Replace static text with GLM-powered summary
- **09:00 crypto review**: After this ships, disable the separate 09:00 n8n workflow
- **Performance**: If fill queries become slow, consider a dedicated "recent fills" cache or DB-backed fill log
