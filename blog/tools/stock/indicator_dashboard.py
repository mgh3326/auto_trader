"""IndicatorDashboard SVG component.

2×2 grid of InfoCard instances displaying technical indicators.
"""

from __future__ import annotations

from blog.tools.components.base import Colors
from blog.tools.components.card import InfoCard


class IndicatorDashboard:
    """Technical indicator dashboard — 2×2 grid of metric cards."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        indicators: dict,
    ) -> str:
        """Render an indicator dashboard as an SVG fragment.

        Args:
            x: X position of the dashboard.
            y: Y position of the dashboard.
            width: Dashboard width.
            height: Dashboard height.
            indicators: Dict with keys like rsi14, macd_histogram, macd_signal,
                       adx, plus_di, minus_di, stoch_rsi_k, stoch_rsi_d.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        # Calculate 2×2 grid
        card_w = (width - 20) // 2
        card_h = (height - 20) // 2
        gap = 10

        # RSI Card
        rsi = indicators.get("rsi14", 50.0)
        rsi_zone = "과매수" if rsi > 70 else "과매도" if rsi < 30 else "중립"
        rsi_color = Colors.BULLISH if rsi > 70 else Colors.BEARISH if rsi < 30 else Colors.NEUTRAL
        parts.append(
            InfoCard.create(
                x=x, y=y, width=card_w, height=card_h,
                title="RSI(14)", value=f"{rsi:.1f}",
                description=rsi_zone, color=rsi_color,
            )
        )

        # MACD Card
        macd_hist = indicators.get("macd_histogram", 0)
        macd_signal = indicators.get("macd_signal", "중립")
        macd_color = Colors.BULLISH if macd_hist > 0 else Colors.BEARISH
        parts.append(
            InfoCard.create(
                x=x + card_w + gap, y=y, width=card_w, height=card_h,
                title="MACD", value=f"{macd_hist:,.0f}",
                description=macd_signal, color=macd_color,
            )
        )

        # ADX Card
        adx = indicators.get("adx", 0)
        plus_di = indicators.get("plus_di", 0)
        minus_di = indicators.get("minus_di", 0)
        trend_strength = "강한 추세" if adx > 25 else "약한 추세"
        adx_color = Colors.TECHNICAL
        parts.append(
            InfoCard.create(
                x=x, y=y + card_h + gap, width=card_w, height=card_h,
                title="ADX", value=f"{adx:.1f}",
                description=trend_strength, color=adx_color,
                sub_items=[("+DI", f"{plus_di:.1f}"), ("-DI", f"{minus_di:.1f}")],
            )
        )

        # StochRSI Card
        stoch_k = indicators.get("stoch_rsi_k", 0)
        stoch_d = indicators.get("stoch_rsi_d", 0)
        stoch_zone = "과매수" if stoch_k > 0.8 else "과매도" if stoch_k < 0.2 else "중립"
        stoch_color = Colors.BULLISH if stoch_k > 0.8 else Colors.BEARISH if stoch_k < 0.2 else Colors.NEUTRAL
        parts.append(
            InfoCard.create(
                x=x + card_w + gap, y=y + card_h + gap, width=card_w, height=card_h,
                title="StochRSI", value=f"{stoch_k:.2f}",
                description=stoch_zone, color=stoch_color,
                sub_items=[("K", f"{stoch_k:.2f}"), ("D", f"{stoch_d:.2f}")],
            )
        )

        return "\n".join(parts)
