"""Daily trading brief service for n8n integration.

Aggregates pending orders, market context, portfolio summary, and yesterday's fills
into a single unified brief with pre-formatted text for Discord delivery.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

try:
    from app.mcp_server.tooling.trade_journal_tools import compute_active_dca_daily_burn
except ImportError:

    async def compute_active_dca_daily_burn() -> dict[str, Any]:
        return {
            "daily_burn_krw": 0.0,
            "active_count": 0,
            "per_record": [],
            "days_to_next_obligation": None,
            "cash_needed_until_obligation": 0.0,
        }


try:
    from app.schemas.n8n.board_brief import (
        BoardBriefContext,
        BoardBriefRender,
        GateResult,
        N8nG2GatePayload,
    )
except ModuleNotFoundError:

    @dataclass
    class GateResult:
        id: str
        label: str
        status: str
        detail: str

    @dataclass
    class N8nG2GatePayload:
        pass_: bool = True

    @dataclass
    class BoardBriefContext:
        date_fmt: str = ""
        market_overview: Any = None
        pending_by_market: dict[str, Any] = field(default_factory=dict)
        portfolio_by_market: dict[str, Any] = field(default_factory=dict)
        yesterday_fills: dict[str, Any] = field(default_factory=dict)
        daily_burn: dict[str, Any] = field(default_factory=dict)
        gate_results: dict[str, Any] = field(default_factory=dict)
        generated_at: datetime = field(default_factory=now_kst)
        cio_recommendation: str | None = None

        def model_copy(self, update: dict[str, Any]) -> BoardBriefContext:
            return replace(self, **update)

    @dataclass
    class BoardBriefRender:
        phase: str
        brief_text: str
        generated_at: datetime
        gate_results: dict[str, Any] | None = None


from app.schemas.n8n.common import N8nMarketOverview
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


def _collect_symbols_by_market(
    pending_result: dict[str, Any],
    portfolio_result: dict[str, Any],
) -> dict[str, set[str]]:
    """Derive a shared symbol map from pending orders and portfolio positions."""
    symbols_by_market: dict[str, set[str]] = {}

    # Collect from pending orders
    for order in pending_result.get("orders", []):
        market = str(order.get("market") or "").strip()
        raw_symbol = str(order.get("raw_symbol") or order.get("symbol") or "").strip()
        if market and raw_symbol:
            symbols_by_market.setdefault(market, set()).add(raw_symbol)

    # Collect from portfolio positions
    market_map = {"CRYPTO": "crypto", "KR": "kr", "US": "us"}
    for position in portfolio_result.get("positions", []):
        market = market_map.get(str(position.get("market_type") or "").upper())
        symbol = str(position.get("symbol") or "").strip()
        if not market or not symbol:
            continue

        # Normalize crypto symbols for history queries if needed
        if market == "crypto" and "-" not in symbol:
            symbol = f"KRW-{symbol.upper()}"

        symbols_by_market.setdefault(market, set()).add(symbol)

    return symbols_by_market


async def _fetch_yesterday_fills(
    *,
    markets: list[str],
    symbols_by_market: dict[str, set[str]],
) -> dict[str, Any]:
    """Fetch yesterday's filled orders across requested markets.

    Uses provided symbols_by_market to avoid redundant fetches of
    pending orders or portfolio data.
    """
    from app.mcp_server.tooling.orders_history import get_order_history_impl

    fills: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

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
            [
                p
                for p in market_positions
                if p.get("profit_rate") is not None and not p.get("dust")
            ],
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
            "dust_positions": [
                {
                    "symbol": p.get("symbol"),
                    "quantity": p.get("quantity"),
                    "current_krw_value": float(p.get("evaluation") or 0),
                }
                for p in market_positions
                if p.get("dust")
            ],
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
    daily_burn: dict[str, Any] | None = None,
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

    dust_lines: list[str] = []
    for market_key in ("crypto", "kr", "us"):
        market_data = portfolio_by_market.get(market_key) or {}
        for dust_pos in market_data.get("dust_positions") or []:
            symbol = str(dust_pos.get("symbol") or "")
            if symbol.startswith("KRW-"):
                symbol = symbol[4:]
            quantity_raw = dust_pos.get("quantity")
            try:
                quantity_fmt = f"{float(quantity_raw):g}"
            except (TypeError, ValueError):
                quantity_fmt = "-"
            krw_value = float(dust_pos.get("current_krw_value") or 0)
            dust_lines.append(f"{symbol} {quantity_fmt} (~{krw_value:.0f} KRW)")
    if dust_lines:
        lines.append("🧹 Dust")
        lines.append(
            f"{', '.join(dust_lines)} — Upbit 최소 주문 금액 미만, execution-actionable 제외, journal 유지. cleanup backlog."
        )
        lines.append("")

    # Funding
    burn_data = daily_burn or {}
    burn_krw = float(burn_data.get("daily_burn_krw") or 0)
    active_dca_count = int(burn_data.get("active_count") or 0)
    lines.append("💰 자금 현황")
    lines.append(
        f"daily_burn: {burn_krw:,.0f} KRW (active DCA {active_dca_count}종 · 재산출)"
    )
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


def _build_tc_preliminary_text(ctx: BoardBriefContext) -> str:
    lines: list[str] = []
    lines.append("📊 TC Preliminary — 자금 현황 재계산")
    lines.append("")
    lines.append("🧭 Framing")
    lines.append("경로 A·B 병행 가능. 경로 A/B는 상호배타가 아니다.")
    lines.append("")
    lines.append("💰 자금 현황")
    lines.append(f"- manual_cash: {fmt_amount(float(ctx.manual_cash))}")
    lines.append(
        f"- daily_burn: {fmt_amount(float(ctx.daily_burn)) if ctx.daily_burn is not None else '-'}"
    )
    lines.append(
        f"- days_to_next_obligation: {ctx.days_to_next_obligation}일"
        if ctx.days_to_next_obligation is not None
        else "- days_to_next_obligation: -"
    )
    lines.append("")
    lines.append("📈 쏠림/편중")
    if ctx.weights_top_n:
        for item in ctx.weights_top_n:
            lines.append(f"- {item.symbol} {item.weight_pct:.1f}% ({item.market})")
    else:
        lines.append("- 상위 비중 데이터 없음")
    lines.append("")
    lines.append("📉 매도/축소 후보")
    if ctx.holdings:
        for item in ctx.holdings[:5]:
            lines.append(f"- {item.symbol} ({item.market})")
    else:
        lines.append("- 후보 없음")
    if ctx.dust_items:
        dust_line = ", ".join(
            f"{item.symbol} (~{int(item.value_krw):,} KRW)" for item in ctx.dust_items
        )
        lines.append(f"- Dust footnote: {dust_line}")
    lines.append("")
    lines.append("🛣️ 경로 A / 경로 B")
    lines.append("- A: 즉시 재배치")
    lines.append("- B: 분할 대응")
    lines.append("- 경로 A·B 병행 가능")
    return "\n".join(lines)


def _normalize_gate_results(
    gate_results: dict[str, GateResult | N8nG2GatePayload],
) -> dict[str, GateResult]:
    defaults = {
        "G1": ("G1 Runway", "tbd", "TBD (S7)"),
        "G2": ("G2 Diversification", "pending", "S5 engineering 중"),
        "G3": ("G3", "tbd", "TBD (Sx)"),
        "G4": ("G4", "tbd", "TBD (Sx)"),
        "G5": ("G5", "tbd", "TBD (Sx)"),
        "G6": ("G6", "tbd", "TBD (Sx)"),
    }
    normalized: dict[str, GateResult] = {}
    for gate_id, (label, status, detail) in defaults.items():
        item = gate_results.get(gate_id)
        if item is None or not isinstance(item, GateResult):
            normalized[gate_id] = GateResult(
                id=gate_id, label=label, status=status, detail=detail
            )
        else:
            normalized[gate_id] = item
    return normalized


def _build_cio_pending_text(ctx: BoardBriefContext) -> str:
    lines: list[str] = [_build_tc_preliminary_text(ctx), ""]
    lines.append("🎯 권고")
    lines.append(ctx.cio_recommendation or "(CIO 의견 대기 중)")
    lines.append("")
    lines.append("📊 Gate 판정 결과")
    gate_status_results = _normalize_gate_results(ctx.gate_results)
    for gate_id in ("G1", "G2", "G3", "G4", "G5", "G6"):
        gate = gate_status_results[gate_id]
        lines.append(f"- {gate.id} {gate.label}: {gate.status} ({gate.detail})")
    g2_payload = ctx.gate_results.get("G2")
    if isinstance(g2_payload, N8nG2GatePayload) and not g2_payload.pass_:
        lines.append("🚫 신규 매수 차단 — G2 fail")
    lines.append("")
    lines.append("[funding] 자금 의도/제약 확인. (경로 A·B 병행 가능)")
    lines.append("[action] 실행 우선순위 확인. (경로 A·B 병행 가능)")
    return "\n".join(lines)


def build_tc_preliminary(ctx: BoardBriefContext) -> BoardBriefRender:
    brief_text = _build_tc_preliminary_text(ctx)
    return BoardBriefRender(
        phase="tc_preliminary",
        brief_text=brief_text,
        generated_at=ctx.generated_at,
    )


def build_cio_pending_decision(ctx: BoardBriefContext) -> BoardBriefRender:
    gate_status_results = _normalize_gate_results(ctx.gate_results)
    merged_gate_results: dict[str, GateResult | N8nG2GatePayload] = {
        **gate_status_results
    }
    if isinstance(ctx.gate_results.get("G2"), N8nG2GatePayload):
        merged_gate_results["G2"] = ctx.gate_results["G2"]

    pending_ctx = ctx.model_copy(update={"gate_results": merged_gate_results})
    brief_text = _build_cio_pending_text(pending_ctx)
    return BoardBriefRender(
        phase="cio_pending",
        brief_text=brief_text,
        gate_results=merged_gate_results,
        generated_at=ctx.generated_at,
    )


async def fetch_daily_brief(
    *,
    markets: list[str] | None = None,
    min_amount: float = 50_000,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Fetch the unified daily trading brief.

    Orchestrates parallel fetches in two stages:
    Stage 1: Gather pending orders and portfolio summary.
    Stage 2: Use derived symbol context to fetch market indicators and filled orders.

    Returns dict matching N8nDailyBriefResponse schema.
    """
    effective_markets = list(markets or _DEFAULT_MARKETS)
    effective_as_of = as_of or now_kst().replace(microsecond=0)
    errors: list[dict[str, object]] = []

    date_fmt = fmt_date_with_weekday(effective_as_of)

    # Stage 1: Gather shared inputs once
    pending_task = fetch_pending_orders(
        market="all",
        min_amount=min_amount,
        include_current_price=True,
        include_indicators=True,
        side=None,
        as_of=effective_as_of,
    )
    portfolio_task = _get_portfolio_overview(effective_markets)
    daily_burn_task = compute_active_dca_daily_burn()

    results_s1 = await asyncio.gather(
        pending_task,
        portfolio_task,
        daily_burn_task,
        return_exceptions=True,
    )

    # Unpack Stage 1 results with fallbacks
    pending_result: dict[str, Any] = {}
    if isinstance(results_s1[0], dict):
        pending_result = results_s1[0]
    elif isinstance(results_s1[0], Exception):
        errors.append({"source": "pending_orders", "error": str(results_s1[0])})

    portfolio_result: dict[str, Any] = {}
    if isinstance(results_s1[1], dict):
        portfolio_result = results_s1[1]
    elif isinstance(results_s1[1], Exception):
        errors.append({"source": "portfolio", "error": str(results_s1[1])})

    daily_burn_result: dict[str, Any] = {
        "daily_burn_krw": 0.0,
        "active_count": 0,
        "per_record": [],
        "days_to_next_obligation": None,
        "cash_needed_until_obligation": 0.0,
    }
    if isinstance(results_s1[2], dict):
        daily_burn_result = results_s1[2]
    elif isinstance(results_s1[2], Exception):
        errors.append({"source": "daily_burn", "error": str(results_s1[2])})

    # Derive shared symbol context for Stage 2
    symbols_by_market = _collect_symbols_by_market(pending_result, portfolio_result)

    # Preserve exact market prefixes for downstream context fetches.
    crypto_symbols = sorted(symbols_by_market.get("crypto", set()))

    # Stage 2: Parallel fetch remaining data using shared symbols
    context_task = fetch_market_context(
        market="crypto",
        symbols=crypto_symbols
        or ["BTC"],  # Default to BTC for general context if empty
        include_fear_greed=True,
        include_economic_calendar=True,
        as_of=effective_as_of,
    )
    fills_task = _fetch_yesterday_fills(
        markets=effective_markets,
        symbols_by_market=symbols_by_market,
    )

    results_s2 = await asyncio.gather(
        context_task,
        fills_task,
        return_exceptions=True,
    )

    # Unpack Stage 2 results with fallbacks
    context_result: dict[str, Any] = {}
    if isinstance(results_s2[0], dict):
        context_result = results_s2[0]
    elif isinstance(results_s2[0], Exception):
        errors.append({"source": "market_context", "error": str(results_s2[0])})

    fills_result: dict[str, Any] = {"total": 0, "fills": []}
    if isinstance(results_s2[1], dict):
        fills_result = results_s2[1]
    elif isinstance(results_s2[1], Exception):
        errors.append({"source": "fills", "error": str(results_s2[1])})

    # Build per-market breakdowns
    pending_by_market = _group_pending_by_market(pending_result)
    portfolio_by_market = _build_portfolio_summary(portfolio_result)

    # Market overview with fallback
    from app.schemas.n8n.common import N8nMarketOverview

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
        daily_burn=daily_burn_result,
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
        "daily_burn": daily_burn_result,
        "brief_text": brief_text,
        "errors": errors,
    }


__all__ = ["build_cio_pending_decision", "build_tc_preliminary", "fetch_daily_brief"]
