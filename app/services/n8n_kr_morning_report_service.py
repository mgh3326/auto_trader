from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.analysis_tool_handlers import screen_stocks_impl
from app.services.brokers.kis.client import KISClient
from app.services.n8n_formatting import (
    fmt_amount,
    fmt_date_with_weekday,
    fmt_pnl,
)
from app.services.n8n_pending_orders_service import fetch_pending_orders

logger = logging.getLogger(__name__)


async def fetch_kr_morning_report(
    include_screen: bool = True,
    screen_strategy: str | None = None,
    include_pending: bool = True,
    top_n: int = 20,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Fetch and build the KR morning report."""
    as_of_dt = (as_of or now_kst()).replace(microsecond=0)

    portfolio_task = asyncio.create_task(_get_portfolio_overview())
    cash_task = asyncio.create_task(_fetch_kis_cash_balance())

    screen_task = (
        asyncio.create_task(
            _fetch_screening(screen_strategy=screen_strategy, top_n=top_n)
        )
        if include_screen
        else None
    )

    pending_task = (
        asyncio.create_task(
            fetch_pending_orders(market="kr", include_current_price=True)
        )
        if include_pending
        else None
    )

    # Wait for core tasks
    results = await asyncio.gather(portfolio_task, cash_task, return_exceptions=True)
    portfolio_raw = (
        results[0] if not isinstance(results[0], Exception) else {"positions": []}
    )
    kis_cash = results[1] if not isinstance(results[1], Exception) else 0.0

    errors: list[dict[str, str]] = []

    if isinstance(results[0], Exception):
        logger.error("Failed to fetch portfolio overview: %s", results[0])
        errors.append({"source": "portfolio", "error": str(results[0])})
    if isinstance(results[1], Exception):
        logger.error("Failed to fetch KIS cash balance: %s", results[1])
        errors.append({"source": "cash", "error": str(results[1])})

    # Process holdings
    holdings = _build_holdings(portfolio_raw)

    # Process cash balance
    cash_balance = {
        "kis_krw": kis_cash,
        "kis_krw_fmt": fmt_amount(kis_cash),
        "toss_krw": None,
        "toss_krw_fmt": "수동 관리",
        "total_krw": kis_cash,
        "total_krw_fmt": fmt_amount(kis_cash),
    }

    # Process screening
    screening_data = {
        "total_scanned": 0,
        "top_n": top_n,
        "strategy": screen_strategy,
        "results": [],
        "summary": {},
    }
    if screen_task:
        try:
            screening_data = await screen_task
        except Exception as exc:
            logger.error("Failed to fetch screening: %s", exc)
            errors.append({"source": "screening", "error": str(exc)})

    # Process pending orders
    pending_data = {"total": 0, "buy_count": 0, "sell_count": 0, "orders": []}
    if pending_task:
        try:
            pending_raw = await pending_task
            pending_data = _build_pending_summary(pending_raw)
        except Exception as exc:
            logger.error("Failed to fetch pending orders: %s", exc)
            errors.append({"source": "pending_orders", "error": str(exc)})

    # Build brief text
    brief_text = _build_brief_text(
        date_fmt=fmt_date_with_weekday(as_of_dt),
        holdings=holdings,
        cash_balance=cash_balance,
        screening=screening_data,
        pending_orders=pending_data,
        include_screen=include_screen,
        include_pending=include_pending,
    )

    success = len(errors) == 0

    return {
        "success": success,
        "as_of": as_of_dt.isoformat(),
        "date_fmt": fmt_date_with_weekday(as_of_dt),
        "holdings": holdings,
        "cash_balance": cash_balance,
        "screening": screening_data,
        "pending_orders": pending_data,
        "brief_text": brief_text,
        "errors": errors,
    }


async def _get_portfolio_overview() -> dict[str, Any]:
    from app.services.portfolio_overview_service import PortfolioOverviewService

    async with AsyncSessionLocal() as session:
        service = PortfolioOverviewService(session)
        return await service.get_overview(user_id=1)


async def _fetch_kis_cash_balance() -> float:
    client = KISClient()
    payload = await client.inquire_domestic_cash_balance()
    return float(payload.get("stck_cash_ord_psbl_amt") or 0)


async def _fetch_screening(screen_strategy: str | None, top_n: int) -> dict[str, Any]:
    if screen_strategy is None:
        raw = await screen_stocks_impl(
            market="kr",
            strategy=None,
            max_rsi=30.0,
            sort_by="rsi",
            sort_order="asc",
            limit=max(top_n, 30),
        )
        strategy_label = "oversold"
    else:
        raw = await screen_stocks_impl(
            market="kr",
            strategy=screen_strategy,
            limit=max(top_n, 30),
        )
        strategy_label = screen_strategy
    return _normalize_screening(raw, top_n, strategy_label)


def _build_holdings(portfolio_raw: dict[str, Any]) -> dict[str, Any]:
    positions = portfolio_raw.get("positions", [])
    kr_positions = [p for p in positions if p.get("market_type") == "KR"]

    kis_positions = []
    toss_positions = []

    for pos in kr_positions:
        components = pos.get("components") or []
        for component in components:
            qty = float(component.get("quantity") or 0)
            if qty <= 0:
                continue

            broker = str(component.get("broker") or "").lower()
            account_key = str(component.get("account_key") or "").lower()

            normalized = {
                "symbol": pos["symbol"],
                "name": pos["name"],
                "quantity": qty,
                "avg_price": float(component.get("avg_price") or 0),
                "current_price": float(component.get("current_price") or 0) or float(pos.get("current_price") or 0) or None,
                "eval_krw": float(component.get("evaluation") or 0) or None,
                "pnl_pct": round(float(component["profit_rate"]) * 100, 1)
                if component.get("profit_rate") is not None
                else None,
                "pnl_fmt": fmt_pnl(
                    float(component["profit_rate"]) * 100
                    if component.get("profit_rate") is not None
                    else None
                ),
                "eval_fmt": fmt_amount(float(component.get("evaluation") or 0)),
                "account": component.get("account_name") or broker,
            }

            if broker == "kis" or "kis" in account_key:
                kis_positions.append(normalized)
            elif broker == "toss":
                toss_positions.append(normalized)

    def summarize(positions_list):
        total_eval = sum(p["eval_krw"] or 0 for p in positions_list)
        # Weighted PNL
        total_cost = sum((p["avg_price"] * p["quantity"]) for p in positions_list)
        total_pnl_pct = None
        if total_cost > 0:
            total_pnl_pct = ((total_eval - total_cost) / total_cost) * 100

        return {
            "total_count": len(positions_list),
            "total_eval_krw": total_eval,
            "total_eval_fmt": fmt_amount(total_eval),
            "total_pnl_pct": round(total_pnl_pct, 2)
            if total_pnl_pct is not None
            else None,
            "total_pnl_fmt": fmt_pnl(total_pnl_pct),
            "positions": positions_list,
        }

    kis_summary = summarize(kis_positions)
    toss_summary = summarize(toss_positions)
    combined_summary = summarize(kis_positions + toss_positions)

    return {
        "kis": kis_summary,
        "toss": toss_summary,
        "combined": combined_summary,
    }


def _normalize_screening(
    raw: dict[str, Any], top_n: int, strategy: str | None
) -> dict[str, Any]:
    results = raw.get("results", [])

    normalized = []
    for r in results:
        normalized.append(
            {
                "symbol": r.get("symbol") or r.get("code") or "",
                "name": r.get("name") or "",
                "current_price": r.get("current_price"),
                "rsi": r.get("rsi"),
                "change_pct": r.get("change_rate") or r.get("change_pct"),
                "volume_ratio": r.get("volume_ratio"),
                "market_cap_fmt": r.get("market_cap_fmt"),
                "signal": r.get("signal"),
                "sector": r.get("sector"),
            }
        )

    # Sort by RSI ascending (oversold first)
    normalized.sort(key=lambda x: (x["rsi"] is None, x["rsi"] or 999))
    trimmed = normalized[:top_n]

    rsi_vals = [r["rsi"] for r in trimmed if r["rsi"] is not None]
    summary = {
        "oversold_count": len([r for r in rsi_vals if r < 30]),
        "overbought_count": len([r for r in rsi_vals if r > 70]),
        "avg_rsi": round(sum(rsi_vals) / len(rsi_vals), 1) if rsi_vals else None,
    }

    return {
        "total_scanned": raw.get("total_count", 0),
        "top_n": top_n,
        "strategy": strategy,
        "results": trimmed,
        "summary": summary,
    }


def _build_pending_summary(raw: dict[str, Any]) -> dict[str, Any]:
    summary = raw.get("summary") or {}
    return {
        "total": int(summary.get("total") or 0),
        "buy_count": int(summary.get("buy_count") or 0),
        "sell_count": int(summary.get("sell_count") or 0),
        "total_buy_fmt": summary.get("total_buy_fmt", "0"),
        "total_sell_fmt": summary.get("total_sell_fmt", "0"),
        "orders": list(raw.get("orders") or [])[:10],
    }


def _build_brief_text(
    date_fmt: str,
    holdings: dict[str, Any],
    cash_balance: dict[str, Any],
    screening: dict[str, Any],
    pending_orders: dict[str, Any],
    include_screen: bool,
    include_pending: bool,
) -> str:
    sections = [f"📊 KR 모닝 리포트 — {date_fmt}"]

    # Holdings
    h_kis = holdings["kis"]
    h_toss = holdings["toss"]
    h_comb = holdings["combined"]

    sections.append("\n💼 국내주식 잔고")
    sections.append(
        f"KIS: {h_kis['total_eval_fmt']} ({h_kis['total_pnl_fmt']}) — {h_kis['total_count']}종목"
    )
    sections.append(
        f"토스: {h_toss['total_eval_fmt']} ({h_toss['total_pnl_fmt']}) — {h_toss['total_count']}종목"
    )
    sections.append(f"합산: {h_comb['total_eval_fmt']} ({h_comb['total_pnl_fmt']})")

    # Cash
    sections.append("\n💰 가용 현금")
    sections.append(
        f"KIS: {cash_balance['kis_krw_fmt']} | 토스: 수동 관리 | 합산: {cash_balance['total_krw_fmt']}"
    )

    # Pending
    if include_pending and pending_orders["total"] > 0:
        sections.append(f"\n⏳ 미체결 주문 ({pending_orders['total']}건)")
        for order in pending_orders["orders"][:5]:
            sections.append(f"• {order.get('summary_line')}")
        if pending_orders["total"] > 5:
            sections.append(f"...외 {pending_orders['total'] - 5}건")

    # Screening
    if include_screen and screening["results"]:
        sections.append("\n🔍 실시간 스크리닝 (RSI 저점 순)")
        for r in screening["results"][:5]:
            rsi_str = f"RSI {r['rsi']:.1f}" if r["rsi"] is not None else "RSI -"
            chg_str = f"{r['change_pct']:+.1f}%" if r["change_pct"] is not None else "-"
            sections.append(f"• {r['name']}({r['symbol']}): {rsi_str}, {chg_str}")

    sections.append("\n상세 분석은 스레드에서 진행합니다.")
    return "\n".join(sections)
