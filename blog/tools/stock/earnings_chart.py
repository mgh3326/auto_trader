"""EarningsChart SVG component.

Annual earnings bar chart with quarterly margin subplot.
"""

from __future__ import annotations

from blog.tools.components.bar_chart import BarChart
from blog.tools.components.base import Colors, escape_xml, format_large


class EarningsChart:
    """Annual earnings chart with quarterly margins."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        financials: dict,
    ) -> str:
        """Render earnings chart as an SVG fragment.

        Args:
            x: X position.
            y: Y position.
            width: Width.
            height: Height.
            financials: Dict with 'annual_earnings' and 'quarterly_margins'.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        annual = financials.get("annual_earnings", [])
        quarterly = financials.get("quarterly_margins", [])

        # Title
        parts.append(
            f'    <text x="{x + width // 2}" y="{y + 20}" '
            f'font-family="Arial, sans-serif" font-size="16" '
            f'font-weight="bold" fill="#333333" text-anchor="middle">'
            f'연간 영업이익 및 분기 영업이익률</text>'
        )

        # Annual earnings (top half)
        if annual:
            chart_h = height // 2 - 40
            data = []
            for item in annual:
                year = str(item.get("year", ""))
                income = item.get("operating_income", 0)
                # Color based on trend (simplified)
                color = Colors.BULLISH if income > 0 else Colors.BEARISH
                data.append((year, income / 1e12, color))  # Convert to trillion

            parts.append(
                BarChart.create(
                    x=x + 50, y=y + 40, width=width // 2 - 60, height=chart_h,
                    data=data, direction="vertical", show_labels=True,
                )
            )

            # Legend
            parts.append(
                f'    <text x="{x + 50}" y="{y + chart_h + 60}" '
                f'font-family="Arial, sans-serif" font-size="11" '
                f'fill="#666666">단위: 조원</text>'
            )

        # Quarterly margins (bottom/right)
        if quarterly:
            margin_x = x + width // 2 + 20
            margin_data = []
            for item in quarterly:
                q = str(item.get("quarter", ""))
                margin = item.get("margin", 0) * 100  # Convert to percentage
                color = Colors.BULLISH if margin > 10 else Colors.NEUTRAL if margin > 5 else Colors.BEARISH
                margin_data.append((q, margin, color))

            parts.append(
                BarChart.create(
                    x=margin_x, y=y + 40, width=width // 2 - 40, height=height // 2 - 40,
                    data=margin_data, direction="horizontal", show_labels=True,
                    chart_title="분기 영업이익률",
                )
            )

        return "\n".join(parts)
