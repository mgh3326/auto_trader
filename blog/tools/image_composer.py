"""Image composition utilities for embedding PNG screenshots into SVG layouts.

This module provides tools to compose hybrid technical analysis images
where a real chart screenshot is embedded within an SVG layout alongside
vector-based indicators and support/resistance levels.
"""

from __future__ import annotations

import base64
from pathlib import Path

from blog.tools.components.base import FONT_FAMILY, SVGComponent


class ImageComposer:
    """Composes PNG images into SVG layouts for hybrid technical analysis.

    The composer is pure and deterministic - it reads PNG data and returns
    SVG fragments without any file I/O beyond reading the source PNG.
    """

    @staticmethod
    def embed_png(
        png_path: Path,
        x: int,
        y: int,
        width: int,
        height: int,
        border: bool = True,
        shadow: bool = True,
    ) -> str:
        """Embed a PNG file as base64-encoded image in SVG.

        Args:
            png_path: Path to the PNG file
            x: X position in SVG
            y: Y position in SVG
            width: Display width
            height: Display height
            border: Whether to add a border stroke
            shadow: Whether to add a drop shadow filter

        Returns:
            SVG image element string, or a comment if file not found
        """
        if not png_path.exists():
            return f"<!-- Image not found: {png_path} -->"

        png_bytes = png_path.read_bytes()
        base64_data = base64.b64encode(png_bytes).decode("ascii")

        clip_id = f"clip-{x}-{y}-{width}-{height}"

        # Build filter attribute
        filter_attr = 'filter="url(#dropShadow)"' if shadow else ""

        # Build stroke attribute for border
        stroke_attrs = 'stroke="#415a77" stroke-width="2"' if border else ""

        svg_parts = [
            f'    <clipPath id="{clip_id}">',
            f'        <rect x="{x}" y="{y}" width="{width}" height="{height}"/>',
            "    </clipPath>",
        ]

        if shadow:
            svg_parts.extend([
                '    <defs>',
                '        <filter id="dropShadow" x="-20%" y="-20%" width="140%" height="140%">',
                '            <feGaussianBlur in="SourceAlpha" stdDeviation="3"/>',
                '            <feOffset dx="2" dy="2" result="offsetblur"/>',
                '            <feComponentTransfer>',
                '                <feFuncA type="linear" slope="0.3"/>',
                '            </feComponentTransfer>',
                '            <feMerge>',
                '                <feMergeNode/>',
                '                <feMergeNode in="SourceGraphic"/>',
                '            </feMerge>',
                '        </filter>',
                '    </defs>',
            ])

        svg_parts.append(
            f'    <image x="{x}" y="{y}" width="{width}" height="{height}" '
            f'clip-path="url(#{clip_id})" {filter_attr} '
            f'xlink:href="data:image/png;base64,{base64_data}"/>'
        )

        if border:
            svg_parts.append(
                f'    <rect x="{x}" y="{y}" width="{width}" height="{height}" '
                f'fill="none" {stroke_attrs} rx="4"/>'
            )

        return "\n".join(svg_parts) + "\n"

    @staticmethod
    def create_hybrid_technical(
        screenshot_path: Path,
        indicator_fragment: str,
        support_resistance_fragment: str,
        company_name: str,
        width: int = 1400,
        height: int = 800,
        theme: str = "dark",
    ) -> str:
        """Create a hybrid technical analysis SVG.

        Layout:
        - Left (60%): Embedded chart screenshot
        - Right (35%): Indicator dashboard (vector)
        - Bottom: Support/resistance levels (vector)

        Args:
            screenshot_path: Path to the captured chart screenshot PNG
            indicator_fragment: SVG fragment for the indicator panel
            support_resistance_fragment: SVG fragment for support/resistance
            company_name: Company name for the title
            width: SVG canvas width
            height: SVG canvas height
            theme: Color theme (dark, light, etc.)

        Returns:
            Complete SVG document as string
        """
        # Screenshot positioned on left ~60%
        screenshot_x = 60
        screenshot_y = 95
        screenshot_width = 780
        screenshot_height = 350

        svg = SVGComponent.header(width, height)
        svg += SVGComponent.background(width, height, theme=theme)
        svg += SVGComponent.title(width, f"{company_name} — 기술적 분석", fill="#e0e1dd")

        # Embed the screenshot
        svg += ImageComposer.embed_png(
            screenshot_path,
            x=screenshot_x,
            y=screenshot_y,
            width=screenshot_width,
            height=screenshot_height,
            border=True,
            shadow=True,
        )

        # Add indicator dashboard on the right
        svg += indicator_fragment

        # Add support/resistance at the bottom
        svg += support_resistance_fragment

        svg += SVGComponent.footer()
        return svg
