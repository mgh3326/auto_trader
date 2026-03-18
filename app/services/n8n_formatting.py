from __future__ import annotations

from datetime import datetime
from typing import Any

_WEEKDAY_KR = ("월", "화", "수", "목", "금", "토", "일")


def _fmt_decimal(value: float, max_decimals: int) -> str:
    """Format a number with up to max_decimals, keeping at least 1 decimal for whole numbers."""
    formatted = f"{value:.{max_decimals}f}"
    if "." in formatted:
        formatted = formatted.rstrip("0")
        if formatted.endswith("."):
            formatted += "0"
    return formatted


def fmt_price(price: float | None, currency: str = "KRW") -> str:
    """Format a price for human display.

    KRW: 1억+ → "1.08억", 1만+ → "1.65만", else → "2,470"
    USD: 1000+ → "$1,234", else → "$12.50"
    """
    if price is None:
        return "-"
    if currency == "USD":
        if price == 0:
            return "$0.00"
        if price >= 1000:
            return f"${price:,.0f}"
        return f"${price:,.2f}"
    # KRW
    if price == 0:
        return "0"
    if price >= 1_000_000_000:
        return f"{_fmt_decimal(price / 100_000_000, 1)}억"
    if price >= 100_000_000:
        return f"{_fmt_decimal(price / 100_000_000, 2)}억"
    if price >= 10_000:
        return f"{_fmt_decimal(price / 10_000, 2)}만"
    int_price = int(price)
    if int_price == price:
        return f"{int_price:,}" if int_price >= 1000 else str(int_price)
    return f"{price:,.0f}" if price >= 1000 else f"{price:g}"


def fmt_gap(gap_pct: float | None) -> str:
    """Format gap percentage with sign. +14.0%, -3.2%, 0.0%."""
    if gap_pct is None:
        return "-"
    if gap_pct > 0:
        return f"+{gap_pct:.1f}%"
    return f"{gap_pct:.1f}%"


def fmt_amount(amount_krw: float | None) -> str:
    """Format KRW amount. >= 10,000 uses 만 units, else comma format."""
    if amount_krw is None:
        return "-"
    if amount_krw == 0:
        return "0"
    if amount_krw >= 10_000:
        man = amount_krw / 10_000
        return f"{man:,.1f}만"
    return f"{amount_krw:,.0f}"


def fmt_age(age_hours: int) -> str:
    """Format order age. >= 24h shows days, else hours."""
    if age_hours >= 24:
        return f"{age_hours // 24}일"
    return f"{age_hours}시간"


def build_summary_line(order: dict[str, Any]) -> str:
    """Build a one-line order summary.

    Format with name: "현대로템(064350) buy @18.8만 (현재 17.5만, -6.9%, 18.8만, 2일)"
    Format without name: "BTC buy @1.49억 (현재 1.49억, +0.5%, 29.7만, 6시간)"
    """
    currency = str(order.get("currency") or "KRW")
    symbol = str(order.get("symbol") or "")
    name = order.get("name")
    side = str(order.get("side") or "")
    price_str = fmt_price(order.get("order_price"), currency)
    current_str = fmt_price(order.get("current_price"), currency)
    gap_str = fmt_gap(order.get("gap_pct"))
    amount_str = fmt_amount(order.get("amount_krw"))
    age_str = fmt_age(int(order.get("age_hours") or 0))

    display_symbol = f"{name}({symbol})" if name else symbol

    return f"{display_symbol} {side} @{price_str} (현재 {current_str}, {gap_str}, {amount_str}, {age_str})"


def build_summary_title(
    *,
    total: int,
    buy_count: int,
    sell_count: int,
    as_of: datetime,
) -> str:
    """Build the summary title line.

    Format: "📋 미체결 리뷰 — 03/16 (13건, 매수 4 / 매도 9)"
    """
    date_str = as_of.strftime("%m/%d")
    return (
        f"📋 미체결 리뷰 — {date_str} ({total}건, 매수 {buy_count} / 매도 {sell_count})"
    )


def enrich_order_fmt(order: dict[str, Any]) -> None:
    """Add _fmt fields to an order dict in-place."""
    currency = str(order.get("currency") or "KRW")
    order["order_price_fmt"] = fmt_price(order.get("order_price"), currency)
    order["current_price_fmt"] = fmt_price(order.get("current_price"), currency)
    order["gap_pct_fmt"] = fmt_gap(order.get("gap_pct"))
    order["amount_fmt"] = fmt_amount(order.get("amount_krw"))
    order["age_fmt"] = fmt_age(int(order.get("age_hours") or 0))
    order["summary_line"] = build_summary_line(order)


def enrich_summary_fmt(
    summary: dict[str, Any],
    *,
    as_of: datetime,
) -> None:
    """Add formatted fields to a summary dict in-place."""
    summary["total_buy_fmt"] = fmt_amount(summary.get("total_buy_krw"))
    summary["total_sell_fmt"] = fmt_amount(summary.get("total_sell_krw"))
    summary["title"] = build_summary_title(
        total=int(summary.get("total") or 0),
        buy_count=int(summary.get("buy_count") or 0),
        sell_count=int(summary.get("sell_count") or 0),
        as_of=as_of,
    )


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
