"""PriceChart SVG component.

OHLCV line chart with optional Bollinger bands and EMA overlays.
"""

from __future__ import annotations

from typing import Any


class PriceChart:
    """Price line chart with technical overlays."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        ohlcv: list[dict[str, Any]],
        bollinger: dict[str, list[float]] | None = None,
        ema_values: dict[str, list[float]] | None = None,
    ) -> str:
        """Render a price chart as an SVG fragment.

        Args:
            x: X position of the chart.
            y: Y position of the chart.
            width: Chart width.
            height: Chart height.
            ohlcv: List of OHLCV dicts with 'date' and 'close' keys.
            bollinger: Optional dict with 'upper' and 'lower' band values.
            ema_values: Optional dict of EMA name -> value list.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        if not ohlcv:
            return f"    <!-- Empty price chart at ({x}, {y}) -->\n"

        parts: list[str] = []

        # Extract close prices
        closes = [c["close"] for c in ohlcv]
        min_price = min(closes)
        max_price = max(closes)
        price_range = max_price - min_price or 1

        # Add padding
        min_price -= price_range * 0.05
        max_price += price_range * 0.05
        price_range = max_price - min_price

        # Chart area
        chart_h = height - 40  # Leave room for labels

        def price_to_y(price: float) -> float:
            return y + chart_h - (price - min_price) / price_range * chart_h

        # Draw Bollinger band area first (if provided)
        if bollinger and "upper" in bollinger and "lower" in bollinger:
            upper = bollinger["upper"]
            lower = bollinger["lower"]
            if len(upper) == len(ohlcv) and len(lower) == len(ohlcv):
                points = []
                # Upper band (left to right)
                for i, val in enumerate(upper):
                    px = x + (i / (len(ohlcv) - 1)) * width
                    py = price_to_y(val)
                    points.append(f"{px},{py}")
                # Lower band (right to left)
                for i in range(len(lower) - 1, -1, -1):
                    px = x + (i / (len(ohlcv) - 1)) * width
                    py = price_to_y(lower[i])
                    points.append(f"{px},{py}")

                parts.append(
                    f'    <polygon points="{" ".join(points)}" '
                    f'fill="#e3f2fd" opacity="0.5"/>'
                )

        # Draw price line
        price_points = []
        for i, candle in enumerate(ohlcv):
            px = x + (i / (len(ohlcv) - 1)) * width if len(ohlcv) > 1 else x
            py = price_to_y(candle["close"])
            price_points.append(f"{px},{py}")

        parts.append(
            f'    <polyline points="{" ".join(price_points)}" '
            f'fill="none" stroke="#2196F3" stroke-width="2"/>'
        )

        # Draw EMA lines
        if ema_values:
            colors = ["#FF9800", "#4CAF50", "#9C27B0"]
            for idx, (_name, values) in enumerate(ema_values.items()):
                if len(values) == len(ohlcv):
                    ema_points = []
                    for i, val in enumerate(values):
                        px = x + (i / (len(ohlcv) - 1)) * width if len(ohlcv) > 1 else x
                        py = price_to_y(val)
                        ema_points.append(f"{px},{py}")

                    color = colors[idx % len(colors)]
                    parts.append(
                        f'    <polyline points="{" ".join(ema_points)}" '
                        f'fill="none" stroke="{color}" stroke-width="1.5" '
                        f'stroke-dasharray="5,3"/>'
                    )

        # Y-axis labels
        for i in range(5):
            price = min_price + price_range * i / 4
            label_y = price_to_y(price)
            parts.append(
                f'    <text x="{x - 5}" y="{label_y + 4}" '
                f'font-family="Arial, sans-serif" font-size="10" '
                f'fill="#666666" text-anchor="end">{int(price):,}</text>'
            )

        return "\n".join(parts) + "\n"
