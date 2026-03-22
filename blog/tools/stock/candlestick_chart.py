"""CandlestickChart SVG component."""

from __future__ import annotations

from typing import Any

from blog.tools.components.base import FONT_FAMILY

# Korean candle colors (local constants for isolated market modes)
KOREAN_BULLISH = "#ef5350"  # Red for up
KOREAN_BEARISH = "#1565c0"  # Blue for down

# Theme colors
LIGHT_GRID = "#e0e0e0"
LIGHT_LABEL = "#666666"
DARK_GRID = "#333333"
DARK_LABEL = "#aaaaaa"

# EMA colors
EMA_COLORS = ["#FF9800", "#4CAF50", "#9C27B0"]


class CandlestickChart:
    """OHLCV candlestick chart with technical overlays."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        ohlcv: list[dict[str, Any]],
        volume: bool = False,
        max_candles: int = 200,
        ema_values: dict[str, list[float]] | None = None,
        bollinger: dict[str, list[float]] | None = None,
        theme: str = "light",
    ) -> str:
        """Render a candlestick chart as an SVG fragment.

        Args:
            x: X position of the chart.
            y: Y position of the chart.
            width: Chart width.
            height: Chart height.
            ohlcv: List of OHLCV dicts with 'date', 'open', 'high', 'low', 'close' keys.
            volume: Whether to show volume bars at the bottom.
            max_candles: Maximum number of candles to render (truncates from end).
            ema_values: Optional dict of EMA name -> value list.
            bollinger: Optional dict with 'upper', 'lower', 'middle' band values.
            theme: 'light' or 'dark' theme.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        if not ohlcv:
            return f'    <text x="{x + width // 2}" y="{y + height // 2}" font-family="{FONT_FAMILY}" font-size="14" fill="{LIGHT_LABEL if theme == "light" else DARK_LABEL}" text-anchor="middle">Empty OHLCV</text>\n'

        # Filter and truncate data
        required_keys = {"open", "high", "low", "close"}
        rows = [row for row in ohlcv[-max_candles:] if required_keys <= row.keys()]

        if not rows:
            return f'    <text x="{x + width // 2}" y="{y + height // 2}" font-family="{FONT_FAMILY}" font-size="14" fill="{LIGHT_LABEL if theme == "light" else DARK_LABEL}" text-anchor="middle">Empty OHLCV</text>\n'

        parts: list[str] = []

        # Calculate price and volume ranges
        price_min = min(row["low"] for row in rows)
        price_max = max(row["high"] for row in rows)
        price_range = price_max - price_min or 1

        # Add padding to price range
        price_min -= price_range * 0.05
        price_max += price_range * 0.05
        price_range = price_max - price_min

        # Volume range
        volume_max = 0
        if volume and rows:
            volume_values = [row.get("volume", 0) for row in rows]
            volume_max = max(volume_values) if any(volume_values) else 0

        # Split chart regions
        if volume and volume_max > 0:
            price_height = height * 0.78
            volume_height = height * 0.20
            separator_height = height * 0.02
        else:
            price_height = height
            volume_height = 0
            separator_height = 0

        # Chart area for price (leave room for labels)
        chart_h = int(price_height - 30)

        # Color selection based on theme
        grid_color = LIGHT_GRID if theme == "light" else DARK_GRID
        label_color = LIGHT_LABEL if theme == "light" else DARK_LABEL

        def price_to_y(price: float) -> float:
            return y + chart_h - (price - price_min) / price_range * chart_h

        def volume_to_h(vol: float) -> float:
            if volume_max == 0:
                return 0
            return (vol / volume_max) * volume_height

        # Draw Bollinger band area first (if provided)
        if bollinger and "upper" in bollinger and "lower" in bollinger:
            upper = bollinger["upper"]
            lower = bollinger["lower"]
            if len(upper) == len(rows) and len(lower) == len(rows):
                points = []
                # Upper band (left to right)
                for i, val in enumerate(upper):
                    px = x + (i / (len(rows) - 1)) * width if len(rows) > 1 else x
                    py = price_to_y(val)
                    points.append(f"{px},{py}")
                # Lower band (right to left)
                for i in range(len(lower) - 1, -1, -1):
                    px = x + (i / (len(rows) - 1)) * width if len(rows) > 1 else x
                    py = price_to_y(lower[i])
                    points.append(f"{px},{py}")

                fill_color = "#e3f2fd" if theme == "light" else "#1a3a5c"
                parts.append(
                    f'    <polygon points="{" ".join(points)}" '
                    f'fill="{fill_color}" opacity="0.4"/>'
                )

                # Middle band (dashed line)
                if "middle" in bollinger and len(bollinger["middle"]) == len(rows):
                    middle_points = []
                    for i, val in enumerate(bollinger["middle"]):
                        px = x + (i / (len(rows) - 1)) * width if len(rows) > 1 else x
                        py = price_to_y(val)
                        middle_points.append(f"{px},{py}")
                    parts.append(
                        f'    <polyline points="{" ".join(middle_points)}" '
                        f'fill="none" stroke="#2196F3" stroke-width="1.5" '
                        f'stroke-dasharray="3,3"/>'
                    )

        # Draw horizontal grid lines (5 lines)
        for i in range(5):
            price = price_min + price_range * i / 4
            label_y = price_to_y(price)
            # Grid line
            parts.append(
                f'    <line x1="{x}" y1="{label_y}" x2="{x + width}" y2="{label_y}" '
                f'stroke="{grid_color}" stroke-width="1"/>'
            )
            # Y-axis label
            parts.append(
                f'    <text x="{x - 5}" y="{label_y + 4}" '
                f'font-family="{FONT_FAMILY}" font-size="10" '
                f'fill="{label_color}" text-anchor="end">{int(price):,}</text>'
            )

        # Calculate candle dimensions
        slot_width = width / len(rows)
        candle_width = max(slot_width * 0.7, 2)

        # Draw volume separator if volume enabled
        if volume and volume_height > 0:
            separator_y = y + price_height
            parts.append(
                f'    <line x1="{x}" y1="{separator_y}" x2="{x + width}" y2="{separator_y}" '
                f'stroke="{grid_color}" stroke-width="1"/>'
            )

        # Draw candles (wick first, then body)
        candle_svg_elements: list[tuple[int, str]] = []  # (z-index, svg)

        for i, row in enumerate(rows):
            open_p = row["open"]
            high_p = row["high"]
            low_p = row["low"]
            close_p = row["close"]
            vol = row.get("volume", 0)

            is_bullish = close_p > open_p
            is_bearish = close_p < open_p

            color = KOREAN_BULLISH if is_bullish else KOREAN_BEARISH

            cx = x + i * slot_width + slot_width / 2

            # Wick (high to low)
            wick_y1 = price_to_y(high_p)
            wick_y2 = price_to_y(low_p)
            candle_svg_elements.append(
                (0, f'    <line x1="{cx}" y1="{wick_y1}" x2="{cx}" y2="{wick_y2}" stroke="{color}" stroke-width="1"/>')
            )

            # Body
            open_y = price_to_y(open_p)
            close_y = price_to_y(close_p)
            body_top = min(open_y, close_y)
            body_height = max(abs(close_y - open_y), 1.5)

            candle_x = cx - candle_width / 2
            candle_svg_elements.append(
                (1, f'    <rect x="{candle_x}" y="{body_top}" width="{candle_width}" height="{body_height}" fill="{color}"/>')
            )

            # Volume bar
            if volume and volume_height > 0 and vol > 0:
                vol_h = volume_to_h(vol)
                vol_y = y + height - vol_h
                candle_svg_elements.append(
                    (0, f'    <rect x="{candle_x}" y="{vol_y}" width="{candle_width}" height="{vol_h}" fill="{color}" opacity="0.4"/>')
                )

        # Sort by z-index and add to parts
        candle_svg_elements.sort(key=lambda t: t[0])
        for _, element in candle_svg_elements:
            parts.append(element)

        # Draw EMA lines (over candles)
        if ema_values:
            for idx, (_name, values) in enumerate(ema_values.items()):
                if len(values) == len(rows):
                    ema_points = []
                    for i, val in enumerate(values):
                        px = x + (i / (len(rows) - 1)) * width if len(rows) > 1 else x
                        py = price_to_y(val)
                        ema_points.append(f"{px},{py}")

                    color = EMA_COLORS[idx % len(EMA_COLORS)]
                    parts.append(
                        f'    <polyline points="{" ".join(ema_points)}" '
                        f'fill="none" stroke="{color}" stroke-width="1.5" '
                        f'stroke-dasharray="5,3"/>'
                    )

        # X-axis date labels (5 to 7 labels depending on row count)
        label_count = min(7, max(5, len(rows) // 10 or 1))
        label_step = max(1, len(rows) // label_count)

        for i in range(0, len(rows), label_step):
            row = rows[i]
            date_val = row.get("date", "")
            label_x = x + i * slot_width + slot_width / 2
            label_y = y + price_height - 5

            # Format date: show MM-DD if YYYY-MM-DD format
            if isinstance(date_val, str) and len(date_val) >= 10 and date_val[4] == "-":
                label_text = f"{date_val[5:7]}-{date_val[8:10]}"
            else:
                label_text = str(date_val)

            parts.append(
                f'    <text x="{label_x}" y="{label_y}" '
                f'font-family="{FONT_FAMILY}" font-size="9" '
                f'fill="{label_color}" text-anchor="middle">{label_text}</text>'
            )

        return "\n".join(parts) + "\n"
