"""InvestorFlow SVG component.

Horizontal bar chart for foreign/institutional net trading flow.
"""

from __future__ import annotations

from blog.tools.components.bar_chart import BarChart
from blog.tools.components.base import Colors, escape_xml


class InvestorFlow:
    """Investor trading flow visualization."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        investor_trends: dict,
    ) -> str:
        """Render investor flow chart as an SVG fragment.

        Args:
            x: X position.
            y: Y position.
            width: Width.
            height: Height.
            investor_trends: Dict with foreign_net, institution_net, individual_net,
                           foreign_consecutive_sell_days.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        foreign = investor_trends.get("foreign_net", 0)
        inst = investor_trends.get("institution_net", 0)
        ind = investor_trends.get("individual_net", 0)
        consec = investor_trends.get("foreign_consecutive_sell_days", 0)

        # Title
        parts.append(
            f'    <text x="{x + width // 2}" y="{y + 25}" '
            f'font-family="Arial, sans-serif" font-size="16" '
            f'font-weight="bold" fill="#333333" text-anchor="middle">'
            f'투자자별 순매매동향 (단위: 백만원)</text>'
        )

        # Warning for consecutive foreign selling
        if consec >= 3:
            parts.append(
                f'    <text x="{x + width // 2}" y="{y + 45}" '
                f'font-family="Arial, sans-serif" font-size="12" '
                f'fill="{Colors.BEARISH}" text-anchor="middle">'
                f'⚠️ 외국인 {consec}일 연속 순매도</text>'
            )

        # Bar chart data
        data = [
            ("외국인", foreign / 1e6, Colors.BULLISH if foreign > 0 else Colors.BEARISH),
            ("기관", inst / 1e6, Colors.BULLISH if inst > 0 else Colors.BEARISH),
            ("개인", ind / 1e6, Colors.TECHNICAL if ind > 0 else Colors.NEUTRAL),
        ]

        parts.append(
            BarChart.create(
                x=x + 80, y=y + 60, width=width - 100, height=height - 80,
                data=data, direction="horizontal", show_labels=True,
            )
        )

        return "\n".join(parts)
