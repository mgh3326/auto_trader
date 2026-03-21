"""Blog thumbnail template component.

Generates standardized 1200×630 thumbnail images with gradient background,
title, subtitle, icon grid, and tech stack footer.
"""

from __future__ import annotations


class ThumbnailTemplate:
    """Blog thumbnail template — 1200×630 standard layout."""

    @staticmethod
    def create(
        title_line1: str,
        title_line2: str = "",
        subtitle: str = "",
        icons: list[tuple[str, str, str]] | None = None,
        tech_stack: str = "",
        bg_gradient: tuple[str, str, str] = ("#0d1b2a", "#1b263b", "#415a77"),
        accent_color: str = "#4CAF50",
    ) -> str:
        """Generate a thumbnail SVG (1200×630).

        Args:
            title_line1: Primary title line.
            title_line2: Secondary title line (optional, uses accent color).
            subtitle: Subtitle text below titles.
            icons: List of (emoji, label, color) tuples for icon grid.
            tech_stack: Footer text showing technologies used.
            bg_gradient: Three-stop gradient colors (top, mid, bottom).
            accent_color: Highlight color for title_line2.
        """
        width, height = 1200, 630

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

    <!-- 제목 -->
    <text x="{width // 2}" y="140" font-family="Arial, sans-serif" font-size="52" font-weight="bold" fill="#ffffff" text-anchor="middle">
        {title_line1}
    </text>
'''

        if title_line2:
            svg += f'''    <text x="{width // 2}" y="210" font-family="Arial, sans-serif" font-size="52" font-weight="bold" fill="{accent_color}" text-anchor="middle">
        {title_line2}
    </text>
'''
            subtitle_y = 290
        else:
            subtitle_y = 220

        if subtitle:
            svg += f'''    <!-- 부제목 -->
    <text x="{width // 2}" y="{subtitle_y}" font-family="Arial, sans-serif" font-size="30" fill="#778da9" text-anchor="middle">
        {subtitle}
    </text>
'''

        if icons:
            icon_start_x = width // 2 - (len(icons) * 130) // 2 + 50
            svg += f'\n    <!-- 아이콘들 -->\n    <g transform="translate({icon_start_x}, 380)">\n'

            for i, (emoji, label, color) in enumerate(icons):
                x = i * 130
                svg += f'''        <rect x="{x}" y="0" width="100" height="100" rx="10" fill="{color}" opacity="0.9"/>
        <text x="{x + 50}" y="55" font-family="Arial, sans-serif" font-size="40" fill="#ffffff" text-anchor="middle">{emoji}</text>
        <text x="{x + 50}" y="85" font-family="Arial, sans-serif" font-size="14" fill="#ffffff" text-anchor="middle">{label}</text>
'''
            svg += "    </g>\n"

        if tech_stack:
            svg += f'''    <!-- 하단 기술 스택 -->
    <text x="{width // 2}" y="590" font-family="Arial, sans-serif" font-size="20" fill="#778da9" text-anchor="middle">
        {tech_stack}
    </text>
'''

        svg += "</svg>"
        return svg
