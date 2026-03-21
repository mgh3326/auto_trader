"""ValuationCards SVG component.

4-panel grid showing PER, PBR, ROE, and consensus target price.
"""

from __future__ import annotations

from blog.tools.components.base import Colors, format_price
from blog.tools.components.card import InfoCard


class ValuationCards:
    """Valuation metrics — 4-panel card grid."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        valuation: dict,
    ) -> str:
        """Render valuation cards as an SVG fragment.

        Args:
            x: X position.
            y: Y position.
            width: Width.
            height: Height.
            valuation: Dict with per, pbr, roe, consensus_target, current_price.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        # 4-card layout
        card_w = (width - 30) // 4
        card_h = height - 20
        gap = 10

        per = valuation.get("per", 0)
        pbr = valuation.get("pbr", 0)
        roe = valuation.get("roe", 0)
        target = valuation.get("consensus_target", 0)
        current = valuation.get("current_price", 0)

        # PER Card
        per_color = Colors.BEARISH if per > 30 else Colors.BULLISH if per < 15 else Colors.NEUTRAL
        parts.append(
            InfoCard.create(
                x=x, y=y, width=card_w, height=card_h,
                title="PER", value=f"{per:.2f}",
                color=per_color,
            )
        )

        # PBR Card
        pbr_color = Colors.BEARISH if pbr > 2 else Colors.BULLISH if pbr < 1 else Colors.NEUTRAL
        parts.append(
            InfoCard.create(
                x=x + card_w + gap, y=y, width=card_w, height=card_h,
                title="PBR", value=f"{pbr:.2f}",
                color=pbr_color,
            )
        )

        # ROE Card
        roe_color = Colors.BULLISH if roe > 10 else Colors.NEUTRAL if roe > 5 else Colors.BEARISH
        parts.append(
            InfoCard.create(
                x=x + 2 * (card_w + gap), y=y, width=card_w, height=card_h,
                title="ROE", value=f"{roe:.2f}%",
                color=roe_color,
            )
        )

        # Consensus Target Card
        if target > 0 and current > 0:
            upside = (target - current) / current * 100
            target_color = Colors.BULLISH if upside > 0 else Colors.BEARISH
            parts.append(
                InfoCard.create(
                    x=x + 3 * (card_w + gap), y=y, width=card_w, height=card_h,
                    title="컨센서스 목표가", value=format_price(target),
                    description=f"{'+' if upside > 0 else ''}{upside:.1f}%",
                    color=target_color,
                )
            )
        else:
            parts.append(
                InfoCard.create(
                    x=x + 3 * (card_w + gap), y=y, width=card_w, height=card_h,
                    title="컨센서스 목표가", value="N/A",
                    color=Colors.TEXT_MUTED,
                )
            )

        return "\n".join(parts)
