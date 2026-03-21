"""SupportResistance SVG component.

Horizontal support and resistance level visualization.
"""

from __future__ import annotations

from blog.tools.components.base import Colors, format_price


class SupportResistance:
    """Support and resistance level display."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        supports: list[float],
        resistances: list[float],
        current_price: float,
    ) -> str:
        """Render support/resistance levels as an SVG fragment.

        Args:
            x: X position.
            y: Y position.
            width: Width.
            height: Height.
            supports: List of support price levels.
            resistances: List of resistance price levels.
            current_price: Current market price.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        # Section labels
        parts.append(
            f'    <text x="{x + width // 4}" y="{y + 20}" '
            f'font-family="Arial, sans-serif" font-size="14" '
            f'font-weight="bold" fill="{Colors.BULLISH}" text-anchor="middle">'
            f"지지선 (Support)</text>"
        )
        parts.append(
            f'    <text x="{x + 3 * width // 4}" y="{y + 20}" '
            f'font-family="Arial, sans-serif" font-size="14" '
            f'font-weight="bold" fill="{Colors.BEARISH}" text-anchor="middle">'
            f"저항선 (Resistance)</text>"
        )

        # Support levels
        start_y = y + 50
        for i, level in enumerate(supports[:3]):  # Max 3 levels
            level_y = start_y + i * 40
            bar_width = (1 - abs(level - current_price) / current_price) * (
                width // 2 - 40
            )
            bar_width = max(bar_width, 50)

            parts.append(
                f'    <rect x="{x + 20}" y="{level_y - 10}" width="{bar_width}" '
                f'height="20" fill="{Colors.BULLISH}" opacity="0.2" rx="4"/>'
            )
            parts.append(
                f'    <line x1="{x + 20}" y1="{level_y}" x2="{x + width // 2 - 20}" '
                f'y2="{level_y}" stroke="{Colors.BULLISH}" stroke-width="1" '
                f'stroke-dasharray="4,2"/>'
            )
            parts.append(
                f'    <text x="{x + width // 4}" y="{level_y + 4}" '
                f'font-family="Arial, sans-serif" font-size="12" '
                f'fill="{Colors.BULLISH}" text-anchor="middle">'
                f"{format_price(level)}</text>"
            )

        # Resistance levels
        for i, level in enumerate(resistances[:3]):  # Max 3 levels
            level_y = start_y + i * 40
            bar_width = (1 - abs(level - current_price) / current_price) * (
                width // 2 - 40
            )
            bar_width = max(bar_width, 50)

            parts.append(
                f'    <rect x="{x + width // 2 + 20}" y="{level_y - 10}" '
                f'width="{bar_width}" height="20" fill="{Colors.BEARISH}" '
                f'opacity="0.2" rx="4"/>'
            )
            parts.append(
                f'    <line x1="{x + width // 2 + 20}" y1="{level_y}" '
                f'x2="{x + width - 20}" y2="{level_y}" stroke="{Colors.BEARISH}" '
                f'stroke-width="1" stroke-dasharray="4,2"/>'
            )
            parts.append(
                f'    <text x="{x + 3 * width // 4}" y="{level_y + 4}" '
                f'font-family="Arial, sans-serif" font-size="12" '
                f'fill="{Colors.BEARISH}" text-anchor="middle">'
                f"{format_price(level)}</text>"
            )

        # Current price indicator
        parts.append(
            f'    <text x="{x + width // 2}" y="{y + height - 20}" '
            f'font-family="Arial, sans-serif" font-size="14" '
            f'font-weight="bold" fill="#333333" text-anchor="middle">'
            f"현재가: {format_price(current_price)}</text>"
        )

        return "\n".join(parts) + "\n"
