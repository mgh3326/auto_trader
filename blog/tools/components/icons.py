"""Lucide icon system for SVG components.

Provides vector icon paths from Lucide icons as SVG path data.
Icons are rendered at 24x24 viewBox and can be scaled as needed.

Usage:
    from blog.tools.components.icons import Icon
    svg = Icon.render("chart-line", x=100, y=200, size=48, color="#2196F3")
"""

from __future__ import annotations

# Lucide icon paths (24x24 viewBox)
# Path data extracted from https://lucide.dev
ICON_PATHS: dict[str, str] = {
    "chart-line": 'M3 3v18h18 M18 17V9 M13 17V5 M8 17v-3',
    "chart-bar": 'M3 3v18h18 M18 17V9 M13 17V5 M8 17v-3',
    "trending-up": 'M23 6l-9.5 9.5-5-5L1 18',
    "trending-down": 'M23 18l-9.5-9.5-5 5L1 6',
    "server": 'M5 10h14M5 14h14M19 3H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2V5a2 2 0 00-2-2z',
    "database": 'M12 8c4.97 0 9-1.79 9-4s-4.03-4-9-4-9 1.79-9 4 4.03 4 9 4zm0 0v12m0 0c-4.97 0-9-1.79-9-4m9 4c4.97 0 9-1.79 9-4',
    "cpu": 'M4 4h16v16H4z M9 9h6v6H9z M9 1v3 M15 1v3 M9 20v3 M15 20v3 M20 9h3 M20 15h3 M1 9h3 M1 15h3',
    "code": 'm16 18 6-6-6-6 M8 6l-6 6 6 6',
    "globe": 'M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M2 12h20 M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z',
    "terminal": 'm4 17 6-6-6-6 M12 19h8',
    "dollar-sign": 'M12 2v20 M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6',
    "wallet": 'M21 12V7H5a2 2 0 01-2-2 2 2 0 012-2h14v14a2 2 0 01-2 2H5a2 2 0 01-2-2V7',
    "candlestick-chart": 'M9 5v14 M15 5v14 M5 9h14 M5 15h14 M9 2v3 M9 19v3 M15 2v3 M15 19v3',
    "shield": 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
    "zap": 'M13 2L3 14h9l-1 8 10-12h-9l1-8z',
    "alert-triangle": 'M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01',
    "check-circle": 'M22 11.08V12a10 10 0 11-5.93-9.14 M22 4 12 14.01l-3-3',
    "x-circle": 'M22 12a10 10 0 11-20 0 10 10 0 0120 0z M15 9l-6 6 M9 9l6 6',
    "bot": 'M12 8V4H8 M12 4h4v4h-4z M8 8v8h8V8 M2 14h4 M20 14h2 M12 16v4 M8 20h8',
    "send": 'M22 2L11 13 M22 2l-7 20-4-9-9-4 20-7z',
    "message-square": 'M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z',
    "arrow-up": 'M12 19V5 M5 12l7-7 7 7',
    "arrow-down": 'M12 5v14 M5 12l7 7 7-7',
    "arrow-right": 'M5 12h14 M12 5l7 7-7 7',
    "refresh-cw": 'M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15',
}


class Icon:
    """Lucide icon renderer for SVG."""

    @staticmethod
    def render(
        name: str,
        x: int,
        y: int,
        size: int = 24,
        color: str = "currentColor",
        stroke_width: int = 2,
    ) -> str:
        """Render an icon as an SVG group.

        Args:
            name: Icon name (e.g., "chart-line", "database").
            x: X position.
            y: Y position.
            size: Icon size (default 24).
            color: Stroke color.
            stroke_width: Stroke width.

        Returns:
            SVG group element containing the icon path, or empty string if unknown.
        """
        if name not in ICON_PATHS:
            return ""

        path_data = ICON_PATHS[name]
        scale = size / 24

        return (
            f'<g transform="translate({x}, {y}) scale({scale})">'
            f'<path d="{path_data}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke_width}" stroke-linecap="round" stroke-linejoin="round"/>'
            f'</g>'
        )

    @staticmethod
    def exists(name: str) -> bool:
        """Check if an icon exists."""
        return name in ICON_PATHS

    @staticmethod
    def available() -> list[str]:
        """Return list of available icon names."""
        return list(ICON_PATHS.keys())
