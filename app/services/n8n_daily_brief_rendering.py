"""Markdown/text rendering helpers for n8n daily briefs."""

from __future__ import annotations

from typing import Any

from app.schemas.n8n.common import N8nMarketOverview


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
