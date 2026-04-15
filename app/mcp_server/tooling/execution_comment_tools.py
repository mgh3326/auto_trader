"""Execution comment formatting MCP tool implementations."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _format_fill_comment(
    symbol: str,
    side: str,
    filled_qty: float,
    filled_price: float,
    currency: str,
    journal_context: dict[str, Any] | None,
    market_brief: str | None,
) -> str:
    side_label = "매수" if side == "buy" else "매도"
    side_emoji = "\U0001f7e2" if side == "buy" else "\U0001f534"

    lines = [
        f"## {side_emoji} {symbol} {side_label} 체결",
        "",
        f"- **수량**: {filled_qty:,.4g}",
        f"- **체결가**: {currency}{filled_price:,.2f}",
        f"- **체결금액**: {currency}{filled_qty * filled_price:,.0f}",
        f"- **시각**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S KST')}",
    ]

    if journal_context:
        lines.append("")
        lines.append("### 투자 논거")
        if journal_context.get("thesis"):
            lines.append(f"- **논거**: {journal_context['thesis']}")
        if journal_context.get("strategy"):
            lines.append(f"- **전략**: {journal_context['strategy']}")
        if journal_context.get("target_price") is not None:
            lines.append(
                f"- **목표가**: {currency}{journal_context['target_price']:,.2f}"
            )
        if journal_context.get("stop_loss") is not None:
            lines.append(f"- **손절가**: {currency}{journal_context['stop_loss']:,.2f}")
        if journal_context.get("min_hold_days") is not None:
            lines.append(f"- **최소 보유**: {journal_context['min_hold_days']}일")

    if market_brief:
        lines.append("")
        lines.append("### 시장 컨텍스트")
        lines.append(market_brief)

    return "\n".join(lines)


def _format_follow_up_comment(
    symbol: str,
    side: str,
    filled_qty: float,
    filled_price: float,
    currency: str,
    journal_context: dict[str, Any] | None,
    market_brief: str | None,
    next_action: str | None,
    analysis_summary: str | None,
) -> str:
    side_label = "매수" if side == "buy" else "매도"

    lines = [
        f"## 후속 판단: {symbol} {side_label} 체결 후",
        "",
        f"- **체결**: {filled_qty:,.4g} @ {currency}{filled_price:,.2f}",
    ]

    if journal_context:
        entry_price = journal_context.get("entry_price")
        if entry_price and side == "sell":
            pnl_pct = (filled_price / entry_price - 1) * 100
            pnl_sign = "+" if pnl_pct >= 0 else ""
            lines.append(f"- **수익률**: {pnl_sign}{pnl_pct:.2f}%")

    if analysis_summary:
        lines.append("")
        lines.append("### 분석 요약")
        lines.append(analysis_summary)

    if market_brief:
        lines.append("")
        lines.append("### 시장 컨텍스트")
        lines.append(market_brief)

    if next_action:
        lines.append("")
        lines.append("### 다음 행동")
        lines.append(f"**{next_action}**")

    return "\n".join(lines)


async def format_execution_comment(
    stage: str,
    symbol: str,
    side: str,
    filled_qty: float,
    filled_price: float,
    currency: str = "₩",
    journal_context: dict[str, Any] | None = None,
    market_brief: str | None = None,
    next_action: str | None = None,
    analysis_summary: str | None = None,
) -> dict[str, Any]:
    """Format a structured Markdown comment for trade execution events.

    stage: 'fill' for immediate fill notification, 'follow_up' for post-fill analysis.
    symbol: the traded symbol.
    side: 'buy' or 'sell'.
    filled_qty: quantity filled.
    filled_price: price at which the fill occurred.
    currency: currency symbol (default ₩).
    journal_context: dict with thesis, strategy, target_price, stop_loss, etc.
    market_brief: short market context string.
    next_action: recommended next action (hold / 추가매수 / 익절 / 손절).
    analysis_summary: post-fill analysis summary text.
    """
    if stage not in ("fill", "follow_up"):
        return {"success": False, "error": "stage must be 'fill' or 'follow_up'"}
    if side not in ("buy", "sell"):
        return {"success": False, "error": "side must be 'buy' or 'sell'"}
    if filled_qty <= 0:
        return {"success": False, "error": "filled_qty must be positive"}
    if filled_price <= 0:
        return {"success": False, "error": "filled_price must be positive"}

    try:
        if stage == "fill":
            text = _format_fill_comment(
                symbol=symbol,
                side=side,
                filled_qty=filled_qty,
                filled_price=filled_price,
                currency=currency,
                journal_context=journal_context,
                market_brief=market_brief,
            )
        else:
            text = _format_follow_up_comment(
                symbol=symbol,
                side=side,
                filled_qty=filled_qty,
                filled_price=filled_price,
                currency=currency,
                journal_context=journal_context,
                market_brief=market_brief,
                next_action=next_action,
                analysis_summary=analysis_summary,
            )

        return {
            "success": True,
            "stage": stage,
            "markdown": text,
        }
    except Exception as exc:
        return {"success": False, "error": f"format_execution_comment failed: {exc}"}
