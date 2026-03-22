"""SVG component system foundation.

Provides color constants, number formatting utilities, and base SVG helpers.
All components in this package return SVG fragment strings (no <svg> wrapper)
that can be composed into complete SVG documents using SVGComponent.header/footer.
"""

from __future__ import annotations

from dataclasses import dataclass

FONT_FAMILY = "'Noto Sans KR', 'Inter', 'Arial', sans-serif"


@dataclass(frozen=True)
class Theme:
    name: str
    bg_gradient: tuple[str, str, str]
    bg_fill: str
    text_primary: str
    text_secondary: str
    text_muted: str
    accent: str
    card_bg: str
    card_border: str
    header_bg: str


THEMES: dict[str, Theme] = {
    "dark": Theme(
        name="dark",
        bg_gradient=("#0d1b2a", "#1b263b", "#415a77"),
        bg_fill="#1b263b",
        text_primary="#ffffff",
        text_secondary="#e0e0e0",
        text_muted="#9e9e9e",
        accent="#4CAF50",
        card_bg="#1b263b",
        card_border="#415a77",
        header_bg="#0d1b2a",
    ),
    "light": Theme(
        name="light",
        bg_gradient=("#f8f9fa", "#e9ecef", "#dee2e6"),
        bg_fill="#f8f9fa",
        text_primary="#212529",
        text_secondary="#495057",
        text_muted="#6c757d",
        accent="#4CAF50",
        card_bg="#ffffff",
        card_border="#dee2e6",
        header_bg="#f8f9fa",
    ),
    "terminal": Theme(
        name="terminal",
        bg_gradient=("#0d1117", "#161b22", "#21262d"),
        bg_fill="#0d1117",
        text_primary="#e6edf3",
        text_secondary="#8b949e",
        text_muted="#6e7681",
        accent="#3fb950",
        card_bg="#161b22",
        card_border="#30363d",
        header_bg="#0d1117",
    ),
    "crisis": Theme(
        name="crisis",
        bg_gradient=("#1a0000", "#2d0000", "#400000"),
        bg_fill="#2d0000",
        text_primary="#ffffff",
        text_secondary="#ffcccc",
        text_muted="#cc9999",
        accent="#ff5252",
        card_bg="#2d0000",
        card_border="#400000",
        header_bg="#1a0000",
    ),
    "data": Theme(
        name="data",
        bg_gradient=("#f0fdf4", "#ecfdf5", "#d1fae5"),
        bg_fill="#f0fdf4",
        text_primary="#166534",
        text_secondary="#15803d",
        text_muted="#86efac",
        accent="#22c55e",
        card_bg="#ffffff",
        card_border="#bbf7d0",
        header_bg="#ecfdf5",
    ),
}


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
    def background(
        width: int,
        height: int,
        fill: str = "#f8f9fa",
        theme: str | None = None,
    ) -> str:
        """Full-canvas background rectangle."""
        if theme and theme in THEMES:
            fill = THEMES[theme].bg_fill
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
            f'font-family="{FONT_FAMILY}" font-size="{font_size}" '
            f'font-weight="bold" fill="{fill}" text-anchor="middle">'
            f"{escape_xml(text)}</text>\n"
        )
