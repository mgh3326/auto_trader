"""Daily trading brief service for n8n integration.

Aggregates pending orders, market context, portfolio summary, and yesterday's fills
into a single unified brief with pre-formatted text for Discord delivery.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.schemas.n8n import N8nMarketOverview
from app.services.n8n_formatting import (
    fmt_amount,
    fmt_date_with_weekday,
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
                    {**order, "_market": market} for order in result.get("orders", [])
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
                    symbol = symbol[len(prefix) :]
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

        normalized_fills.append(
            {
                "symbol": symbol,
                "market": market,
                "side": fill.get("side", ""),
                "price_fmt": fmt_price(float(price), currency),
                "amount_fmt": fmt_amount(amount if currency == "KRW" else None),
                "time": time_str,
            }
        )

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

        # Derive cost from profit_rate and evaluation to avoid currency mismatch.
        # For US stocks, avg_price may be in KRW (manual holdings) or USD (KIS),
        # but profit_rate and evaluation are always in the same currency context.
        total_cost = 0.0
        for p in market_positions:
            eval_amt = float(p.get("evaluation") or 0)
            rate = p.get("profit_rate")
            if eval_amt > 0 and rate is not None:
                denominator = 1.0 + float(rate)
                if denominator > 0:
                    total_cost += eval_amt / denominator
                else:
                    # profit_rate == -1.0 means total loss; cost = eval - profit_loss
                    profit_loss = float(p.get("profit_loss") or 0)
                    total_cost += eval_amt - profit_loss
            elif eval_amt <= 0 and rate is not None and rate <= -1.0:
                # Zero evaluation, total loss — derive cost from profit_loss
                profit_loss = float(p.get("profit_loss") or 0)
                total_cost += -profit_loss if profit_loss < 0 else 0
            else:
                # Fallback: use avg_price * quantity (safe for same-currency markets)
                avg = float(p.get("avg_price") or 0)
                qty = float(p.get("quantity") or 0)
                total_cost += avg * qty

        pnl_pct = (
            ((total_eval - total_cost) / total_cost * 100) if total_cost > 0 else None
        )

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
        trend_kr = {
            "improving": "개선 중",
            "stable": "유지",
            "deteriorating": "악화 중",
        }.get(str(fg_trend or ""), str(fg_trend or ""))
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
            event_name = (
                event.event if hasattr(event, "event") else event.get("event", "")
            )
            importance = (
                event.importance
                if hasattr(event, "importance")
                else event.get("importance", "")
            )
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
            market: pending_by_market.get(market) for market in effective_markets
        },
        "portfolio_summary": {
            market: portfolio_by_market.get(market) for market in effective_markets
        },
        "yesterday_fills": fills_result,
        "brief_text": brief_text,
        "errors": errors,
    }


__all__ = ["fetch_daily_brief"]
