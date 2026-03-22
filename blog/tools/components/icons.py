"""Lucide icon system for SVG components.

Provides vector icon paths from Lucide icons as SVG path data.
Icons are rendered at 24x24 viewBox and can be scaled as needed.

Usage:
    from blog.tools.components.icons import Icon
    svg = Icon.render("chart-line", x=100, y=200, size=48, color="#2196F3")
"""

from __future__ import annotations

# Lucide icon paths (24x24 viewBox)
# Path data extracted from https://github.com/lucide-icons/lucide
# All paths use stroke-linecap="round" stroke-linejoin="round"
# Multi-path icons stored as tuple of path strings
ICON_PATHS: dict[str, tuple[str, ...]] = {
    # Chart icons
    "chart-line": (
        "M3 3v16a2 2 0 0 0 2 2h16",
        "m19 9-5 5-4-4-3 3",
    ),
    "chart-bar": (
        "M3 3v16a2 2 0 0 0 2 2h16",
        "M18 17V9",
        "M13 17V5",
        "M8 17v-3",
    ),
    "candlestick-chart": (
        "M9 5v14",
        "M15 5v14",
        "M5 9h14",
        "M5 15h14",
        "M9 2v3",
        "M9 19v3",
        "M15 2v3",
        "M15 19v3",
    ),
    # Trending icons
    "trending-up": (
        "M16 7h6v6",
        "m22 7-8.5 8.5-5-5L2 17",
    ),
    "trending-down": (
        "M16 17h6v-6",
        "m22 17-8.5-8.5-5 5L2 7",
    ),
    # Tech/Server icons
    "server": (
        "M5 10h14",
        "M5 14h14",
        "M19 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z",
    ),
    "database": (
        "M12 8c4.97 0 9-1.79 9-4s-4.03-4-9-4-9 1.79-9 4 4.03 4 9 4",
        "M12 8v12",
        "M12 20c-4.97 0-9-1.79-9-4",
        "M12 20c4.97 0 9-1.79 9-4",
    ),
    "cpu": (
        "M4 4h16v16H4z",
        "M9 9h6v6H9z",
        "M9 1v3",
        "M15 1v3",
        "M9 20v3",
        "M15 20v3",
        "M20 9h3",
        "M20 15h3",
        "M1 9h3",
        "M1 15h3",
    ),
    # Code/Terminal icons
    "code": (
        "m16 18 6-6-6-6",
        "M8 6l-6 6 6 6",
    ),
    "terminal": (
        "m4 17 6-6-6-6",
        "M12 19h8",
    ),
    # Globe/World icons
    "globe": (
        "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z",
        "M2 12h20",
        "M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z",
    ),
    # Money/Finance icons
    "dollar-sign": (
        "M12 2v20",
        "M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6",
    ),
    "wallet": (
        "M21 12V7H5a2 2 0 0 1-2-2 2 2 0 0 1 2-2h14v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7",
    ),
    # Shield/Security icons
    "shield": (
        "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
    ),
    "zap": (
        "M13 2 3 14h9l-1 8 10-12h-9l1-8z",
    ),
    # Alert/Status icons
    "alert-triangle": (
        "M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z",
        "M12 9v4",
        "M12 17h.01",
    ),
    "check-circle": (
        "M22 11.08V12a10 10 0 1 1-5.93-9.14",
        "M22 4 12 14.01l-3-3",
    ),
    "x-circle": (
        "M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0z",
        "M15 9l-6 6",
        "M9 9l6 6",
    ),
    # Bot/AI icons
    "bot": (
        "M12 8V4H8",
        "M12 4h4v4h-4z",
        "M8 8v8h8V8",
        "M2 14h4",
        "M20 14h2",
        "M12 16v4",
        "M8 20h8",
    ),
    "send": (
        "M22 2 11 13",
        "M22 2l-7 20-4-9-9-4 20-7z",
    ),
    "message-square": (
        "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
    ),
    # Arrow icons
    "arrow-up": (
        "M12 19V5",
        "M5 12l7-7 7 7",
    ),
    "arrow-down": (
        "M12 5v14",
        "M5 12l7 7 7-7",
    ),
    "arrow-right": (
        "M5 12h14",
        "M12 5l7 7-7 7",
    ),
    "refresh-cw": (
        "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8",
        "M21 3v5h-5",
        "M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16",
        "M8 21h5v-5",
    ),
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
            SVG group element containing the icon path(s), or empty string if unknown.
        """
        if name not in ICON_PATHS:
            return ""

        path_data = ICON_PATHS[name]
        scale = size / 24

        # Build path elements
        path_elements = []
        for segment in path_data:
            path_elements.append(
                f'<path d="{segment}" fill="none" stroke="{color}" '
                f'stroke-width="{stroke_width}" stroke-linecap="round" stroke-linejoin="round"/>'
            )

        paths_svg = "\n".join(path_elements)

        return (
            f'<g transform="translate({x}, {y}) scale({scale})">'
            f'{paths_svg}'
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
