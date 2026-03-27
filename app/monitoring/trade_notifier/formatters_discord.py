"""Discord embed formatters for trade notifications.

Each function is pure (no I/O, no side effects) and returns a DiscordEmbed dict.
"""

from __future__ import annotations

from app.core.timezone import format_datetime

from .types import COLORS, DECISION_EMOJI, DECISION_TEXT, DiscordEmbed, DiscordField


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
                "name": "매수 가격대",
                "value": "\n".join(price_list),
                "inline": False,
            }
        )

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
                "name": "매도 가격대",
                "value": "\n".join(price_list),
                "inline": False,
            }
        )

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
) -> DiscordEmbed:
    timestamp = format_datetime()
    is_usd = currency == "$"

    def price_fmt(price: float) -> str:
        return f"${price:,.2f}" if is_usd else f"{price:,.0f}{currency}"

    fields: list[DiscordField] = [
        {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
        {"name": "시장", "value": market_type, "inline": True},
        {"name": "현재가", "value": price_fmt(current_price), "inline": False},
        {
            "name": "토스 보유",
            "value": f"{toss_quantity}주 (평단가 {price_fmt(toss_avg_price)})",
            "inline": False,
        },
    ]

    if kis_quantity and kis_quantity > 0 and kis_avg_price:
        fields.append(
            {
                "name": "한투 보유",
                "value": f"{kis_quantity}주 (평단가 {price_fmt(kis_avg_price)})",
                "inline": False,
            }
        )

    fields.extend(
        [
            {
                "name": "💡 추천 매수가",
                "value": price_fmt(recommended_price),
                "inline": False,
            },
            {
                "name": "추천 수량",
                "value": f"{recommended_quantity}주",
                "inline": False,
            },
        ]
    )

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
) -> DiscordEmbed:
    timestamp = format_datetime()
    is_usd = currency == "$"

    def price_fmt(price: float) -> str:
        return f"${price:,.2f}" if is_usd else f"{price:,.0f}{currency}"

    profit_sign = "+" if profit_percent >= 0 else ""

    fields: list[DiscordField] = [
        {"name": "종목", "value": f"{korean_name} ({symbol})", "inline": True},
        {"name": "시장", "value": market_type, "inline": True},
        {"name": "현재가", "value": price_fmt(current_price), "inline": False},
        {
            "name": "토스 보유",
            "value": f"{toss_quantity}주 (평단가 {price_fmt(toss_avg_price)})",
            "inline": False,
        },
    ]

    if kis_quantity and kis_quantity > 0 and kis_avg_price:
        fields.append(
            {
                "name": "한투 보유",
                "value": f"{kis_quantity}주 (평단가 {price_fmt(kis_avg_price)})",
                "inline": False,
            }
        )

    fields.extend(
        [
            {
                "name": "💡 추천 매도가",
                "value": f"{price_fmt(recommended_price)} ({profit_sign}{profit_percent:.1f}%)",
                "inline": False,
            },
            {
                "name": "추천 수량",
                "value": f"{recommended_quantity}주",
                "inline": False,
            },
            {
                "name": "예상 수익",
                "value": price_fmt(expected_profit),
                "inline": False,
            },
        ]
    )

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
) -> DiscordEmbed:
    timestamp = format_datetime()
    is_usd = currency == "$"

    def price_fmt(price: float) -> str:
        return f"${price:,.2f}" if is_usd else f"{price:,.0f}{currency}"

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

    return {
        "title": "📊 [토스] AI 분석",
        "description": f"🕒 {timestamp}",
        "color": color,
        "fields": fields,
    }
