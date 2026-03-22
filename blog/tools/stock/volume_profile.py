"""VolumeProfile SVG component."""

from __future__ import annotations

from typing import Any

from blog.tools.components.base import FONT_FAMILY


class VolumeProfile:
    """Volume profile histogram showing volume distribution by price level."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        ohlcv: list[dict[str, Any]],
        current_price: float | None = None,
        bins: int = 20,
    ) -> str:
        """Render a volume profile as an SVG fragment.

        Args:
            x: X position of the profile.
            y: Y position of the profile.
            width: Profile width.
            height: Profile height.
            ohlcv: List of OHLCV dicts with 'open', 'high', 'low', 'close', 'volume' keys.
            current_price: Optional current price to mark with an indicator.
            bins: Number of price bins to distribute volume into.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        if not ohlcv:
            return f'    <!-- Empty volume profile at ({x}, {y}) -->\n'

        # Filter rows with required keys
        required_keys = {"open", "high", "low", "close", "volume"}
        rows = [row for row in ohlcv if required_keys <= row.keys()]

        if not rows:
            return f'    <!-- No valid OHLCV data for volume profile -->\n'

        # Calculate price range from all rows
        global_min = min(row["low"] for row in rows)
        global_max = max(row["high"] for row in rows)
        price_range = global_max - global_min or 1

        # Create bins
        bin_height = price_range / bins

        # Distribute volume into bins using midpoint heuristic
        bin_volumes: list[float] = [0.0] * bins
        for row in rows:
            # Use midpoint of candle
            midpoint = (row["high"] + row["low"]) / 2
            volume = row.get("volume", 0)

            # Find which bin this belongs to
            bin_idx = int((midpoint - global_min) / bin_height)
            bin_idx = max(0, min(bin_idx, bins - 1))  # Clamp to valid range

            bin_volumes[bin_idx] += volume

        # Find max volume for scaling
        max_volume = max(bin_volumes) if bin_volumes else 1
        if max_volume == 0:
            max_volume = 1

        parts: list[str] = []

        # Draw horizontal bars for each bin
        for i, vol in enumerate(bin_volumes):
            # Price position (inverted: lowest price at bottom)
            bin_price = global_min + (i + 0.5) * bin_height
            price_y = y + height - ((bin_price - global_min) / price_range * height)

            # Bar width proportional to volume
            bar_width = (vol / max_volume) * width

            # Small bar height based on available space
            bar_h = max(height / bins * 0.8, 2)

            bar_y = price_y - bar_h / 2

            # Color intensity based on volume
            opacity = 0.3 + (vol / max_volume) * 0.5

            parts.append(
                f'    <rect x="{x}" y="{bar_y}" width="{bar_width}" height="{bar_h}" '
                f'fill="#2196F3" opacity="{opacity:.2f}"/>'
            )

        # Draw current price marker (arrow/triangle)
        if current_price is not None and global_min <= current_price <= global_max:
            marker_y = y + height - ((current_price - global_min) / price_range * height)
            marker_size = 6

            # Triangle pointing right
            triangle_points = [
                f"{x + width},{marker_y - marker_size}",
                f"{x + width + marker_size * 1.5},{marker_y}",
                f"{x + width},{marker_y + marker_size}",
            ]

            parts.append(
                f'    <polygon points="{" ".join(triangle_points)}" '
                f'fill="#FF5722"/>'
            )

            # Price label
            parts.append(
                f'    <text x="{x + width + marker_size * 2}" y="{marker_y + 3}" '
                f'font-family="{FONT_FAMILY}" font-size="9" '
                f'fill="#FF5722" text-anchor="start">{int(current_price):,}</text>'
            )

        return "\n".join(parts) + "\n"
