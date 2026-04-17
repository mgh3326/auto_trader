"""Daily trading brief service for n8n integration.

Aggregates pending orders, market context, portfolio summary, and yesterday's fills
into a single unified brief with pre-formatted text for Discord delivery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.schemas.n8n.board_brief import (
    BoardBriefContext,
    BoardBriefPhase,
    BoardBriefRender,
    BoardFundingResponse,
    FundingIntent,
    GateResult,
    N8nG2GatePayload,
)
from app.schemas.n8n.common import N8nMarketOverview
from app.services.cio_coin_briefing.prompts.gate_phrases import (
    BOARD_QUESTIONS_TEMPLATE,
    FORBIDDEN_PATTERNS,
    FRAMING_AB_PATH_NON_EXCLUSIVE,
    G2_NEW_BUDGET_LINES,
    G2_RUNWAY_FUEL_LINES,
    PATH_SECTION_AB_REPEAT,
)
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
_FAIL_CLOSED_PREFIX = "⚠️ "
_FAIL_CLOSED_SEPARATOR = " 누락 — "
_DUST_AGGREGATE_RE = re.compile(
    r"^🧹 Dust \d+종목 · 합계 .+ · 포트폴리오 \d+(?:\.\d+)?%$"
)


@dataclass(frozen=True)
class InvariantViolation:
    """One render invariant violation."""

    code: str
    detail: str


class RenderInvariantError(RuntimeError):
    """Raised when final markdown violates render safety invariants."""

    def __init__(self, violations: list[InvariantViolation]) -> None:
        self.violations = violations
        details = ", ".join(f"{item.code}: {item.detail}" for item in violations)
        super().__init__(f"Board brief render invariant failed: {details}")


class RenderRouter:
    """Route render output to board or ops channels.

    The default implementation only performs ops escalation when
    N8N_OPS_ESCALATION_WEBHOOK is configured. Tests can inject a subclass to
    assert fail-closed routing without making network calls.
    """

    def route_ops_escalation(self, message: str) -> None:
        webhook_url = os.getenv("N8N_OPS_ESCALATION_WEBHOOK", "").strip()
        if not webhook_url:
            logger.warning("Ops escalation webhook not configured: %s", message)
            return
        try:
            with httpx.Client(timeout=5) as client:
                client.post(webhook_url, json={"content": message})
        except httpx.HTTPError:
            logger.exception("Failed to send ops escalation webhook")


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


def _fmt_krw(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.0f} KRW"


def _fmt_pct(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1f}%"


def _fmt_days(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}일"


def _extract_followup_context(
    payload: BoardBriefContext | dict[str, Any],
) -> BoardBriefContext:
    """Normalize router/test payloads into the board brief render context."""
    if isinstance(payload, BoardBriefContext):
        return payload
    return BoardBriefContext.model_validate(payload)


def _missing_required_context(ctx: BoardBriefContext) -> tuple[str, str] | None:
    required_checks: list[tuple[str, bool, str]] = [
        (
            "exchange_krw",
            ctx.exchange_krw > 0,
            "거래소 주문가능 KRW 미수신, 브리핑 생성 불가",
        ),
        (
            "unverified_cap",
            ctx.unverified_cap is not None,
            "manual_cash 관련 권고/문구 생성 금지",
        ),
        (
            "next_obligation",
            ctx.next_obligation is not None,
            "obligation 행과 경로 A 평가 보류",
        ),
        (
            "tier_scenarios",
            bool(ctx.tier_scenarios),
            "입금 시나리오 산출 보류",
        ),
        (
            "data_sufficient_by_symbol",
            bool(ctx.data_sufficient_by_symbol),
            "심볼별 G1 데이터 충분성 평가 불가",
        ),
        (
            "btc_regime",
            ctx.btc_regime is not None,
            "G4 regime 통과 판정 금지",
        ),
        (
            "holdings",
            bool(ctx.holdings),
            "포트폴리오 쏠림 및 dust 평가 불가",
        ),
    ]
    for field, present, reason in required_checks:
        if not present:
            return field, reason
    return None


def _route_fail_closed(
    *,
    ctx: BoardBriefContext,
    phase: BoardBriefPhase,
    field: str,
    reason: str,
    router: RenderRouter,
) -> BoardBriefRender:
    generated_at = ctx.generated_at or now_kst().replace(microsecond=0)
    text = f"⚠️ {field} 누락 — {reason}"
    router.route_ops_escalation(text)
    logger.error(
        "Board brief fail-closed render routed to ops",
        extra={"phase": phase, "missing_field": field},
    )
    return BoardBriefRender(
        phase=phase,
        embed={},
        text=text,
        gate_results=None,
        generated_at=generated_at,
    )


def _format_unverified_amounts(ctx: BoardBriefContext) -> set[str]:
    if not ctx.unverified_cap:
        return set()
    amount = ctx.unverified_cap.amount
    return {
        f"{amount:,.0f}",
        f"{amount:.0f}",
        _fmt_krw(amount),
    }


def _gate_passed(gate: GateResult | N8nG2GatePayload | None) -> bool:
    if isinstance(gate, N8nG2GatePayload):
        return gate.passed and gate.status == "pass"
    if isinstance(gate, GateResult):
        return gate.status == "pass"
    return False


def _line_count(text: str, needle: str) -> int:
    return sum(1 for line in text.splitlines() if needle in line)


def _contains_fail_closed_anchor(text: str) -> bool:
    return any(
        line.startswith(_FAIL_CLOSED_PREFIX) and _FAIL_CLOSED_SEPARATOR in line
        for line in text.splitlines()
    )


def _validate_funding_rows(text: str) -> list[InvariantViolation]:
    exchange_rows = _line_count(text, "거래소 KRW:")
    unverified_rows = _line_count(text, "미확인 cap")
    if exchange_rows == 1 and unverified_rows == 1:
        return []
    return [
        InvariantViolation(
            code="funding_rows",
            detail=(
                "expected 거래소 KRW and 미확인 cap rows exactly once "
                f"(got {exchange_rows}/{unverified_rows})"
            ),
        )
    ]


def _validate_runway_excludes_unverified_cap(
    text: str, ctx: BoardBriefContext
) -> list[InvariantViolation]:
    runway_lines = [line for line in text.splitlines() if "runway" in line.lower()]
    unverified_amounts = _format_unverified_amounts(ctx)
    if not any(
        amount and amount in line
        for line in runway_lines
        for amount in unverified_amounts
    ):
        return []
    return [
        InvariantViolation(
            code="runway_excludes_unverified_cap",
            detail="runway line includes unverified_cap amount",
        )
    ]


def _validate_ab_anchors(
    text: str, *, phase: BoardBriefPhase
) -> list[InvariantViolation]:
    framing_count = text.count(FRAMING_AB_PATH_NON_EXCLUSIVE)
    path_repeat_count = text.count(PATH_SECTION_AB_REPEAT)
    funding_question_count = text.count("[funding-confirmation]") + text.count(
        "[funding]"
    )
    action_question_count = text.count("[action]")
    if phase == "cio_pending":
        ab_ok = (
            framing_count == 1
            and path_repeat_count == 1
            and funding_question_count == 1
            and action_question_count == 1
        )
    else:
        ab_ok = framing_count == 1 and path_repeat_count == 1
    if ab_ok:
        return []
    return [
        InvariantViolation(
            code="ab_anchor_triple",
            detail=(
                "expected A/B framing, repeat, and CIO question anchors exactly once "
                f"(got {framing_count}/{path_repeat_count}/"
                f"{funding_question_count}/{action_question_count})"
            ),
        )
    ]


def _validate_g2_phrase(text: str) -> list[InvariantViolation]:
    runway_phrase_count = text.count("**운영 연료**")
    new_budget_phrase_count = text.count("신규 risk budget 후보")
    if runway_phrase_count + new_budget_phrase_count == 1:
        return []
    return [
        InvariantViolation(
            code="g2_phrase_exactly_one",
            detail=(
                "expected exactly one G2 phrase head "
                f"(got {runway_phrase_count}/{new_budget_phrase_count})"
            ),
        )
    ]


def _validate_immediate_buy_gates(
    text: str, ctx: BoardBriefContext
) -> list[InvariantViolation]:
    if "CIO 권고 (1) 즉시 매수" not in text:
        return []
    gates = _build_gate_results(ctx)
    missing = [
        name for name in ("G2", "G3", "G4", "G5") if not _gate_passed(gates.get(name))
    ]
    if not missing:
        return []
    return [
        InvariantViolation(
            code="immediate_buy_requires_g2_g5_pass",
            detail=f"immediate buy rendered while {', '.join(missing)} not pass",
        )
    ]


def _validate_cio_pending_invariants(
    text: str, ctx: BoardBriefContext
) -> list[InvariantViolation]:
    return [
        *_validate_g2_phrase(text),
        *_validate_immediate_buy_gates(text, ctx),
    ]


def _validate_dust_aggregate(text: str) -> list[InvariantViolation]:
    dust_count = sum(1 for line in text.splitlines() if _DUST_AGGREGATE_RE.match(line))
    if dust_count == 1:
        return []
    return [
        InvariantViolation(
            code="dust_aggregate",
            detail=f"expected exactly one dust aggregate line (got {dust_count})",
        )
    ]


def _validate_fail_closed_anchor(text: str) -> list[InvariantViolation]:
    if not _contains_fail_closed_anchor(text):
        return []
    return [
        InvariantViolation(
            code="fail_closed_anchor",
            detail="fail-closed anchors must bypass normal board render validation",
        )
    ]


def validate_render_invariants(
    text: str,
    ctx: BoardBriefContext,
    *,
    phase: BoardBriefPhase,
) -> list[InvariantViolation]:
    """Return structural render invariant violations for final markdown."""
    violations: list[InvariantViolation] = []
    violations.extend(_validate_funding_rows(text))
    violations.extend(_validate_runway_excludes_unverified_cap(text, ctx))
    violations.extend(_validate_ab_anchors(text, phase=phase))
    if phase == "cio_pending":
        violations.extend(_validate_cio_pending_invariants(text, ctx))
    violations.extend(_validate_dust_aggregate(text))
    violations.extend(_validate_fail_closed_anchor(text))
    return violations


def _check_forbidden_patterns(text: str) -> list[InvariantViolation]:
    violations: list[InvariantViolation] = []
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(text):
            violations.append(
                InvariantViolation(
                    code="forbidden_pattern",
                    detail=pattern.pattern,
                )
            )
    return violations


def _postprocess_and_validate_render(
    *,
    text: str,
    ctx: BoardBriefContext,
    phase: BoardBriefPhase,
    router: RenderRouter,
    text_postprocessor: Callable[[str], str] | None,
) -> str:
    if text_postprocessor:
        text = text_postprocessor(text)

    violations = _check_forbidden_patterns(text)
    violations.extend(validate_render_invariants(text, ctx, phase=phase))
    if violations:
        message = "; ".join(f"{item.code}: {item.detail}" for item in violations)
        logger.error(
            "Board brief render invariant violation",
            extra={
                "phase": phase,
                "violations": [item.__dict__ for item in violations],
            },
        )
        router.route_ops_escalation(message)
        raise RenderInvariantError(violations)
    return text


def _default_gate_results() -> dict[str, GateResult | N8nG2GatePayload]:
    return {
        "G1": GateResult(status="pending", detail="G1 데이터 충분성 평가 대기"),
        "G2": N8nG2GatePayload(
            passed=True,
            status="pending",
            detail="G2 funding intent 평가 대기",
        ),
        "G3": GateResult(status="tbd", detail="TBD (S3)"),
        "G4": GateResult(status="tbd", detail="TBD (S4)"),
        "G5": GateResult(status="tbd", detail="TBD (S5)"),
        "G6": GateResult(status="tbd", detail="TBD (S6)"),
    }


def _build_gate_results(
    ctx: BoardBriefContext,
) -> dict[str, GateResult | N8nG2GatePayload]:
    """Return complete G1-G6 rows for CIO pending embeds."""
    gates = _default_gate_results()
    gates.update(ctx.gate_results or {})
    return gates


def _has_v2_funding_context(ctx: BoardBriefContext) -> bool:
    return bool(
        ctx.exchange_krw
        or ctx.unverified_cap
        or ctx.next_obligation
        or ctx.tier_scenarios
    )


def _cash_runway_days(ctx: BoardBriefContext) -> float | None:
    if ctx.manual_cash_runway_days is not None:
        return ctx.manual_cash_runway_days
    if _has_v2_funding_context(ctx) and ctx.daily_burn_krw > 0:
        return ctx.exchange_krw / ctx.daily_burn_krw
    if ctx.daily_burn_krw > 0:
        return ctx.manual_cash_krw / ctx.daily_burn_krw
    return None


def _funding_amount(board_response: BoardFundingResponse | None) -> float:
    return board_response.amount if board_response else 0


def _funding_verified(
    ctx: BoardBriefContext, board_response: BoardFundingResponse | None
) -> bool:
    return bool(
        (board_response and board_response.manual_cash_verified)
        or (ctx.unverified_cap and ctx.unverified_cap.verified_by_boss_today)
    )


def _format_g2_lines(lines: list[str], *, amount: float, days: int) -> list[str]:
    return [line.format(amount=f"{amount:,.0f}", days=days) for line in lines]


def resolve_funding_intent(
    ctx: BoardBriefContext,
    board_response: BoardFundingResponse | None,
) -> tuple[FundingIntent, list[str]]:
    """Resolve G2 funding intent and the exact phrase set to render.

    >>> ctx = BoardBriefContext(exchange_krw=100, daily_burn_krw=10)
    >>> resolve_funding_intent(ctx, None)[0]
    'runway_recovery'
    >>> response = BoardFundingResponse(
    ...     amount=100, target="BTC", funding_intent="new_buy", manual_cash_verified=True
    ... )
    >>> ctx = BoardBriefContext(
    ...     exchange_krw=100, daily_burn_krw=10,
    ...     next_obligation={"date": "2026-04-24", "days_remaining": 7, "cash_needed_until": 50}
    ... )
    >>> resolve_funding_intent(ctx, response)[0]
    'new_buy'
    """
    amount = _funding_amount(board_response)
    days = ctx.next_obligation.days_remaining if ctx.next_obligation else 0
    runway_lines = _format_g2_lines(
        G2_RUNWAY_FUEL_LINES,
        amount=amount,
        days=days,
    )
    new_budget_lines = _format_g2_lines(
        G2_NEW_BUDGET_LINES,
        amount=amount,
        days=days,
    )

    verified_amount = amount if _funding_verified(ctx, board_response) else 0
    if (
        ctx.next_obligation
        and ctx.next_obligation.cash_needed_until > ctx.exchange_krw + verified_amount
    ):
        return "runway_recovery", runway_lines

    if (
        board_response
        and board_response.target
        and _funding_verified(ctx, board_response)
    ):
        return "new_buy", new_budget_lines

    return "runway_recovery", runway_lines


def _build_concentration_lines(ctx: BoardBriefContext) -> list[str]:
    if not ctx.weights_top_n:
        return ["- 상위 비중 데이터 없음"]
    return [
        f"- {item.symbol}: {_fmt_pct(item.weight_pct)}"
        for item in ctx.weights_top_n[:5]
    ]


def _build_candidate_lines(ctx: BoardBriefContext) -> list[str]:
    candidates = [holding for holding in ctx.holdings if not holding.dust]
    if not candidates:
        return ["- execution-actionable 매도/축소 후보 없음"]
    return [
        f"- {holding.symbol}: {_fmt_krw(holding.current_krw_value)}"
        for holding in candidates[:5]
    ]


def _build_dust_lines(ctx: BoardBriefContext) -> list[str]:
    dust_items = ctx.dust_items or [holding for holding in ctx.holdings if holding.dust]
    dust_total = sum(float(item.current_krw_value) for item in dust_items)
    portfolio_total = sum(float(holding.current_krw_value) for holding in ctx.holdings)
    dust_pct = (dust_total / portfolio_total * 100) if portfolio_total > 0 else 0
    return [
        f"🧹 Dust {len(dust_items)}종목 · 합계 {_fmt_krw(dust_total)} · 포트폴리오 {dust_pct:.2f}%"
    ]


def _build_funding_lines(ctx: BoardBriefContext) -> list[str]:
    runway_days = _cash_runway_days(ctx)
    unverified_cap = ctx.unverified_cap.amount if ctx.unverified_cap else None
    unverified_badges = []
    if ctx.unverified_cap and ctx.unverified_cap.stale_warning:
        unverified_badges.append("stale_warning")
    if ctx.unverified_cap and ctx.unverified_cap.verified_by_boss_today:
        unverified_badges.append("verified_by_boss_today")
    badge_text = f" ({' / '.join(unverified_badges)})" if unverified_badges else ""
    obligation = (
        f"{ctx.next_obligation.date.isoformat()} / D-{ctx.next_obligation.days_remaining} / "
        f"{_fmt_krw(ctx.next_obligation.cash_needed_until)}"
        if ctx.next_obligation
        else "-"
    )
    runway_formula = (
        f"{_fmt_krw(ctx.exchange_krw)} / {_fmt_krw(ctx.daily_burn_krw)} = "
        f"{_fmt_days(runway_days)}"
        if runway_days is not None and ctx.daily_burn_krw > 0
        else "-"
    )
    return [
        f"- 거래소 KRW: {_fmt_krw(ctx.exchange_krw)}",
        f"- 미확인 cap (보스 확인 전): {_fmt_krw(unverified_cap)}{badge_text}",
        f"- 일일 소진 (daily_burn): {_fmt_krw(ctx.daily_burn_krw)}",
        f"- 다음 의무 (date / days_remaining / cash_needed_until): {obligation}",
        f"- runway 산식: {runway_formula}",
    ]


def _build_tier_lines(ctx: BoardBriefContext) -> list[str]:
    if not ctx.tier_scenarios:
        return ["tier_scenarios 미수신, 입금 시나리오 산출 보류"]
    obligation = (
        f"{ctx.next_obligation.date.isoformat()} / D-{ctx.next_obligation.days_remaining}"
        if ctx.next_obligation
        else "-"
    )
    lines = [
        (
            "deposit_amount | next_obligation | cash_needed_until | "
            "cushion_after_obligation | target_exchange_krw | buffer_days (보조)"
        )
    ]
    for scenario in ctx.tier_scenarios:
        cash_needed = (
            ctx.next_obligation.cash_needed_until if ctx.next_obligation else None
        )
        lines.append(
            f"{_fmt_krw(scenario.deposit_amount)} | {obligation} | "
            f"{_fmt_krw(cash_needed)} | {_fmt_krw(scenario.cushion_after_obligation)} | "
            f"{_fmt_krw(scenario.target_exchange_krw)} | {scenario.buffer_days}"
        )
    return lines


def _build_board_response_lines(ctx: BoardBriefContext) -> list[str]:
    """Render the board funding response section when one is available.

    `amount == 0` means the board explicitly responded with "자금 지원 안 함";
    funding_intent in that case is optional since no funding path is selected.
    """
    response = ctx.board_response
    if response is None:
        return []
    if response.amount == 0:
        detail_parts = ["자금 지원 안 함 (0 KRW)"]
        if response.funding_intent:
            detail_parts.append(f"intent: {response.funding_intent}")
        if response.manual_cash_verified:
            detail_parts.append("manual_cash_verified")
        return [
            "🗳️ 보드 응답",
            f"- {' · '.join(detail_parts)} — 경로 A 지속, 신규 매수 차단",
        ]
    target = response.target or "-"
    intent = response.funding_intent or "-"
    verified = " · manual_cash_verified" if response.manual_cash_verified else ""
    return [
        "🗳️ 보드 응답",
        f"- {_fmt_krw(response.amount)} → {target} (intent: {intent}){verified}",
    ]


def _build_tc_preliminary_text(ctx: BoardBriefContext) -> str:
    """Build TC preliminary text without recommendation or gate sections."""
    lines = [
        "📊 TC Preliminary — 입금 약속 반영 시나리오 (pledged, 거래소 미반영)",
        "",
        f"요약: 경로 A·B 병행 가능. {FRAMING_AB_PATH_NON_EXCLUSIVE}",
        "",
        "💵 자금 현황",
        *_build_funding_lines(ctx),
        "",
        "📌 쏠림/편중",
        *_build_concentration_lines(ctx),
        "",
        "🔻 매도/축소 후보",
        *_build_candidate_lines(ctx),
    ]
    dust_lines = _build_dust_lines(ctx)
    if dust_lines:
        lines.extend(["", *dust_lines])
    board_lines = _build_board_response_lines(ctx)
    if board_lines:
        lines.extend(["", *board_lines])
    lines.extend(
        [
            "",
            "경로 A: 신규 매수 없이 현금 runway 회복 우선.",
            "경로 B: board funding 확인 후 CIO pending decision에서 gate 재평가.",
            "",
            "§7 Tier table",
            *_build_tier_lines(ctx),
            PATH_SECTION_AB_REPEAT,
        ]
    )
    return "\n".join(lines)


def _gate_status_label(gate: GateResult | N8nG2GatePayload) -> str:
    if isinstance(gate, N8nG2GatePayload):
        return "pass" if gate.passed else "fail"
    return gate.status


def _gate_detail(gate: GateResult | N8nG2GatePayload) -> str:
    if isinstance(gate, N8nG2GatePayload):
        return gate.blocking_reason or gate.detail or ""
    return gate.detail


def _build_cio_pending_text(ctx: BoardBriefContext) -> str:
    """Build CIO pending text with recommendation placeholder and G1-G6 rows."""
    gates = _build_gate_results(ctx)
    g2 = gates["G2"]
    g2_failed = isinstance(g2, N8nG2GatePayload) and not g2.passed
    _, g2_lines = resolve_funding_intent(ctx, ctx.board_response)
    no_funding = ctx.board_response is not None and ctx.board_response.amount == 0
    if no_funding:
        recommendation = "🚫 신규 매수 차단 — 보드 응답: 자금 지원 없음 (0 KRW)"
    elif g2_failed:
        recommendation = "🚫 신규 매수 차단 — G2 fail"
    else:
        recommendation = "CIO 의견 대기 중 — gate 결과 확인 후 action 확정"
    lines = [
        _build_tc_preliminary_text(ctx),
        "",
        "🎯 권고",
        recommendation,
        *g2_lines,
        "",
        "📊 Gate 판정 결과",
    ]
    for gate_name in ("G1", "G2", "G3", "G4", "G5", "G6"):
        gate = gates[gate_name]
        detail = _gate_detail(gate)
        lines.append(
            f"- {gate_name}: {_gate_status_label(gate)}"
            + (f" — {detail}" if detail else "")
        )

    lines.extend(
        [
            "",
            BOARD_QUESTIONS_TEMPLATE,
        ]
    )
    return "\n".join(lines)


def _build_board_embed(*, title: str, text: str, color: int) -> dict[str, Any]:
    return {
        "title": title,
        "description": text[:4000],
        "color": color,
    }


def build_tc_preliminary(
    payload: BoardBriefContext | dict[str, Any],
    *,
    router: RenderRouter | None = None,
    text_postprocessor: Callable[[str], str] | None = None,
) -> BoardBriefRender:
    """Render the first TC follow-up phase."""
    ctx = _extract_followup_context(payload)
    router = router or RenderRouter()
    if missing := _missing_required_context(ctx):
        return _route_fail_closed(
            ctx=ctx,
            phase="tc_preliminary",
            field=missing[0],
            reason=missing[1],
            router=router,
        )
    generated_at = ctx.generated_at or now_kst().replace(microsecond=0)
    text = _build_tc_preliminary_text(ctx)
    text = _postprocess_and_validate_render(
        text=text,
        ctx=ctx,
        phase="tc_preliminary",
        router=router,
        text_postprocessor=text_postprocessor,
    )
    return BoardBriefRender(
        phase="tc_preliminary",
        embed=_build_board_embed(
            title="📊 TC Preliminary — 입금 약속 반영 시나리오 (pledged, 거래소 미반영)",
            text=text,
            color=0x3498DB,
        ),
        text=text,
        gate_results=None,
        generated_at=generated_at,
    )


def build_cio_pending_decision(
    payload: BoardBriefContext | dict[str, Any],
    *,
    router: RenderRouter | None = None,
    text_postprocessor: Callable[[str], str] | None = None,
) -> BoardBriefRender:
    """Render the second CIO follow-up phase."""
    ctx = _extract_followup_context(payload)
    router = router or RenderRouter()
    if missing := _missing_required_context(ctx):
        return _route_fail_closed(
            ctx=ctx,
            phase="cio_pending",
            field=missing[0],
            reason=missing[1],
            router=router,
        )
    generated_at = ctx.generated_at or now_kst().replace(microsecond=0)
    gate_results = _build_gate_results(ctx)
    funding_intent, _ = resolve_funding_intent(ctx, ctx.board_response)
    ctx = ctx.model_copy(
        update={"funding_intent": funding_intent, "gate_results": gate_results}
    )
    text = _build_cio_pending_text(ctx)
    text = _postprocess_and_validate_render(
        text=text,
        ctx=ctx,
        phase="cio_pending",
        router=router,
        text_postprocessor=text_postprocessor,
    )
    return BoardBriefRender(
        phase="cio_pending",
        embed=_build_board_embed(
            title="🎯 CIO Pending Decision — Gate 판정 결과",
            text=text,
            color=0xF1C40F,
        ),
        text=text,
        funding_intent=funding_intent,
        gate_results=gate_results,
        generated_at=generated_at,
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

    results_s1 = await asyncio.gather(
        pending_task,
        portfolio_task,
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


__all__ = [
    "InvariantViolation",
    "RenderInvariantError",
    "RenderRouter",
    "build_cio_pending_decision",
    "build_tc_preliminary",
    "fetch_daily_brief",
    "validate_render_invariants",
]
