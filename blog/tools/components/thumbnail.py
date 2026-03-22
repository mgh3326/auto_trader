"""Blog thumbnail template component.

Generates standardized 1200×630 thumbnail images with gradient background,
title, subtitle, icon grid, and tech stack footer.
"""

from __future__ import annotations

from blog.tools.components.base import FONT_FAMILY, THEMES
from blog.tools.components.icons import Icon


class ThumbnailTemplate:
    """Blog thumbnail template — 1200×630 standard layout."""

    @staticmethod
    def _render_icon_cell(icon_name_or_emoji: str, label: str, color: str, x: int) -> str:
        """Render a single icon cell with Lucide icon or emoji.

        Args:
            icon_name_or_emoji: Lucide icon name or emoji character.
            label: Label text below icon.
            color: Background color.
            x: X position of the cell.

        Returns:
            SVG fragment for the icon cell.
        """
        parts = [
            f'        <rect x="{x}" y="0" width="100" height="100" rx="10" fill="{color}" opacity="0.9"/>'
        ]

        # Check if it's a Lucide icon or emoji/text
        if Icon.exists(icon_name_or_emoji):
            # Lucide icon - render as path
            icon_svg = Icon.render(icon_name_or_emoji, x + 26, y=26, size=48, color="#ffffff")
            parts.append(f"        {icon_svg}")
        else:
            # Emoji or text
            parts.append(
                f'        <text x="{x + 50}" y="55" font-family="{FONT_FAMILY}" '
                f'font-size="40" fill="#ffffff" text-anchor="middle">{icon_name_or_emoji}</text>'
            )

        # Label
        parts.append(
            f'        <text x="{x + 50}" y="85" font-family="{FONT_FAMILY}" '
            f'font-size="14" fill="#ffffff" text-anchor="middle">{label}</text>'
        )

        return "\n".join(parts)

    @staticmethod
    def create(
        title_line1: str,
        title_line2: str = "",
        subtitle: str = "",
        icons: list[tuple[str, str, str]] | None = None,
        tech_stack: str = "",
        bg_gradient: tuple[str, str, str] = ("#0d1b2a", "#1b263b", "#415a77"),
        accent_color: str = "#4CAF50",
        theme: str | None = None,
        bg_pattern: str = "none",
    ) -> str:
        """Generate a thumbnail SVG (1200×630).

        Args:
            title_line1: Primary title line.
            title_line2: Secondary title line (optional, uses accent color).
            subtitle: Subtitle text below titles.
            icons: List of (icon_name/emoji, label, color) tuples for icon grid.
            tech_stack: Footer text showing technologies used.
            bg_gradient: Three-stop gradient colors (top, mid, bottom).
            accent_color: Highlight color for title_line2.
            theme: Optional theme name (e.g., "dark", "light", "terminal", "crisis").
            bg_pattern: Background pattern ("none", "candlestick", "grid", "dots", "wave").
        """
        width, height = 1200, 630

        # Apply theme if specified
        default_accent = "#4CAF50"
        if theme and theme in THEMES:
            theme_obj = THEMES[theme]
            bg_gradient = theme_obj.bg_gradient
            if accent_color == default_accent:
                accent_color = theme_obj.accent

        svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="bgGradient" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" style="stop-color:{bg_gradient[0]};stop-opacity:1" />
            <stop offset="50%" style="stop-color:{bg_gradient[1]};stop-opacity:1" />
            <stop offset="100%" style="stop-color:{bg_gradient[2]};stop-opacity:1" />
        </linearGradient>
    </defs>

    <!-- 배경 -->
    <rect width="{width}" height="{height}" fill="url(#bgGradient)"/>
'''

        # Background pattern
        if bg_pattern != "none":
            pattern_svg = ThumbnailTemplate._render_pattern(bg_pattern, width, height)
            svg += pattern_svg

        # Title
        svg += f'''
    <!-- 제목 -->
    <text x="{width // 2}" y="140" font-family="{FONT_FAMILY}" font-size="52" font-weight="bold" fill="#ffffff" text-anchor="middle">
        {title_line1}
    </text>
'''

        if title_line2:
            svg += f'''    <text x="{width // 2}" y="210" font-family="{FONT_FAMILY}" font-size="52" font-weight="bold" fill="{accent_color}" text-anchor="middle">
        {title_line2}
    </text>
'''
            subtitle_y = 290
        else:
            subtitle_y = 220

        if subtitle:
            svg += f'''    <!-- 부제목 -->
    <text x="{width // 2}" y="{subtitle_y}" font-family="{FONT_FAMILY}" font-size="30" fill="#778da9" text-anchor="middle">
        {subtitle}
    </text>
'''

        if icons:
            icon_start_x = width // 2 - (len(icons) * 130) // 2 + 50
            svg += f'\n    <!-- 아이콘들 -->\n    <g transform="translate({icon_start_x}, 380)">\n'

            for i, (icon_name_or_emoji, label, color) in enumerate(icons):
                x = i * 130
                svg += ThumbnailTemplate._render_icon_cell(icon_name_or_emoji, label, color, x) + "\n"
            svg += "    </g>\n"

        if tech_stack:
            svg += f'''    <!-- 하단 기술 스택 -->
    <text x="{width // 2}" y="590" font-family="{FONT_FAMILY}" font-size="20" fill="#778da9" text-anchor="middle">
        {tech_stack}
    </text>
'''

        svg += "</svg>"
        return svg

    @staticmethod
    def _render_pattern(pattern: str, width: int, height: int) -> str:
        """Render a background pattern overlay.

        Args:
            pattern: Pattern name ("candlestick", "grid", "dots", "wave").
            width: SVG width.
            height: SVG height.

        Returns:
            SVG fragment for the pattern.
        """
        if pattern == "candlestick":
            return ThumbnailTemplate._pattern_candlestick(width, height)
        elif pattern == "grid":
            return ThumbnailTemplate._pattern_grid(width, height)
        elif pattern == "dots":
            return ThumbnailTemplate._pattern_dots(width, height)
        elif pattern == "wave":
            return ThumbnailTemplate._pattern_wave(width, height)
        return ""

    @staticmethod
    def _pattern_candlestick(width: int, height: int) -> str:
        """Candlestick chart pattern."""
        # Fixed positions for candlesticks (deterministic, not random)
        candles = [
            (100, 50, 30, 80, "#ff5252"),   # bearish
            (200, 100, 25, 60, "#4CAF50"),  # bullish
            (300, 80, 35, 70, "#ff5252"),
            (400, 120, 20, 50, "#4CAF50"),
            (500, 60, 30, 90, "#ff5252"),
            (600, 90, 25, 55, "#4CAF50"),
            (700, 70, 30, 75, "#ff5252"),
            (800, 110, 20, 45, "#4CAF50"),
            (900, 55, 35, 85, "#ff5252"),
            (1000, 95, 25, 50, "#4CAF50"),
            (1100, 75, 30, 70, "#4CAF50"),
        ]

        parts = ['    <!-- Background pattern: candlesticks -->']
        for x, y, w, h, color in candles:
            parts.append(
                f'    <rect x="{x}" y="{y}" width="{w}" height="{h}" '
                f'fill="{color}" opacity="0.05" rx="2"/>'
            )
        parts.append("")
        return "\n".join(parts)

    @staticmethod
    def _pattern_grid(width: int, height: int) -> str:
        """Grid pattern."""
        parts = ['    <!-- Background pattern: grid -->']
        # Vertical lines
        for x in range(100, width, 100):
            parts.append(
                f'    <line x1="{x}" y1="0" x2="{x}" y2="{height}" '
                f'stroke="#ffffff" stroke-width="1" opacity="0.05"/>'
            )
        # Horizontal lines
        for y in range(100, height, 100):
            parts.append(
                f'    <line x1="0" y1="{y}" x2="{width}" y2="{y}" '
                f'stroke="#ffffff" stroke-width="1" opacity="0.05"/>'
            )
        parts.append("")
        return "\n".join(parts)

    @staticmethod
    def _pattern_dots(width: int, height: int) -> str:
        """Dots pattern."""
        parts = ['    <!-- Background pattern: dots -->']
        # Fixed grid of dots
        for x in range(50, width, 80):
            for y in range(50, height, 80):
                parts.append(
                    f'    <circle cx="{x}" cy="{y}" r="2" '
                    f'fill="#ffffff" opacity="0.08"/>'
                )
        parts.append("")
        return "\n".join(parts)

    @staticmethod
    def _pattern_wave(width: int, height: int) -> str:
        """Wave pattern."""
        parts = ['    <!-- Background pattern: wave -->']
        # Simple wave lines
        for offset in range(0, 200, 40):
            parts.append(
                f'    <path d="M0,{150 + offset} Q{width//4},{100 + offset} '
                f'{width//2},{150 + offset} T{width},{150 + offset}" '
                f'fill="none" stroke="#ffffff" stroke-width="2" opacity="0.06"/>'
            )
        parts.append("")
        return "\n".join(parts)
