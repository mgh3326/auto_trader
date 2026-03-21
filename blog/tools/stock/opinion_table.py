"""OpinionTable SVG component.

Analyst opinions table with consensus summary.
"""

from __future__ import annotations

from typing import Any

from blog.tools.components.base import format_price
from blog.tools.components.table import ComparisonTable


class OpinionTable:
    """Analyst opinions table with consensus summary."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        opinions: dict[str, Any],
    ) -> str:
        """Render opinion table as an SVG fragment.

        Args:
            x: X position.
            y: Y position.
            width: Width.
            height: Height.
            opinions: Dict with 'opinions' list and 'consensus' dict.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        # Title
        parts.append(
            f'    <text x="{x + width // 2}" y="{y + 20}" '
            f'font-family="Arial, sans-serif" font-size="16" '
            f'font-weight="bold" fill="#333333" text-anchor="middle">'
            f"투자의견 및 목표가</text>"
        )

        # Prepare table data
        headers = ["증권사", "의견", "목표가"]
        rows = []

        for op in opinions.get("opinions", []):
            firm = op.get("firm", "")
            rating = op.get("rating", "")
            target = op.get("target", 0)
            rows.append([firm, rating, format_price(target) if target else "-"])

        # Add consensus row
        consensus = opinions.get("consensus", {})
        if consensus:
            rows.append(
                [
                    "컨센서스",
                    consensus.get("rating", "-"),
                    format_price(consensus.get("avg_target", 0)),
                ]
            )

        if rows:
            parts.append(
                ComparisonTable.create(
                    x=x,
                    y=y + 40,
                    width=width,
                    height=height - 50,
                    headers=headers,
                    rows=rows,
                    highlight_row=len(rows) - 1 if consensus else None,
                )
            )

        return "\n".join(parts)
