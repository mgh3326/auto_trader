"""InfoCard SVG component.

A rectangular card with a colored header strip, large value display,
optional description, and optional sub-items. Used for KPI displays,
indicator summaries, and metric cards.

Layout:
    ├─── [title] ─────────┐
    │                      │
    │      [value]         │
    │  [description]       │
    │  [sub_item_1]        │
    │  [sub_item_2]        │
    └──────────────────────┘
"""

from __future__ import annotations

from blog.tools.components.base import FONT_FAMILY, escape_xml


class InfoCard:
    """Single information card — colored header + large value + description."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        title: str,
        value: str,
        description: str = "",
        color: str = "#2196F3",
        highlight: bool = False,
        sub_items: list[tuple[str, str]] | None = None,
    ) -> str:
        """Render an info card as an SVG fragment.

        Args:
            x: X position of the card.
            y: Y position of the card.
            width: Card width.
            height: Card height.
            title: Header text (e.g., "PER", "RSI").
            value: Large display value (e.g., "30.38", "+43.3%").
            description: Optional description text below value.
            color: Header strip and accent color.
            highlight: If True, use thicker border and slight shadow effect.
            sub_items: Optional list of (label, value) pairs below description.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        stroke_width = 3 if highlight else 1
        header_h = 32

        safe_title = escape_xml(title)
        safe_value = escape_xml(value)

        parts: list[str] = []

        # Card body
        parts.append(
            f'    <rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'fill="#ffffff" stroke="{color}" stroke-width="{stroke_width}" rx="8"/>'
        )

        # Colored header strip
        parts.append(
            f'    <rect x="{x}" y="{y}" width="{width}" height="{header_h}" '
            f'fill="{color}" rx="8"/>'
        )
        # Square off bottom corners of header
        parts.append(
            f'    <rect x="{x}" y="{y + header_h - 8}" width="{width}" height="8" '
            f'fill="{color}"/>'
        )

        # Title text (centered in header)
        parts.append(
            f'    <text x="{x + width // 2}" y="{y + header_h // 2 + 5}" '
            f'font-family="{FONT_FAMILY}" font-size="14" font-weight="bold" '
            f'fill="#ffffff" text-anchor="middle">{safe_title}</text>'
        )

        # Value (large, centered)
        value_y = y + header_h + 35
        parts.append(
            f'    <text x="{x + width // 2}" y="{value_y}" '
            f'font-family="{FONT_FAMILY}" font-size="28" font-weight="bold" '
            f'fill="{color}" text-anchor="middle">{safe_value}</text>'
        )

        # Description
        current_y = value_y + 25
        if description:
            safe_desc = escape_xml(description)
            parts.append(
                f'    <text x="{x + width // 2}" y="{current_y}" '
                f'font-family="{FONT_FAMILY}" font-size="12" '
                f'fill="#666666" text-anchor="middle">{safe_desc}</text>'
            )
            current_y += 20

        # Sub-items
        if sub_items:
            for label, val in sub_items:
                safe_label = escape_xml(label)
                safe_val = escape_xml(val)
                parts.append(
                    f'    <text x="{x + 15}" y="{current_y}" '
                    f'font-family="{FONT_FAMILY}" font-size="11" '
                    f'fill="#666666">{safe_label}: {safe_val}</text>'
                )
                current_y += 18

        return "\n".join(parts) + "\n"
