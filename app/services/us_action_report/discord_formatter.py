from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.core.symbol import to_db_symbol
from app.schemas.us_action_report import (
    KISUSAccountSnapshot,
    USHeldPositionActionCard,
    USHeldPositionActionCards,
    USNewBuyCandidateCard,
    USNewBuyCandidateCards,
    USOpenOrder,
)

_ACTION_LABELS = {
    "sell": "매도 검토",
    "trim": "일부 익절/축소 검토",
    "hold": "보유/대기",
    "add": "추가매수 검토",
    "watch": "관찰",
}


def _normal_symbol(symbol: str) -> str:
    return to_db_symbol(symbol.upper())


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "확인 불가"
    return f"${value:,.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "확인 불가"
    return f"{value:+.1f}%"


def _fmt_qty(value: float | None) -> str:
    if value is None:
        return "확인 불가"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _short_join(items: Iterable[str], *, empty: str = "특이사항 없음") -> str:
    filtered = [str(item).strip() for item in items if str(item).strip()]
    return "; ".join(filtered) if filtered else empty


def _order_line(order: USOpenOrder) -> str:
    side_label = {"buy": "매수", "sell": "매도", "unknown": "방향 확인 필요"}.get(
        order.side, "방향 확인 필요"
    )
    order_id = f" · order_id={order.order_id}" if order.order_id else ""
    return f"- {order.symbol} {side_label} 미체결 {_fmt_qty(order.pending_qty)}주{order_id}"


def _holding_action_line(card: USHeldPositionActionCard) -> str:
    label = _ACTION_LABELS.get(card.action, card.action)
    trim = f" · trim {card.suggested_trim_pct}%" if card.suggested_trim_pct else ""
    target_stop = (
        f" · 목표 {_fmt_usd(card.target_price_usd)} / 손절 {_fmt_usd(card.stop_loss_usd)}"
        if card.target_price_usd is not None or card.stop_loss_usd is not None
        else ""
    )
    reasoning = _short_join(card.reason_codes, empty="reason code 없음")
    risk = _short_join([*card.missing_context_codes, *card.warnings], empty="추가 경고 없음")
    return (
        f"- **{card.symbol}** ({card.display_name}) — {label}{trim}\n"
        f"  수량 {_fmt_qty(card.quantity)}주 / KIS 매도가능 {_fmt_qty(card.sellable_qty)}주 "
        f"/ 실행 전 검토수량 {_fmt_qty(card.executable_qty)}주\n"
        f"  P/L {_fmt_pct(card.pnl_rate)} ({_fmt_usd(card.pnl_usd)}) "
        f"· 현재가 {_fmt_usd(card.last_price_usd)}{target_stop}\n"
        f"  근거: {reasoning}\n"
        f"  주의: {risk}"
    )


def _candidate_line(card: USNewBuyCandidateCard) -> str:
    risk = _short_join([*card.risk_notes, *card.data_warnings], empty="추가 경고 없음")
    return (
        f"- **{card.symbol}** {card.name or ''} — {card.priority_label} / {card.label}\n"
        f"  예상 {_fmt_qty(card.quantity_estimate)}주, {_fmt_usd(card.notional_estimate_usd)} "
        f"@ {_fmt_usd(card.price_usd)} · {card.sizing_note}\n"
        f"  목표 {_fmt_usd(card.target_price_usd)} / 손절 {_fmt_usd(card.stop_loss_usd)} "
        f"/ 최소보유 {card.min_hold_days}일\n"
        f"  논리: {card.thesis}\n"
        f"  주의: {risk}"
    )


def _summary_lines(snapshot: KISUSAccountSnapshot) -> list[str]:
    total_value = sum(holding.value_usd or 0.0 for holding in snapshot.holdings)
    total_pnl = sum(holding.pnl_usd or 0.0 for holding in snapshot.holdings)
    missing_prices = [holding.symbol for holding in snapshot.holdings if holding.price_state == "missing"]
    return [
        "## KIS live US action report (preview only)",
        "### 1) KIS live account summary",
        f"- Source of truth: KIS live ({snapshot.source}); captured_at={snapshot.captured_at.isoformat()}",
        f"- USD cash: {_fmt_usd(snapshot.usd_cash)} / buying power: {_fmt_usd(snapshot.usd_buying_power)}",
        f"- KIS tradeable holdings: {len(snapshot.holdings)} symbols, value {_fmt_usd(total_value)}, P/L {_fmt_usd(total_pnl)}",
        f"- Open orders: {len(snapshot.open_orders)} pending rows",
        f"- Price gaps: {_short_join(missing_prices, empty='none')}",
    ]


def build_us_action_report_discord_message(
    *,
    account_snapshot: KISUSAccountSnapshot,
    held_actions: Sequence[USHeldPositionActionCard] | USHeldPositionActionCards,
    new_buy_candidates: Sequence[USNewBuyCandidateCard] | USNewBuyCandidateCards,
    manual_reference_symbols: Iterable[str] | None = None,
    title_suffix: str | None = None,
) -> str:
    """Format a read-only Discord/operator report for KIS-live US actions.

    This function is deliberately pure: it formats already-collected account,
    held-position, and new-buy outputs. It does not submit/cancel/modify live
    orders and does not write watch/order-intent or journal state.
    """

    lines = _summary_lines(account_snapshot)
    if title_suffix:
        lines[0] = f"{lines[0]} — {title_suffix}"

    lines.extend(["", "### 2) Tradeable holdings actions"])
    if held_actions:
        for card in held_actions:
            lines.append(_holding_action_line(card))
    else:
        lines.append("- KIS live tradeable holding action 없음")

    manual_symbols = sorted({_normal_symbol(symbol) for symbol in (manual_reference_symbols or []) if symbol})
    lines.extend(["", "### 3) Manual/reference caveat"])
    if manual_symbols:
        lines.append(
            "- Manual/Toss/reference symbols: "
            + ", ".join(manual_symbols)
            + " — 참고용이며 KIS 매도가능/거래가능 수량에 포함하지 않음."
        )
    else:
        lines.append("- Manual/Toss/reference balances are reference-only and not counted as KIS tradeable quantity.")
    lines.append("- Sell/trim quantities above are capped to KIS live sellable quantity only.")

    lines.extend(["", "### 4) New-buy candidates"])
    candidate_warnings = getattr(new_buy_candidates, "warnings", [])
    if new_buy_candidates:
        for card in new_buy_candidates:
            lines.append(_candidate_line(card))
    else:
        lines.append("- 신규매수 검토 후보 없음")
    if candidate_warnings:
        lines.append("- Candidate builder warnings: " + _short_join(candidate_warnings))

    lines.extend(["", "### 5) Open-order and journal warnings"])
    if account_snapshot.open_orders:
        lines.extend(_order_line(order) for order in account_snapshot.open_orders)
    else:
        lines.append("- KIS live open orders: none")

    warning_lines: list[str] = []
    warning_lines.extend(account_snapshot.warnings)
    warning_lines.extend(getattr(held_actions, "warnings", []))
    warning_lines.extend(candidate_warnings)
    if warning_lines:
        for warning in dict.fromkeys(warning_lines):
            lines.append(f"- warning: {warning}")
    else:
        lines.append("- Snapshot/journal warnings: none")

    lines.extend(
        [
            "",
            "### 6) Order-before-execution checklist",
            "- Reconfirm KIS live cash, buying power, holdings, sellable quantity, and open orders.",
            "- Reconfirm latest quote/orderbook, spread, session status, and limit price before any preview.",
            "- Recheck active journal thesis, target/stop, min-hold, and pending duplicate orders.",
            "- Treat Toss/manual/reference balances as non-executable context only.",
            "- If proceeding later, run only preview/dry-run validation first and obtain explicit operator approval.",
            "",
            "### 7) Safety / no-submit statement",
            "- NO LIVE ORDER WAS SUBMITTED, CANCELLED, OR MODIFIED by this report.",
            "- This Discord message is a read-only preview/checklist, not execution authorization.",
        ]
    )
    return "\n".join(lines).strip()
