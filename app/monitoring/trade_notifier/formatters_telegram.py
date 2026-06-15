"""Telegram message formatters for trade notifications.

Each function is pure (no I/O) and returns a formatted string.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.hermes_client import ReviewTriggerPayload

from app.core.timezone import format_datetime
from app.services.fill_notification import (
    FillEnrichment,
    FillOrder,
    format_fill_money,
    format_fill_quantity,
)

from .types import DECISION_EMOJI, DECISION_TEXT



def format_buy_notification_telegram(
    symbol: str,
    korean_name: str,
    order_count: int,
    total_amount: float,
    prices: list[float],
    volumes: list[float],
    market_type: str = "암호화폐",
) -> str:
    """Format buy order notification as Telegram markdown message."""
    timestamp = format_datetime()

    lines = [
        "*💰 매수 주문 접수*",
        "",
        f"🕒 {timestamp}",
        "",
        f"*종목:* {korean_name} \\({symbol}\\)",
        f"*시장:* {market_type}",
        f"*주문 수:* {order_count}건",
        f"*총 금액:* {total_amount:,.0f}원",
    ]

    # Add order details if available
    if prices and volumes and len(prices) == len(volumes):
        lines.append("")
        lines.append("*주문 상세:*")
        for i, (price, volume) in enumerate(zip(prices, volumes, strict=True), 1):
            lines.append(f"{i}. {price:,.2f}원 × {volume:.8g}")
    elif prices:
        lines.append("")
        lines.append("*매수 가격대:*")
        for i, price in enumerate(prices, 1):
            lines.append(f"{i}. {price:,.2f}원")

    return "\n".join(lines)


def format_sell_notification_telegram(
    symbol: str,
    korean_name: str,
    order_count: int,
    total_volume: float,
    prices: list[float],
    volumes: list[float],
    expected_amount: float,
    market_type: str = "암호화폐",
) -> str:
    """Format sell order notification as Telegram markdown message."""
    timestamp = format_datetime()

    lines = [
        "*💸 매도 주문 접수*",
        "",
        f"🕒 {timestamp}",
        "",
        f"*종목:* {korean_name} \\({symbol}\\)",
        f"*시장:* {market_type}",
        f"*주문 수:* {order_count}건",
        f"*총 수량:* {total_volume:.8g}",
        f"*예상 금액:* {expected_amount:,.0f}원",
    ]

    # Add order details if available
    if prices and volumes and len(prices) == len(volumes):
        lines.append("")
        lines.append("*주문 상세:*")
        for i, (price, volume) in enumerate(zip(prices, volumes, strict=True), 1):
            lines.append(f"{i}. {price:,.2f}원 × {volume:.8g}")
    elif prices:
        lines.append("")
        lines.append("*매도 가격대:*")
        for i, price in enumerate(prices, 1):
            lines.append(f"{i}. {price:,.2f}원")

    return "\n".join(lines)


def format_cancel_notification_telegram(
    symbol: str,
    korean_name: str,
    cancel_count: int,
    order_type: str = "전체",
    market_type: str = "암호화폐",
) -> str:
    """Format order cancellation notification as Telegram markdown message."""
    timestamp = format_datetime()

    return "\n".join(
        [
            "*🚫 주문 취소*",
            "",
            f"🕒 {timestamp}",
            "",
            f"*종목:* {korean_name} \\({symbol}\\)",
            f"*시장:* {market_type}",
            f"*취소 유형:* {order_type}",
            f"*취소 건수:* {cancel_count}건",
        ]
    )


def format_analysis_notification_telegram(
    symbol: str,
    korean_name: str,
    decision: str,
    confidence: float,
    reasons: list[str],
    market_type: str = "암호화폐",
) -> str:
    """Format AI analysis notification as Telegram markdown message."""
    timestamp = format_datetime()

    emoji = DECISION_EMOJI.get(decision.lower(), "⚪")
    decision_kr = DECISION_TEXT.get(decision.lower(), decision)

    lines = [
        "*📊 AI 분석 완료*",
        "",
        f"🕒 {timestamp}",
        "",
        f"*종목:* {korean_name} \\({symbol}\\)",
        f"*시장:* {market_type}",
        f"*판단:* {emoji} {decision_kr}",
        f"*신뢰도:* {confidence:.1f}%",
    ]

    # Add reasons if available
    if reasons:
        lines.append("")
        lines.append("*주요 근거:*")
        for i, reason in enumerate(reasons[:3], 1):
            lines.append(f"{i}. {reason}")

    return "\n".join(lines)


def format_automation_summary_telegram(
    total_coins: int,
    analyzed: int,
    bought: int,
    sold: int,
    errors: int,
    duration_seconds: float,
) -> str:
    """Format automation execution summary as Telegram markdown message."""
    timestamp = format_datetime()

    lines = [
        "*🤖 자동 거래 실행 완료*",
        "",
        f"🕒 {timestamp}",
        "",
        f"*처리 종목:* {total_coins}개",
        f"*분석 완료:* {analyzed}개",
        f"*매수 주문:* {bought}건",
        f"*매도 주문:* {sold}건",
        f"*실행 시간:* {duration_seconds:.1f}초",
    ]

    if errors > 0:
        lines.append(f"*오류 발생:* {errors}건")

    return "\n".join(lines)


def format_failure_notification_telegram(
    symbol: str,
    korean_name: str,
    reason: str,
    market_type: str = "암호화폐",
) -> str:
    """Format trade failure notification as Telegram markdown message."""
    timestamp = format_datetime()

    return "\n".join(
        [
            "*⚠️ 거래 실패*",
            "",
            f"🕒 {timestamp}",
            "",
            f"*종목:* {korean_name} \\({symbol}\\)",
            f"*시장:* {market_type}",
            f"*사유:* {reason}",
        ]
    )


def format_toss_price_recommendation_html(
    symbol: str,
    korean_name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None = None,
    kis_avg_price: float | None = None,
    decision: str = "hold",
    confidence: float = 0.0,
    reasons: list[str] | None = None,
    appropriate_buy_min: float | None = None,
    appropriate_buy_max: float | None = None,
    appropriate_sell_min: float | None = None,
    appropriate_sell_max: float | None = None,
    buy_hope_min: float | None = None,
    buy_hope_max: float | None = None,
    sell_target_min: float | None = None,
    sell_target_max: float | None = None,
    currency: str = "원",
    market_type: str = "국내주식",
    detail_url: str | None = None,
) -> str:
    """Format Toss price recommendation notification with AI analysis as HTML for Telegram."""
    if reasons is None:
        reasons = []

    timestamp = format_datetime()
    is_usd = currency == "$"

    def price_fmt(p: float) -> str:
        return f"${p:,.2f}" if is_usd else f"{p:,.0f}{currency}"

    # 수익률 계산
    profit_percent = (
        ((current_price / toss_avg_price) - 1) * 100 if toss_avg_price > 0 else 0
    )
    profit_sign = "+" if profit_percent >= 0 else ""

    emoji = DECISION_EMOJI.get(decision.lower(), "⚪")
    decision_kr = DECISION_TEXT.get(decision.lower(), decision)

    # Escape HTML special characters in text values
    safe_name = html.escape(korean_name)
    safe_symbol = html.escape(symbol)

    # Build HTML message
    lines = [
        "📊 <b>[토스] AI 분석</b>",
        f"🕒 {timestamp}",
        "",
        f"<b>종목:</b> {safe_name} ({safe_symbol})",
        f"<b>시장:</b> {market_type}",
        f"<b>현재가:</b> {price_fmt(current_price)}",
        f"<b>보유:</b> {toss_quantity}주 (평단가 {price_fmt(toss_avg_price)}, {profit_sign}{profit_percent:.1f}%)",
    ]

    # KIS 보유 정보 추가
    if kis_quantity is not None and kis_avg_price is not None:
        kis_profit = (
            ((current_price / kis_avg_price) - 1) * 100 if kis_avg_price > 0 else 0
        )
        kis_sign = "+" if kis_profit >= 0 else ""
        lines.append(
            f"<b>KIS 보유:</b> {kis_quantity}주 (평단가 {price_fmt(kis_avg_price)}, {kis_sign}{kis_profit:.1f}%)"
        )

    lines.append(f"<b>AI 판단:</b> {emoji} {decision_kr} (신뢰도 {confidence:.0f}%)")

    # 근거 추가
    if reasons:
        lines.append("")
        lines.append("<b>근거:</b>")
        for i, reason in enumerate(reasons[:3], 1):
            safe_reason = html.escape(reason)
            lines.append(f"{i}. {safe_reason}")

    # 가격 제안 추가
    price_suggestions: list[str] = []

    if appropriate_buy_min or appropriate_buy_max:
        buy_range: list[str] = []
        if appropriate_buy_min:
            buy_range.append(price_fmt(appropriate_buy_min))
        if appropriate_buy_max:
            buy_range.append(price_fmt(appropriate_buy_max))
        price_suggestions.append(f"적정 매수: {' ~ '.join(buy_range)}")

    if appropriate_sell_min or appropriate_sell_max:
        sell_range: list[str] = []
        if appropriate_sell_min:
            sell_range.append(price_fmt(appropriate_sell_min))
        if appropriate_sell_max:
            sell_range.append(price_fmt(appropriate_sell_max))
        price_suggestions.append(f"적정 매도: {' ~ '.join(sell_range)}")

    if buy_hope_min or buy_hope_max:
        hope_range: list[str] = []
        if buy_hope_min:
            hope_range.append(price_fmt(buy_hope_min))
        if buy_hope_max:
            hope_range.append(price_fmt(buy_hope_max))
        price_suggestions.append(f"매수 희망: {' ~ '.join(hope_range)}")

    if sell_target_min or sell_target_max:
        target_range: list[str] = []
        if sell_target_min:
            target_range.append(price_fmt(sell_target_min))
        if sell_target_max:
            target_range.append(price_fmt(sell_target_max))
        price_suggestions.append(f"매도 목표: {' ~ '.join(target_range)}")

    if price_suggestions:
        lines.append("")
        lines.append("<b>가격 제안:</b>")
        for suggestion in price_suggestions:
            lines.append(html.escape(suggestion))

    if detail_url:
        lines.append("")
        lines.append(f"<b>상세:</b> {detail_url}")

    return "\n".join(lines)


def format_fill_notification_telegram(
    order: FillOrder,
    *,
    display_name: str,
    detail_url: str | None = None,
    enrichment: FillEnrichment | None = None,
) -> str:
    """Telegram(legacy Markdown) 체결 메시지."""
    is_sell = order.side == "ask"
    is_partial = order.fill_status == "partial"
    side_emoji = "🔴" if is_sell else ("🟢" if order.side == "bid" else "⚪")
    side_text = "매도" if is_sell else ("매수" if order.side == "bid" else "미확인")
    fill_label = "부분체결" if is_partial else "체결"
    is_usd = (order.currency or "").upper() == "USD"

    price_str = format_fill_money(order.filled_price, is_usd=is_usd)
    if order.order_price:
        diff_pct = (order.filled_price - order.order_price) / order.order_price * 100
        price_str += f" ({diff_pct:+.2f}%)"

    lines = [
        f"*{side_emoji} {fill_label} · {display_name} \\({order.symbol}\\)*",
        "",
        f"*구분:* {side_text} {fill_label}",
        f"*체결가:* {price_str}",
        f"*수량:* {format_fill_quantity(order.filled_qty)}",
        f"*금액:* {format_fill_money(order.filled_amount, is_usd=is_usd)}",
    ]

    if enrichment is not None:
        approx = " ~추정" if enrichment.is_approximate else ""
        if is_sell and enrichment.realized_pnl_amount is not None:
            sign = "+" if enrichment.realized_pnl_amount >= 0 else ""
            rate = (
                f" ({enrichment.realized_pnl_rate:+.2f}%)"
                if enrichment.realized_pnl_rate is not None
                else ""
            )
            lines.append(
                f"*실현손익:* {sign}{format_fill_money(enrichment.realized_pnl_amount, is_usd=is_usd)}{rate}{approx}"
            )
        elif (
            not is_sell
            and enrichment.position_qty is not None
            and enrichment.position_avg_price is not None
        ):
            lines.append(
                f"*보유:* {format_fill_quantity(enrichment.position_qty)} · 평단 "
                f"{format_fill_money(enrichment.position_avg_price, is_usd=is_usd)}{approx}"
            )

    account_val = order.account
    if order.order_id:
        account_val += f" · 주문 {order.order_id[:8]}…"
    lines.append(f"*계좌:* {account_val}")
    lines.append(f"🕒 {format_datetime()}")
    if detail_url:
        lines.append(f"[종목 상세 보기]({detail_url})")

    return "\n".join(lines)


def format_investment_watch_trigger_telegram(
    payload: ReviewTriggerPayload, *, display_name: str, base_url: str
) -> str:
    """Format watch trigger notification as Telegram markdown message."""
    outcome_kr = {
        "notified": "알림",
        "review_required": "검토 필요",
        "preview_attached": "프리뷰 첨부",
        "executed": "모의 실행",
    }.get(payload.outcome, payload.outcome)

    lines = [
        f"*🔔 워치 트리거 · {display_name} \\({payload.symbol}\\)*",
        "",
        f"*조건:* {payload.metric} {payload.operator} {payload.threshold}",
        f"*현재값:* {payload.current_value if payload.current_value is not None else '-'}",
        f"*시장:* {payload.market}",
        f"*구분:* {outcome_kr}",
    ]

    pg = payload.price_guidance
    if pg is not None:
        parts = []
        if pg.entry_review_below_price is not None:
            parts.append(f"진입검토 ≤ {pg.entry_review_below_price}")
        if pg.suggested_limit_price_range is not None:
            parts.append(
                f"지정가 {pg.suggested_limit_price_range.low}~{pg.suggested_limit_price_range.high}"
            )
        if pg.max_chase_price is not None:
            parts.append(f"최대추격 {pg.max_chase_price}")
        if (
            pg.invalidation is not None
            and getattr(pg.invalidation, "price", None) is not None
        ):
            parts.append(f"무효화 {pg.invalidation.price}")
        if parts:
            lines.append("")
            lines.append("*가격 가이드:*")
            for part in parts:
                lines.append(f"• {part}")

    if payload.trigger_checklist:
        lines.append("")
        lines.append("*체크리스트:*")
        for c in payload.trigger_checklist:
            lines.append(f"• {c}")

    desc = ""
    if payload.operator_action_guidance is not None:
        desc = payload.operator_action_guidance.headline
    if desc:
        lines.append("")
        lines.append(desc)

    lines.append("")
    lines.append(f"🕒 {format_datetime()}")

    if payload.invest_links is not None:
        stock_url = f"{base_url}{payload.invest_links.stock_path}"
        report_url = f"{base_url}{payload.invest_links.report_path}"
        lines.append("")
        lines.append(f"[종목 상세]({stock_url}) · [분석 리포트]({report_url})")

    return "\n".join(lines)

