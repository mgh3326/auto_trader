"""SVG component system foundation.

Provides color constants, number formatting utilities, and base SVG helpers.
All components in this package return SVG fragment strings (no <svg> wrapper)
that can be composed into complete SVG documents using SVGComponent.header/footer.
"""

from __future__ import annotations


def escape_xml(text: str) -> str:
    """Escape XML special characters for safe SVG embedding."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class Colors:
    """Standardized color palette for all SVG components."""

    # Signal colors
    BULLISH = "#4CAF50"
    BEARISH = "#e74c3c"
    NEUTRAL = "#FF9800"

    # Analysis perspective colors
    TECHNICAL = "#2196F3"
    FUNDAMENTAL = "#4CAF50"
    SUPPLY_DEMAND = "#FF9800"
    CONCLUSION = "#9C27B0"

    # Background & structural
    BG_LIGHT = "#f8f9fa"
    BG_CARD = "#ffffff"
    BORDER = "#dee2e6"
    TEXT_PRIMARY = "#333333"
    TEXT_SECONDARY = "#666666"
    TEXT_MUTED = "#999999"


def format_price(value: float, currency: str = "KRW") -> str:
    """Format a price value with appropriate currency formatting.

    Args:
        value: The numeric price value.
        currency: "KRW" for Korean Won (integer), "USD" for US Dollar (2 decimals).

    Returns:
        Formatted string like '199,800' or '$12.34'.
    """
    if currency == "USD":
        return f"${value:,.2f}"
    return f"{int(value):,}"


def format_large(value: float) -> str:
    """Format large numbers with Korean magnitude suffixes.

    Args:
        value: The numeric value (e.g. 197_700_000_000_000).

    Returns:
        Formatted string like '197.7조', '8,000억', '5,000만'.
    """
    abs_val = abs(value)
    sign = "-" if value < 0 else ""

    if abs_val >= 1_000_000_000_000:  # 조 (trillion)
        return f"{sign}{abs_val / 1_000_000_000_000:.1f}조"
    if abs_val >= 100_000_000:  # 억 (hundred million)
        return f"{sign}{round(abs_val / 100_000_000):,}억"
    if abs_val >= 10_000:  # 만 (ten thousand)
        return f"{sign}{int(abs_val / 10_000):,}만"
    return f"{sign}{int(abs_val):,}"


def format_pct(value: float) -> str:
    """Format a decimal ratio as a percentage string.

    Args:
        value: Decimal ratio (e.g. 0.4327 → '+43.3%').

    Returns:
        Formatted string with sign prefix.
    """
    pct = value * 100
    if pct > 0:
        return f"+{pct:.1f}%"
    if pct < 0:
        return f"{pct:.1f}%"
    return f"{pct:.1f}%"


class SVGComponent:
    """Base SVG helpers for composing complete SVG documents.

    Usage:
        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800)
        svg += SVGComponent.title(1400, "My Chart")
        svg += SomeComponent.create(...)
        svg += SVGComponent.footer()
    """

    @staticmethod
    def header(width: int, height: int, extra_defs: str = "") -> str:
        """Generate SVG document header with standard defs."""
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#666666" />
        </marker>
        {extra_defs}
    </defs>
'''

    @staticmethod
    def footer() -> str:
        """Close the SVG document."""
        return "</svg>"

    @staticmethod
    def background(width: int, height: int, fill: str = "#f8f9fa") -> str:
        """Full-canvas background rectangle."""
        return f'    <rect width="{width}" height="{height}" fill="{fill}"/>\n'

    @staticmethod
    def title(
        canvas_width: int,
        text: str,
        y: int = 45,
        font_size: int = 28,
        fill: str = "#1a1a2e",
    ) -> str:
        """Centered title text."""
        return (
            f'    <text x="{canvas_width // 2}" y="{y}" '
            f'font-family="Arial, sans-serif" font-size="{font_size}" '
            f'font-weight="bold" fill="{fill}" text-anchor="middle">'
            f"{escape_xml(text)}</text>\n"
        )
