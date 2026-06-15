"""Discord embed formatters for trade notifications.

Each function is pure (no I/O, no side effects) and returns a DiscordEmbed dict.
"""

from __future__ import annotations

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

from .types import COLORS, DECISION_EMOJI, DECISION_TEXT, DiscordEmbed, DiscordField


def _price_fmt(price: float, is_usd: bool, currency: str) -> str:
    return f"${price:,.2f}" if is_usd else f"{price:,.0f}{currency}"


def _append_order_details(
    fields: list[DiscordField],
    prices: list[float],
    volumes: list[float],
    price_label: str,
) -> None:
    if prices and volumes and len(prices) == len(volumes):
        order_details: list[str] = []
        for i, (price, volume) in enumerate(zip(prices, volumes, strict=True), 1):
            order_details.append(f"{i}. {price:,.2f}원 × {volume:.8g}")
        fields.append(
            {
                "name": "주문 상세",
                "value": "\n".join(order_details),
                "inline": False,
            }
        )
    elif prices:
        price_list: list[str] = []
        for i, price in enumerate(prices, 1):
            price_list.append(f"{i}. {price:,.2f}원")
        fields.append(
            {
                "name": price_label,
                "value": "\n".join(price_list),
                "inline": False,
            }
        )


def format_buy_notification(
    symbol: str,
    korean_name: str,
    order_count: int,
    total_amount: float,
    prices: list[float],
    volumes: list[float],
    market_type: str = "암호화폐",
) -> DiscordEmbed:
    timestamp = format_datetime()

    fields: list[DiscordField] = [
        {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
        {"name": "시장", "value": market_type, "inline": True},
        {"name": "주문 수", "value": f"{order_count}건", "inline": True},
        {"name": "총 금액", "value": f"{total_amount:,.0f}원", "inline": False},
    ]

    _append_order_details(fields, prices, volumes, "매수 가격대")

    return {
        "title": "💰 매수 주문 접수",
        "description": f"🕒 {timestamp}",
        "color": COLORS["buy"],
        "fields": fields,
    }


def format_sell_notification(
    symbol: str,
    korean_name: str,
    order_count: int,
    total_volume: float,
    prices: list[float],
    volumes: list[float],
    expected_amount: float,
    market_type: str = "암호화폐",
) -> DiscordEmbed:
    timestamp = format_datetime()

    fields: list[DiscordField] = [
        {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
        {"name": "시장", "value": market_type, "inline": True},
        {"name": "주문 수", "value": f"{order_count}건", "inline": True},
        {"name": "총 수량", "value": f"{total_volume:.8g}", "inline": False},
        {
            "name": "예상 금액",
            "value": f"{expected_amount:,.0f}원",
            "inline": False,
        },
    ]

    _append_order_details(fields, prices, volumes, "매도 가격대")

    return {
        "title": "💸 매도 주문 접수",
        "description": f"🕒 {timestamp}",
        "color": COLORS["sell"],
        "fields": fields,
    }


def format_cancel_notification(
    symbol: str,
    korean_name: str,
    cancel_count: int,
    order_type: str = "전체",
    market_type: str = "암호화폐",
) -> DiscordEmbed:
    timestamp = format_datetime()

    fields: list[DiscordField] = [
        {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
        {"name": "시장", "value": market_type, "inline": True},
        {"name": "취소 유형", "value": order_type, "inline": True},
        {"name": "취소 건수", "value": f"{cancel_count}건", "inline": False},
    ]

    return {
        "title": "🚫 주문 취소",
        "description": f"🕒 {timestamp}",
        "color": COLORS["cancel"],
        "fields": fields,
    }


def _base_toss_fields(
    symbol: str,
    korean_name: str,
    market_type: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None,
    kis_avg_price: float | None,
    is_usd: bool,
    currency: str,
) -> list[DiscordField]:
    fields: list[DiscordField] = [
        {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
        {"name": "시장", "value": market_type, "inline": True},
        {
            "name": "현재가",
            "value": _price_fmt(current_price, is_usd, currency),
            "inline": False,
        },
        {
            "name": "토스 보유",
            "value": f"{toss_quantity}주 (평단가 {_price_fmt(toss_avg_price, is_usd, currency)})",
            "inline": False,
        },
    ]

    if kis_quantity and kis_quantity > 0 and kis_avg_price:
        fields.append(
            {
                "name": "한투 보유",
                "value": f"{kis_quantity}주 (평단가 {_price_fmt(kis_avg_price, is_usd, currency)})",
                "inline": False,
            }
        )
    return fields


def format_analysis_notification(
    symbol: str,
    korean_name: str,
    decision: str,
    confidence: float,
    reasons: list[str],
    market_type: str = "암호화폐",
) -> DiscordEmbed:
    timestamp = format_datetime()

    emoji = DECISION_EMOJI.get(decision.lower(), "⚪")
    decision_kr = DECISION_TEXT.get(decision.lower(), decision)

    fields: list[DiscordField] = [
        {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
        {"name": "시장", "value": market_type, "inline": True},
        {"name": "판단", "value": f"{emoji} {decision_kr}", "inline": True},
        {"name": "신뢰도", "value": f"{confidence:.1f}%", "inline": False},
    ]

    if reasons:
        reason_text = "\n".join(
            f"{i}. {reason}" for i, reason in enumerate(reasons[:3], 1)
        )
        fields.append(
            {
                "name": "주요 근거",
                "value": reason_text,
                "inline": False,
            }
        )

    return {
        "title": "📊 AI 분석 완료",
        "description": f"🕒 {timestamp}",
        "color": COLORS["analysis"],
        "fields": fields,
    }


def format_automation_summary(
    total_coins: int,
    analyzed: int,
    bought: int,
    sold: int,
    errors: int,
    duration_seconds: float,
) -> DiscordEmbed:
    timestamp = format_datetime()

    fields: list[DiscordField] = [
        {"name": "처리 종목", "value": f"{total_coins}개", "inline": True},
        {"name": "분석 완료", "value": f"{analyzed}개", "inline": True},
        {"name": "매수 주문", "value": f"{bought}건", "inline": True},
        {"name": "매도 주문", "value": f"{sold}건", "inline": True},
        {"name": "실행 시간", "value": f"{duration_seconds:.1f}초", "inline": True},
    ]

    if errors > 0:
        fields.append(
            {
                "name": "오류 발생",
                "value": f"{errors}건",
                "inline": False,
            }
        )

    return {
        "title": "🤖 자동 거래 실행 완료",
        "description": f"🕒 {timestamp}",
        "color": COLORS["summary"],
        "fields": fields,
    }


def format_failure_notification(
    symbol: str,
    korean_name: str,
    reason: str,
    market_type: str = "암호화폐",
) -> DiscordEmbed:
    timestamp = format_datetime()

    fields: list[DiscordField] = [
        {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
        {"name": "시장", "value": market_type, "inline": True},
        {"name": "사유", "value": reason, "inline": False},
    ]

    return {
        "title": "⚠️ 거래 실패",
        "description": f"🕒 {timestamp}",
        "color": COLORS["failure"],
        "fields": fields,
    }


def format_toss_buy_recommendation(
    symbol: str,
    korean_name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None,
    kis_avg_price: float | None,
    recommended_price: float,
    recommended_quantity: int,
    currency: str = "원",
    market_type: str = "국내주식",
    detail_url: str | None = None,
) -> DiscordEmbed:
    timestamp = format_datetime()
    is_usd = currency == "$"

    fields = _base_toss_fields(
        symbol,
        korean_name,
        market_type,
        current_price,
        toss_quantity,
        toss_avg_price,
        kis_quantity,
        kis_avg_price,
        is_usd,
        currency,
    )

    fields.extend(
        [
            {
                "name": "💡 추천 매수가",
                "value": _price_fmt(recommended_price, is_usd, currency),
                "inline": False,
            },
            {
                "name": "추천 수량",
                "value": f"{recommended_quantity}주",
                "inline": False,
            },
        ]
    )

    if detail_url:
        fields.append({"name": "상세", "value": detail_url, "inline": False})

    return {
        "title": "📈 [토스 수동매수]",
        "description": f"🕒 {timestamp}",
        "color": COLORS["buy"],
        "fields": fields,
    }


def format_toss_sell_recommendation(
    symbol: str,
    korean_name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None,
    kis_avg_price: float | None,
    recommended_price: float,
    recommended_quantity: int,
    expected_profit: float,
    profit_percent: float,
    currency: str = "원",
    market_type: str = "국내주식",
    detail_url: str | None = None,
) -> DiscordEmbed:
    timestamp = format_datetime()
    is_usd = currency == "$"

    profit_sign = "+" if profit_percent >= 0 else ""

    fields = _base_toss_fields(
        symbol,
        korean_name,
        market_type,
        current_price,
        toss_quantity,
        toss_avg_price,
        kis_quantity,
        kis_avg_price,
        is_usd,
        currency,
    )

    fields.extend(
        [
            {
                "name": "💡 추천 매도가",
                "value": f"{_price_fmt(recommended_price, is_usd, currency)} ({profit_sign}{profit_percent:.1f}%)",
                "inline": False,
            },
            {
                "name": "추천 수량",
                "value": f"{recommended_quantity}주",
                "inline": False,
            },
            {
                "name": "예상 수익",
                "value": _price_fmt(expected_profit, is_usd, currency),
                "inline": False,
            },
        ]
    )

    if detail_url:
        fields.append({"name": "상세", "value": detail_url, "inline": False})

    return {
        "title": "📉 [토스 수동매도]",
        "description": f"🕒 {timestamp}",
        "color": COLORS["sell"],
        "fields": fields,
    }


def format_toss_price_recommendation(
    symbol: str,
    korean_name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    decision: str,
    confidence: float,
    reasons: list[str],
    appropriate_buy_min: float | None,
    appropriate_buy_max: float | None,
    appropriate_sell_min: float | None,
    appropriate_sell_max: float | None,
    buy_hope_min: float | None = None,
    buy_hope_max: float | None = None,
    sell_target_min: float | None = None,
    sell_target_max: float | None = None,
    currency: str = "원",
    market_type: str = "국내주식",
    detail_url: str | None = None,
) -> DiscordEmbed:
    timestamp = format_datetime()
    is_usd = currency == "$"

    def price_fmt(price: float) -> str:
        return _price_fmt(price, is_usd, currency)

    profit_percent = (
        ((current_price / toss_avg_price) - 1) * 100 if toss_avg_price > 0 else 0
    )
    profit_sign = "+" if profit_percent >= 0 else ""

    emoji = DECISION_EMOJI.get(decision.lower(), "⚪")
    decision_kr = DECISION_TEXT.get(decision.lower(), decision)

    decision_color = {
        "buy": COLORS["buy"],
        "hold": COLORS["hold"],
        "sell": COLORS["sell"],
    }
    color = decision_color.get(decision.lower(), COLORS["default"])

    fields: list[DiscordField] = [
        {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
        {"name": "시장", "value": market_type, "inline": True},
        {"name": "현재가", "value": price_fmt(current_price), "inline": True},
        {
            "name": "보유",
            "value": f"{toss_quantity}주 (평단가 {price_fmt(toss_avg_price)}, {profit_sign}{profit_percent:.1f}%)",
            "inline": False,
        },
        {
            "name": "AI 판단",
            "value": f"{emoji} {decision_kr} (신뢰도 {confidence:.0f}%)",
            "inline": False,
        },
    ]

    if reasons:
        reason_text = "\n".join(
            f"{i}. {reason[:80]}..." if len(reason) > 80 else f"{i}. {reason}"
            for i, reason in enumerate(reasons[:3], 1)
        )
        fields.append({"name": "근거", "value": reason_text, "inline": False})

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
        fields.append(
            {
                "name": "가격 제안",
                "value": "\n".join(price_suggestions),
                "inline": False,
            }
        )

    if detail_url:
        fields.append({"name": "상세", "value": detail_url, "inline": False})

    return {
        "title": "📊 [토스] AI 분석",
        "description": f"🕒 {timestamp}",
        "color": color,
        "fields": fields,
    }


def format_fill_notification(
    order: FillOrder,
    *,
    display_name: str,
    detail_url: str | None = None,
    enrichment: FillEnrichment | None = None,
) -> DiscordEmbed:
    is_sell = order.side == "ask"
    is_partial = order.fill_status == "partial"
    side_emoji = "🔴" if is_sell else ("🟢" if order.side == "bid" else "⚪")
    side_text = "매도" if is_sell else ("매수" if order.side == "bid" else "미확인")
    fill_label = "부분체결" if is_partial else "체결"
    is_usd = (order.currency or "").upper() == "USD"

    price_str = format_fill_money(order.filled_price, is_usd=is_usd)
    if order.order_price:
        diff_pct = (order.filled_price - order.order_price) / order.order_price * 100
        price_str += f" ({diff_pct:+.2f}% vs 주문가)"

    fields: list[DiscordField] = [
        {"name": "구분", "value": f"{side_text} {fill_label}", "inline": True},
        {"name": "체결가", "value": price_str, "inline": True},
        {
            "name": "수량",
            "value": format_fill_quantity(order.filled_qty),
            "inline": True,
        },
        {
            "name": "금액",
            "value": format_fill_money(order.filled_amount, is_usd=is_usd),
            "inline": True,
        },
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
            fields.append(
                {
                    "name": "실현손익",
                    "value": f"{sign}{format_fill_money(enrichment.realized_pnl_amount, is_usd=is_usd)}{rate}{approx}",
                    "inline": True,
                }
            )
        elif (
            not is_sell
            and enrichment.position_qty is not None
            and enrichment.position_avg_price is not None
        ):
            fields.append(
                {
                    "name": "보유",
                    "value": f"{format_fill_quantity(enrichment.position_qty)} · 평단 "
                    f"{format_fill_money(enrichment.position_avg_price, is_usd=is_usd)}{approx}",
                    "inline": True,
                }
            )

    account_val = order.account
    if order.order_id:
        account_val += f" · 주문 {order.order_id[:8]}…"
    fields.append({"name": "계좌", "value": account_val, "inline": False})

    embed: DiscordEmbed = {
        "title": f"{side_emoji} {fill_label} · {display_name} ({order.symbol})",
        "description": f"🕒 {format_datetime()}",
        "color": COLORS["sell"] if is_sell else COLORS["buy"],
        "fields": fields,
    }
    if detail_url:
        embed["url"] = detail_url
    return embed


def format_investment_watch_trigger(
    payload: ReviewTriggerPayload, *, display_name: str, base_url: str
) -> DiscordEmbed:
    """ROB-566: watch 트리거 Discord 임베드 (Prefect 렌더 대체)."""
    outcome_kr = {
        "notified": "알림",
        "review_required": "검토 필요",
        "preview_attached": "프리뷰 첨부",
        "executed": "모의 실행",
    }.get(payload.outcome, payload.outcome)

    fields: list[DiscordField] = [
        {
            "name": "조건",
            "value": f"{payload.metric} {payload.operator} {payload.threshold}",
            "inline": True,
        },
        {
            "name": "현재값",
            "value": (
                str(payload.current_value) if payload.current_value is not None else "-"
            ),
            "inline": True,
        },
        {"name": "시장", "value": payload.market, "inline": True},
        {"name": "구분", "value": outcome_kr, "inline": True},
    ]
    pg = payload.price_guidance
    if pg is not None:
        parts: list[str] = []
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
            fields.append(
                {"name": "가격 가이드", "value": "\n".join(parts), "inline": False}
            )
    if payload.trigger_checklist:
        fields.append(
            {
                "name": "체크리스트",
                "value": "\n".join(f"• {c}" for c in payload.trigger_checklist),
                "inline": False,
            }
        )
    if payload.invest_links is not None:
        fields.append(
            {
                "name": "링크",
                "value": f"[리포트]({base_url}{payload.invest_links.report_path}) · [종목]({base_url}{payload.invest_links.stock_path})",
                "inline": False,
            }
        )

    desc = ""
    if payload.operator_action_guidance is not None:
        desc = payload.operator_action_guidance.headline
    desc = (desc + f"\n🕒 {format_datetime()}").strip()

    embed: DiscordEmbed = {
        "title": f"🔔 워치 트리거 · {display_name} ({payload.symbol})",
        "description": desc,
        "color": COLORS["watch"],
        "fields": fields,
    }
    if payload.invest_links is not None:
        embed["url"] = f"{base_url}{payload.invest_links.stock_path}"
    return embed
