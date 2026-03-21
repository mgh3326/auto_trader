"""ConclusionCard SVG component.

Multi-perspective summary card with verdict indicators.
"""

from __future__ import annotations

from typing import Any

from blog.tools.components.base import Colors
from blog.tools.components.card import InfoCard


class ConclusionCard:
    """Multi-perspective analysis conclusion summary."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        data: dict[str, Any],
        company_name: str,
    ) -> str:
        """Render conclusion card as an SVG fragment.

        Args:
            x: X position.
            y: Y position.
            width: Width.
            height: Height.
            data: Full analysis data dict.
            company_name: Company name for display.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        # 4-panel layout: Technical, Fundamental, Supply-Demand, Overall
        card_w = (width - 30) // 2
        card_h = (height - 50) // 2
        gap = 10

        indicators = data.get("indicators", {})
        valuation = data.get("valuation", {})
        investor = data.get("investor_trends", {})

        # Technical verdict
        rsi = indicators.get("rsi14", 50)
        macd = indicators.get("macd_histogram", 0)
        tech_signal = (
            "매수"
            if rsi < 40 and macd > 0
            else "매도"
            if rsi > 70 and macd < 0
            else "중립"
        )
        parts.append(
            InfoCard.create(
                x=x,
                y=y,
                width=card_w,
                height=card_h,
                title="기술적 분석",
                value=tech_signal,
                description=f"RSI: {rsi:.1f}, MACD: {macd:,.0f}",
                color=Colors.TECHNICAL,
            )
        )

        # Fundamental verdict
        per = valuation.get("per", 0)
        roe = valuation.get("roe", 0)
        fund_signal = (
            "매수" if per < 15 and roe > 10 else "매도" if per > 40 else "중립"
        )
        parts.append(
            InfoCard.create(
                x=x + card_w + gap,
                y=y,
                width=card_w,
                height=card_h,
                title="펀더멘탈 분석",
                value=fund_signal,
                description=f"PER: {per:.2f}, ROE: {roe:.2f}%",
                color=Colors.FUNDAMENTAL,
            )
        )

        # Supply-Demand verdict
        foreign = investor.get("foreign_net", 0)
        consec = investor.get("foreign_consecutive_sell_days", 0)
        if consec >= 3:
            sd_signal = "매도"
            sd_desc = f"외국인 {consec}일 연속 순매도"
        elif foreign > 0:
            sd_signal = "매수"
            sd_desc = f"외국인 순매수 {foreign / 1e9:.0f}억"
        else:
            sd_signal = "중립"
            sd_desc = "수급 대기"
        parts.append(
            InfoCard.create(
                x=x,
                y=y + card_h + gap,
                width=card_w,
                height=card_h,
                title="수급 분석",
                value=sd_signal,
                description=sd_desc,
                color=Colors.SUPPLY_DEMAND,
            )
        )

        # Overall verdict (weighted combination)
        scores = {"매수": 0, "중립": 0, "매도": 0}
        scores[tech_signal] += 1
        scores[fund_signal] += 1
        scores[sd_signal] += 1

        overall = max(scores, key=lambda k: scores[k])

        parts.append(
            InfoCard.create(
                x=x + card_w + gap,
                y=y + card_h + gap,
                width=card_w,
                height=card_h,
                title="종합 판단",
                value=overall,
                description=f"{company_name} 종합 분석 결과",
                color=Colors.CONCLUSION,
                highlight=True,
            )
        )

        return "\n".join(parts)
