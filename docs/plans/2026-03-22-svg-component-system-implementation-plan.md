# SVG Component System — Phase 1: Component Layer

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build reusable SVG components under `blog/tools/components/` and `blog/tools/stock/` that extract repeated SVG patterns from existing blog image scripts, culminating in a `StockAnalysisPreset` that generates a 5-image set from MCP data dictionaries.

**Architecture:** Every component exposes `create(**data) -> str` returning an SVG *fragment* (no `<svg>` wrapper). A base module provides color constants, number formatting, and SVG header/footer helpers. Stock-specific components compose generic components. A preset class wires MCP response dicts → stock components → complete SVG images → PNG via existing `SVGConverter`.

**Tech Stack:** Python 3.13+, pytest, Playwright (existing SVG→PNG pipeline in `blog/tools/svg_converter.py`)

---

## Pre-Implementation Context

**Existing Files to Know:**
- `blog/tools/image_generator.py` — `BlogImageGenerator` ABC (generates SVGs via `get_images()` → `generate_svgs()` → `convert_to_png()`). Contains `ThumbnailTemplate` class that will be moved.
- `blog/tools/svg_converter.py` — Playwright-based SVG→PNG converter. Embeds SVG in HTML with Noto Sans KR font. **Do not modify.**
- `blog/tools/__init__.py` — Exports `BlogImageGenerator` and `SVGConverter`. Will be updated.
- `blog/images/kis_trading_images.py` (642 lines) — Reference for FlowDiagram, table, dashboard patterns.
- `blog/images/python314_images.py` (198 lines) — Reference for timeline, bar chart patterns.
- `blog/images/openclaw_images.py` — Reference for architecture/auth-flow diagrams.
- `blog/images/mcp_server_images.py` — Reference for tool-categorization grids.

**Key Conventions (MUST follow):**
- SVG font: `font-family="Arial, sans-serif"` (Playwright applies Noto Sans KR at render time)
- Image sizes: thumbnails 1200×630, body images 1400×800 default
- XML escaping: `&amp;`, `&lt;`, `&gt;` in all user-facing text
- All components are **pure functions** — no I/O, no side effects, return `str`
- `blog/` is **not** a Python package (no `blog/__init__.py`). Scripts use `sys.path.insert(0, ...)`.

**What Does NOT Exist Yet:**
- `samsung_analysis_images.py` — described in spec but not created. The stock/ components produce equivalent output.
- `blog/tests/` directory — will be created as part of this plan.

---

## Task 1: Foundation — `base.py` (Colors, Formatting, SVG Helpers)

**Files:**
- Create: `blog/tools/components/__init__.py`
- Create: `blog/tools/components/base.py`
- Create: `blog/tests/__init__.py`
- Create: `blog/tests/test_components.py`

**Step 1: Create directory structure**

```bash
mkdir -p blog/tools/components blog/tools/stock blog/tools/presets blog/tests
touch blog/tools/components/__init__.py blog/tools/stock/__init__.py blog/tools/presets/__init__.py blog/tests/__init__.py
```

**Step 2: Write the failing tests**

```python
# blog/tests/test_components.py
"""Tests for SVG component system."""

import sys
from pathlib import Path

# Ensure blog package is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestColors:
    """Tests for Colors constants."""

    def test_signal_colors_defined(self) -> None:
        from blog.tools.components.base import Colors

        assert Colors.BULLISH == "#4CAF50"
        assert Colors.BEARISH == "#e74c3c"
        assert Colors.NEUTRAL == "#FF9800"

    def test_perspective_colors_defined(self) -> None:
        from blog.tools.components.base import Colors

        assert Colors.TECHNICAL == "#2196F3"
        assert Colors.FUNDAMENTAL == "#4CAF50"
        assert Colors.SUPPLY_DEMAND == "#FF9800"
        assert Colors.CONCLUSION == "#9C27B0"

    def test_bg_colors_defined(self) -> None:
        from blog.tools.components.base import Colors

        assert Colors.BG_LIGHT == "#f8f9fa"
        assert Colors.BG_CARD == "#ffffff"
        assert Colors.BORDER == "#dee2e6"


class TestFormatting:
    """Tests for number formatting utilities."""

    def test_format_price_krw(self) -> None:
        from blog.tools.components.base import format_price

        assert format_price(199800) == "199,800"
        assert format_price(0) == "0"
        assert format_price(1234567890) == "1,234,567,890"

    def test_format_price_usd(self) -> None:
        from blog.tools.components.base import format_price

        assert format_price(12.34, currency="USD") == "$12.34"
        assert format_price(1234.5, currency="USD") == "$1,234.50"

    def test_format_large_trillion(self) -> None:
        from blog.tools.components.base import format_large

        assert format_large(197_700_000_000_000) == "197.7조"
        assert format_large(32_700_000_000_000) == "32.7조"

    def test_format_large_billion(self) -> None:
        from blog.tools.components.base import format_large

        assert format_large(5_432_000_000_000) == "5.4조"
        assert format_large(800_000_000_000) == "8,000억"
        assert format_large(123_456_000_000) == "1,235억"

    def test_format_large_small_values(self) -> None:
        from blog.tools.components.base import format_large

        assert format_large(50_000_000) == "5,000만"
        assert format_large(1_234_567) == "123만"

    def test_format_pct_positive(self) -> None:
        from blog.tools.components.base import format_pct

        assert format_pct(0.4327) == "+43.3%"
        assert format_pct(0.05) == "+5.0%"

    def test_format_pct_negative(self) -> None:
        from blog.tools.components.base import format_pct

        assert format_pct(-0.099) == "-9.9%"
        assert format_pct(-0.5) == "-50.0%"

    def test_format_pct_zero(self) -> None:
        from blog.tools.components.base import format_pct

        assert format_pct(0.0) == "0.0%"


class TestSVGComponent:
    """Tests for SVGComponent base class."""

    def test_header_produces_valid_svg_opening(self) -> None:
        from blog.tools.components.base import SVGComponent

        result = SVGComponent.header(1400, 800)
        assert '<?xml version="1.0" encoding="UTF-8"?>' in result
        assert 'width="1400"' in result
        assert 'height="800"' in result
        assert "<defs>" in result
        assert "arrowhead" in result

    def test_footer_closes_svg(self) -> None:
        from blog.tools.components.base import SVGComponent

        assert SVGComponent.footer() == "</svg>"

    def test_background_rect(self) -> None:
        from blog.tools.components.base import SVGComponent

        result = SVGComponent.background(1400, 800)
        assert 'width="1400"' in result
        assert 'height="800"' in result
        assert 'fill="#f8f9fa"' in result

    def test_background_custom_color(self) -> None:
        from blog.tools.components.base import SVGComponent

        result = SVGComponent.background(1400, 800, fill="#ffffff")
        assert 'fill="#ffffff"' in result

    def test_title_text(self) -> None:
        from blog.tools.components.base import SVGComponent

        result = SVGComponent.title(1400, "테스트 제목", y=45)
        assert "테스트 제목" in result
        assert 'text-anchor="middle"' in result
        assert 'font-size="28"' in result

    def test_escape_xml(self) -> None:
        from blog.tools.components.base import escape_xml

        assert escape_xml("A & B") == "A &amp; B"
        assert escape_xml("a < b > c") == "a &lt; b &gt; c"
        assert escape_xml('say "hello"') == "say &quot;hello&quot;"
        assert escape_xml("normal text") == "normal text"
```

**Step 3: Run tests to verify they fail**

```bash
uv run python -m pytest blog/tests/test_components.py -v --no-header 2>&1 | head -40
```

Expected: `ModuleNotFoundError` or `ImportError` — `blog.tools.components.base` does not exist yet.

**Step 4: Implement `base.py`**

```python
# blog/tools/components/base.py
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
        return f"{sign}{int(abs_val / 100_000_000):,}억"
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
```

**Step 5: Run tests to verify they pass**

```bash
uv run python -m pytest blog/tests/test_components.py -v --no-header
```

Expected: All tests PASS.

**Step 6: Lint check**

```bash
uv run ruff check blog/tools/components/base.py blog/tests/test_components.py
uv run ruff format --check blog/tools/components/base.py blog/tests/test_components.py
```

**Step 7: Commit**

```bash
git add blog/tools/components/ blog/tools/stock/ blog/tools/presets/ blog/tests/
git commit -m "feat(blog): add SVG component foundation — Colors, formatting utils, SVGComponent base"
```

---

## Task 2: Move ThumbnailTemplate to `components/thumbnail.py`

**Files:**
- Create: `blog/tools/components/thumbnail.py`
- Modify: `blog/tools/image_generator.py` (remove class, add re-export)
- Modify: `blog/tests/test_components.py` (add thumbnail tests)

**Step 1: Write the failing test**

Add to `blog/tests/test_components.py`:

```python
class TestThumbnailTemplate:
    """Tests for ThumbnailTemplate component."""

    def test_import_from_components(self) -> None:
        """ThumbnailTemplate should be importable from components package."""
        from blog.tools.components.thumbnail import ThumbnailTemplate

        assert hasattr(ThumbnailTemplate, "create")

    def test_backward_compat_import(self) -> None:
        """Existing import path must still work."""
        from blog.tools.image_generator import ThumbnailTemplate

        assert hasattr(ThumbnailTemplate, "create")

    def test_create_basic_thumbnail(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(
            title_line1="테스트 제목",
            title_line2="부제목",
            subtitle="설명 텍스트",
        )
        assert "<svg" in svg
        assert "테스트 제목" in svg
        assert "부제목" in svg
        assert 'width="1200"' in svg
        assert 'height="630"' in svg

    def test_create_with_icons(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(
            title_line1="아이콘 테스트",
            icons=[("📈", "Stock", "#2196F3"), ("🤖", "AI", "#4CAF50")],
        )
        assert "📈" in svg
        assert "Stock" in svg
```

**Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest blog/tests/test_components.py::TestThumbnailTemplate -v --no-header
```

Expected: FAIL — `blog.tools.components.thumbnail` does not exist.

**Step 3: Create `thumbnail.py` — move ThumbnailTemplate class verbatim**

```python
# blog/tools/components/thumbnail.py
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
```

**Step 4: Update `image_generator.py` — remove class, add re-export**

Replace the `ThumbnailTemplate` class (lines 213–300) and the `if __name__` block with a re-export:

```python
# At the end of blog/tools/image_generator.py, replace lines 213-316 with:

# ========== 썸네일 템플릿 (moved to components/thumbnail.py) ==========
# Re-export for backward compatibility — existing scripts import from here
from blog.tools.components.thumbnail import ThumbnailTemplate  # noqa: F401, E402
```

Keep the `BlogImageGenerator` class and all SVG helper methods **unchanged** (lines 1–211).

**Step 5: Run tests to verify they pass**

```bash
uv run python -m pytest blog/tests/test_components.py -v --no-header
```

Expected: All tests PASS, including backward-compat import test.

**Step 6: Verify existing scripts still import correctly**

```bash
uv run python -c "from blog.tools.image_generator import BlogImageGenerator, ThumbnailTemplate; print('OK:', ThumbnailTemplate)"
uv run python -c "from blog.tools.components.thumbnail import ThumbnailTemplate; print('OK:', ThumbnailTemplate)"
```

Expected: Both print `OK: <class '...ThumbnailTemplate'>`.

**Step 7: Commit**

```bash
git add blog/tools/components/thumbnail.py blog/tools/image_generator.py blog/tests/test_components.py
git commit -m "refactor(blog): move ThumbnailTemplate to components/thumbnail.py with backward-compat re-export"
```

---

## Task 3: InfoCard Component (`card.py`)

**Files:**
- Create: `blog/tools/components/card.py`
- Modify: `blog/tests/test_components.py` (add InfoCard tests)

**Step 1: Write the failing tests**

Add to `blog/tests/test_components.py`:

```python
class TestInfoCard:
    """Tests for InfoCard component."""

    def test_create_basic_card(self) -> None:
        from blog.tools.components.card import InfoCard

        svg = InfoCard.create(
            x=0, y=0, width=300, height=160,
            title="PER", value="30.38",
            color="#4CAF50",
        )
        assert "<rect" in svg
        assert "30.38" in svg
        assert "PER" in svg

    def test_card_with_description(self) -> None:
        from blog.tools.components.card import InfoCard

        svg = InfoCard.create(
            x=100, y=200, width=280, height=180,
            title="RSI", value="57.16",
            description="중립 구간",
            color="#2196F3",
        )
        assert "57.16" in svg
        assert "중립 구간" in svg
        assert "RSI" in svg

    def test_card_highlight_mode(self) -> None:
        from blog.tools.components.card import InfoCard

        svg_normal = InfoCard.create(
            x=0, y=0, width=300, height=160,
            title="Test", value="100",
        )
        svg_highlight = InfoCard.create(
            x=0, y=0, width=300, height=160,
            title="Test", value="100",
            highlight=True,
        )
        # Highlighted card should have a thicker stroke or different style
        assert svg_normal != svg_highlight

    def test_card_position_offset(self) -> None:
        from blog.tools.components.card import InfoCard

        svg = InfoCard.create(x=500, y=300, width=200, height=150, title="T", value="V")
        # The outermost rect should be positioned at (500, 300)
        assert 'x="500"' in svg
        assert 'y="300"' in svg

    def test_card_escapes_xml(self) -> None:
        from blog.tools.components.card import InfoCard

        svg = InfoCard.create(
            x=0, y=0, width=300, height=160,
            title="A & B", value="<100>",
        )
        assert "&amp;" in svg
        assert "&lt;100&gt;" in svg

    def test_card_sub_items(self) -> None:
        """InfoCard with multiple sub-items (e.g., indicator details)."""
        from blog.tools.components.card import InfoCard

        svg = InfoCard.create(
            x=0, y=0, width=300, height=200,
            title="MACD", value="-527",
            sub_items=[("Signal", "매도 신호"), ("Histogram", "음수")],
        )
        assert "Signal" in svg
        assert "매도 신호" in svg
```

**Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest blog/tests/test_components.py::TestInfoCard -v --no-header
```

**Step 3: Implement `card.py`**

```python
# blog/tools/components/card.py
"""InfoCard SVG component.

A rectangular card with a colored header strip, large value display,
optional description, and optional sub-items. Used for KPI displays,
indicator summaries, and metric cards.

Layout:
    ┌─── [title] ─────────┐
    │                      │
    │      [value]         │
    │  [description]       │
    │  [sub_item_1]        │
    │  [sub_item_2]        │
    └──────────────────────┘
"""

from __future__ import annotations

from blog.tools.components.base import escape_xml


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
            f'font-family="Arial, sans-serif" font-size="14" font-weight="bold" '
            f'fill="#ffffff" text-anchor="middle">{safe_title}</text>'
        )

        # Value (large, centered)
        value_y = y + header_h + 35
        parts.append(
            f'    <text x="{x + width // 2}" y="{value_y}" '
            f'font-family="Arial, sans-serif" font-size="28" font-weight="bold" '
            f'fill="{color}" text-anchor="middle">{safe_value}</text>'
        )

        # Description
        current_y = value_y + 25
        if description:
            safe_desc = escape_xml(description)
            parts.append(
                f'    <text x="{x + width // 2}" y="{current_y}" '
                f'font-family="Arial, sans-serif" font-size="12" '
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
                    f'font-family="Arial, sans-serif" font-size="11" '
                    f'fill="#666666">{safe_label}: {safe_val}</text>'
                )
                current_y += 18

        return "\n".join(parts) + "\n"
```

**Step 4: Run tests to verify they pass**

```bash
uv run python -m pytest blog/tests/test_components.py::TestInfoCard -v --no-header
```

**Step 5: Lint**

```bash
uv run ruff check blog/tools/components/card.py && uv run ruff format --check blog/tools/components/card.py
```

**Step 6: Commit**

```bash
git add blog/tools/components/card.py blog/tests/test_components.py
git commit -m "feat(blog): add InfoCard SVG component — colored header card with value display"
```

---

## Task 4: BarChart Component (`bar_chart.py`)

**Files:**
- Create: `blog/tools/components/bar_chart.py`
- Modify: `blog/tests/test_components.py`

**Step 1: Write the failing tests**

```python
class TestBarChart:
    """Tests for BarChart component."""

    def test_vertical_bar_chart(self) -> None:
        from blog.tools.components.bar_chart import BarChart

        svg = BarChart.create(
            x=60, y=100, width=600, height=300,
            data=[
                ("2021", 51_633, "#4CAF50"),
                ("2022", 43_376, "#4CAF50"),
                ("2023", 6_567, "#e74c3c"),
            ],
            direction="vertical",
        )
        assert "<rect" in svg
        assert "2021" in svg
        assert "2023" in svg

    def test_horizontal_bar_chart(self) -> None:
        from blog.tools.components.bar_chart import BarChart

        svg = BarChart.create(
            x=60, y=100, width=500, height=200,
            data=[
                ("외국인", -15234, "#e74c3c"),
                ("기관", 8721, "#4CAF50"),
                ("개인", 6513, "#2196F3"),
            ],
            direction="horizontal",
        )
        assert "외국인" in svg
        assert "기관" in svg

    def test_bar_chart_with_labels(self) -> None:
        from blog.tools.components.bar_chart import BarChart

        svg = BarChart.create(
            x=0, y=0, width=400, height=200,
            data=[("A", 100, "#ccc"), ("B", 200, "#ddd")],
            direction="vertical",
            show_labels=True,
        )
        assert "A" in svg
        assert "B" in svg

    def test_bar_chart_with_title(self) -> None:
        from blog.tools.components.bar_chart import BarChart

        svg = BarChart.create(
            x=0, y=0, width=400, height=200,
            data=[("X", 50, "#aaa")],
            direction="vertical",
            chart_title="매출 추이",
        )
        assert "매출 추이" in svg

    def test_bar_chart_empty_data(self) -> None:
        from blog.tools.components.bar_chart import BarChart

        svg = BarChart.create(
            x=0, y=0, width=400, height=200,
            data=[],
            direction="vertical",
        )
        # Should return valid SVG fragment (just the axes/frame, no bars)
        assert isinstance(svg, str)
```

**Step 2: Run to verify failure, then implement.**

Implementation pattern: `create()` draws an axis frame, then iterates `data` to place `<rect>` bars proportional to `max(abs(v) for _, v, _ in data)`. Vertical bars grow upward from the x-axis; horizontal bars grow rightward from a center line (to show positive/negative). Labels go below (vertical) or to the left (horizontal).

**Step 3: Implement, test, lint, commit**

```bash
git commit -m "feat(blog): add BarChart SVG component — horizontal and vertical bar charts"
```

---

## Task 5: ComparisonTable Component (`table.py`)

**Files:**
- Create: `blog/tools/components/table.py`
- Modify: `blog/tests/test_components.py`

**Step 1: Write the failing tests**

```python
class TestComparisonTable:
    """Tests for ComparisonTable component."""

    def test_basic_table(self) -> None:
        from blog.tools.components.table import ComparisonTable

        svg = ComparisonTable.create(
            x=60, y=100, width=800, height=300,
            headers=["기업", "시총", "PER", "PBR"],
            rows=[
                ["삼성전자", "350조", "30.38", "1.82"],
                ["SK하이닉스", "140조", "25.12", "2.10"],
            ],
        )
        assert "기업" in svg
        assert "삼성전자" in svg
        assert "SK하이닉스" in svg
        assert "<line" in svg  # Row separators

    def test_table_with_highlight_row(self) -> None:
        from blog.tools.components.table import ComparisonTable

        svg = ComparisonTable.create(
            x=0, y=0, width=600, height=200,
            headers=["Name", "Value"],
            rows=[["A", "1"], ["B", "2"]],
            highlight_row=0,
        )
        # First data row should have a highlight background
        assert svg.count("<rect") >= 2  # At least header bg + highlight bg

    def test_table_with_title(self) -> None:
        from blog.tools.components.table import ComparisonTable

        svg = ComparisonTable.create(
            x=0, y=0, width=600, height=200,
            headers=["H1", "H2"],
            rows=[["a", "b"]],
            table_title="비교 테이블",
        )
        assert "비교 테이블" in svg
```

**Step 2–6: Implement, test, lint, commit**

Implementation: Draw header row with colored background, data rows with alternating light fill, grid lines between rows. `highlight_row` index gets a tinted background.

```bash
git commit -m "feat(blog): add ComparisonTable SVG component — header/row table with highlight"
```

---

## Task 6: EventTimeline Component (`timeline.py`)

**Files:**
- Create: `blog/tools/components/timeline.py`
- Modify: `blog/tests/test_components.py`

**Tests pattern:**

```python
class TestEventTimeline:
    def test_basic_timeline(self) -> None:
        from blog.tools.components.timeline import EventTimeline

        svg = EventTimeline.create(
            x=60, y=100, width=800, height=200,
            events=[
                ("2024.01", "52주 신고가", "73,400", "#4CAF50", "above"),
                ("2024.06", "실적 쇼크", "-35%", "#e74c3c", "below"),
                ("2024.11", "반등 시작", "53,800", "#2196F3", "above"),
            ],
        )
        assert "52주 신고가" in svg
        assert "실적 쇼크" in svg
        assert "<line" in svg  # Timeline axis
        assert "<circle" in svg  # Event markers
```

Implementation: Horizontal line with evenly-spaced circle markers. Labels appear above/below based on `position`. Vertical lines connect markers to the axis.

```bash
git commit -m "feat(blog): add EventTimeline SVG component — event markers on horizontal axis"
```

---

## Task 7: FlowDiagram Component (`flow_diagram.py`)

**Files:**
- Create: `blog/tools/components/flow_diagram.py`
- Modify: `blog/tests/test_components.py`

**Tests pattern:**

```python
class TestFlowDiagram:
    def test_basic_flow(self) -> None:
        from blog.tools.components.flow_diagram import FlowDiagram

        svg = FlowDiagram.create(
            nodes=[
                (100, 100, 200, 80, "FastAPI", "#2196F3"),
                (400, 100, 200, 80, "Celery", "#FF9800"),
                (700, 100, 200, 80, "Redis", "#F44336"),
            ],
            edges=[
                (0, 1, "task.delay()"),
                (1, 2, "broker"),
            ],
        )
        assert "FastAPI" in svg
        assert "Celery" in svg
        assert "task.delay()" in svg
        assert "arrowhead" in svg or "marker-end" in svg

    def test_flow_empty_edges(self) -> None:
        from blog.tools.components.flow_diagram import FlowDiagram

        svg = FlowDiagram.create(
            nodes=[(0, 0, 100, 50, "Solo", "#ccc")],
            edges=[],
        )
        assert "Solo" in svg
```

Implementation: Each node is `<rect>` + centered `<text>`. Edges draw `<line>` with arrow markers between node centers, with optional label at midpoint. This replaces the hand-coded architecture diagrams in `kis_trading_images.py` and `openclaw_images.py`.

```bash
git commit -m "feat(blog): add FlowDiagram SVG component — nodes with labeled arrow edges"
```

---

## Task 8: CodeBlock Component (`code_block.py`)

**Files:**
- Create: `blog/tools/components/code_block.py`
- Modify: `blog/tests/test_components.py`

**Tests pattern:**

```python
class TestCodeBlock:
    def test_basic_code_block(self) -> None:
        from blog.tools.components.code_block import CodeBlock

        svg = CodeBlock.create(
            x=60, y=100, width=600, height=200,
            code='def hello():\n    return "world"',
            language="python",
        )
        assert "def hello():" in svg
        assert 'return "world"' in svg or "return &quot;world&quot;" in svg
        # Should have a dark background
        assert "#" in svg

    def test_code_block_escapes_special_chars(self) -> None:
        from blog.tools.components.code_block import CodeBlock

        svg = CodeBlock.create(
            x=0, y=0, width=400, height=100,
            code='if a < b && c > d:',
        )
        assert "&lt;" in svg
        assert "&amp;" in svg
```

Implementation: Dark background `<rect>` + monospace `<text>` elements for each line. Line numbers optional. The `language` label appears in the top-right corner.

```bash
git commit -m "feat(blog): add CodeBlock SVG component — syntax-highlighted code snippet display"
```

---

## Task 9: Wire `components/__init__.py` Exports

**Files:**
- Modify: `blog/tools/components/__init__.py`
- Modify: `blog/tools/__init__.py`

**Step 1: Update `components/__init__.py`**

```python
# blog/tools/components/__init__.py
"""Reusable SVG building blocks.

All components return SVG fragment strings via static create() methods.
Compose fragments with SVGComponent.header() / .footer() for complete documents.
"""

from blog.tools.components.bar_chart import BarChart
from blog.tools.components.base import Colors, SVGComponent, escape_xml, format_large, format_pct, format_price
from blog.tools.components.card import InfoCard
from blog.tools.components.code_block import CodeBlock
from blog.tools.components.flow_diagram import FlowDiagram
from blog.tools.components.table import ComparisonTable
from blog.tools.components.thumbnail import ThumbnailTemplate
from blog.tools.components.timeline import EventTimeline

__all__ = [
    "BarChart",
    "CodeBlock",
    "Colors",
    "ComparisonTable",
    "EventTimeline",
    "FlowDiagram",
    "InfoCard",
    "SVGComponent",
    "ThumbnailTemplate",
    "escape_xml",
    "format_large",
    "format_pct",
    "format_price",
]
```

**Step 2: Update `blog/tools/__init__.py`**

```python
# blog/tools/__init__.py
"""블로그 이미지 생성 도구 모음

사용법:
    from blog.tools import SVGConverter, BlogImageGenerator
    from blog.tools.components import InfoCard, BarChart, Colors
"""

from blog.tools.image_generator import BlogImageGenerator
from blog.tools.svg_converter import SVGConverter

__all__ = ["SVGConverter", "BlogImageGenerator"]
```

**Step 3: Verify all imports work**

```bash
uv run python -c "
from blog.tools.components import InfoCard, BarChart, ComparisonTable, EventTimeline, FlowDiagram, CodeBlock, ThumbnailTemplate, Colors, SVGComponent
print('All components imported OK')
"
```

**Step 4: Commit**

```bash
git add blog/tools/components/__init__.py blog/tools/__init__.py
git commit -m "feat(blog): wire components package exports"
```

---

## Task 10: IndicatorDashboard (`stock/indicator_dashboard.py`)

**Files:**
- Create: `blog/tools/stock/indicator_dashboard.py`
- Create: `blog/tests/test_stock_components.py`

**Step 1: Write the failing tests**

```python
# blog/tests/test_stock_components.py
"""Tests for stock-specific SVG components."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


SAMPLE_INDICATORS = {
    "rsi14": 57.16,
    "macd_histogram": -527,
    "macd_signal": "매도 신호",
    "adx": 16.37,
    "plus_di": 22.5,
    "minus_di": 18.3,
    "stoch_rsi_k": 0.78,
    "stoch_rsi_d": 0.65,
}


class TestIndicatorDashboard:
    def test_create_dashboard(self) -> None:
        from blog.tools.stock.indicator_dashboard import IndicatorDashboard

        svg = IndicatorDashboard.create(
            x=60, y=95, width=600, height=350,
            indicators=SAMPLE_INDICATORS,
        )
        assert "RSI" in svg
        assert "MACD" in svg
        assert "ADX" in svg
        assert "StochRSI" in svg

    def test_rsi_zone_label(self) -> None:
        from blog.tools.stock.indicator_dashboard import IndicatorDashboard

        svg = IndicatorDashboard.create(
            x=0, y=0, width=600, height=350,
            indicators={**SAMPLE_INDICATORS, "rsi14": 75.0},
        )
        assert "과매수" in svg

    def test_missing_indicators_handled(self) -> None:
        from blog.tools.stock.indicator_dashboard import IndicatorDashboard

        svg = IndicatorDashboard.create(
            x=0, y=0, width=600, height=350,
            indicators={"rsi14": 50.0},  # Minimal data
        )
        assert "RSI" in svg
        assert isinstance(svg, str)
```

**Step 2–6: Implement, test, lint, commit**

Implementation: 2×2 grid of InfoCard instances. Each cell displays one indicator with appropriate zone labels (RSI: 과매도/중립/과매수, MACD: 매수/매도 신호, ADX: 추세 강도, StochRSI: K/D values). Uses `InfoCard.create()` internally.

```bash
git commit -m "feat(blog): add IndicatorDashboard — RSI/MACD/ADX/StochRSI 4-panel grid"
```

---

## Task 11: PriceChart (`stock/price_chart.py`)

**Files:**
- Create: `blog/tools/stock/price_chart.py`
- Modify: `blog/tests/test_stock_components.py`

**Tests pattern:**

```python
SAMPLE_OHLCV = [
    {"date": "2024-01-02", "close": 73400},
    {"date": "2024-01-03", "close": 72800},
    {"date": "2024-01-04", "close": 74100},
    {"date": "2024-01-05", "close": 73600},
    {"date": "2024-01-08", "close": 72200},
]


class TestPriceChart:
    def test_basic_price_line(self) -> None:
        from blog.tools.stock.price_chart import PriceChart

        svg = PriceChart.create(
            x=60, y=95, width=800, height=350,
            ohlcv=SAMPLE_OHLCV,
        )
        assert "<polyline" in svg or "<path" in svg  # Line chart
        assert isinstance(svg, str)

    def test_with_bollinger_bands(self) -> None:
        from blog.tools.stock.price_chart import PriceChart

        svg = PriceChart.create(
            x=60, y=95, width=800, height=350,
            ohlcv=SAMPLE_OHLCV,
            bollinger={"upper": [75000, 74500, 75200, 74800, 73500],
                       "lower": [71000, 71200, 72800, 72400, 70900]},
        )
        assert "polygon" in svg or "polyline" in svg  # Band area

    def test_with_ema_lines(self) -> None:
        from blog.tools.stock.price_chart import PriceChart

        svg = PriceChart.create(
            x=60, y=95, width=800, height=350,
            ohlcv=SAMPLE_OHLCV,
            ema_values={"ema20": [73000, 72900, 73500, 73200, 72800]},
        )
        assert isinstance(svg, str)

    def test_auto_scales_y_axis(self) -> None:
        from blog.tools.stock.price_chart import PriceChart

        svg = PriceChart.create(
            x=0, y=0, width=600, height=300,
            ohlcv=[
                {"date": "2024-01-02", "close": 100},
                {"date": "2024-01-03", "close": 200},
            ],
        )
        # Y axis labels should be present
        assert isinstance(svg, str)
```

Implementation: Maps OHLCV close prices to x/y coordinates within the chart area. Draws `<polyline>` for price, optional `<polygon>` fill for Bollinger band area, and dashed `<polyline>` for EMA lines. Auto-scales Y axis from min/max prices with 5% padding.

```bash
git commit -m "feat(blog): add PriceChart — OHLCV line chart with Bollinger bands and EMA"
```

---

## Task 12: SupportResistance (`stock/support_resistance.py`)

Tests verify horizontal bars at support/resistance levels with price labels and zone indicators. Implementation: Draws horizontal lines at each level within a mini price-axis, with green (support) and red (resistance) coloring.

```bash
git commit -m "feat(blog): add SupportResistance — horizontal support/resistance level map"
```

---

## Task 13: ValuationCards (`stock/valuation_cards.py`)

Tests verify 4-card grid (PER/PBR/ROE/Consensus) using InfoCard internally. Takes a `valuation` dict from MCP `get_valuation` response.

```bash
git commit -m "feat(blog): add ValuationCards — PER/PBR/ROE/Consensus 4-panel card grid"
```

---

## Task 14: EarningsChart (`stock/earnings_chart.py`)

Tests verify vertical bar chart for annual earnings + horizontal bars for quarterly operating margins. Composes BarChart internally with earnings-specific formatting (`format_large` for values).

```bash
git commit -m "feat(blog): add EarningsChart — annual earnings bars + quarterly margin chart"
```

---

## Task 15: InvestorFlow (`stock/investor_flow.py`)

Tests verify horizontal bar chart for foreign/institutional net trading with warning indicators for consecutive selling days. Composes BarChart internally.

```bash
git commit -m "feat(blog): add InvestorFlow — foreign/institutional net trading flow bars"
```

---

## Task 16: OpinionTable (`stock/opinion_table.py`)

Tests verify analyst opinion table (firm, rating, target price) with consensus summary row. Composes ComparisonTable internally.

```bash
git commit -m "feat(blog): add OpinionTable — analyst opinions table with consensus summary"
```

---

## Task 17: ConclusionCard (`stock/conclusion_card.py`)

Tests verify multi-perspective summary card showing Technical/Fundamental/Supply-Demand/Overall verdicts with signal indicators (BULLISH/BEARISH/NEUTRAL colors).

```bash
git commit -m "feat(blog): add ConclusionCard — multi-perspective analysis summary card"
```

---

## Task 18: Wire `stock/__init__.py` Exports

**Files:**
- Modify: `blog/tools/stock/__init__.py`

```python
# blog/tools/stock/__init__.py
"""Stock analysis specific SVG components.

These components consume MCP API response dicts and produce
SVG fragments for stock analysis images.
"""

from blog.tools.stock.conclusion_card import ConclusionCard
from blog.tools.stock.earnings_chart import EarningsChart
from blog.tools.stock.indicator_dashboard import IndicatorDashboard
from blog.tools.stock.investor_flow import InvestorFlow
from blog.tools.stock.opinion_table import OpinionTable
from blog.tools.stock.price_chart import PriceChart
from blog.tools.stock.support_resistance import SupportResistance
from blog.tools.stock.valuation_cards import ValuationCards

__all__ = [
    "ConclusionCard",
    "EarningsChart",
    "IndicatorDashboard",
    "InvestorFlow",
    "OpinionTable",
    "PriceChart",
    "SupportResistance",
    "ValuationCards",
]
```

```bash
git add blog/tools/stock/__init__.py
git commit -m "feat(blog): wire stock component package exports"
```

---

## Task 19: StockAnalysisPreset (`presets/stock_analysis.py`)

**Files:**
- Create: `blog/tools/presets/stock_analysis.py`
- Create: `blog/tests/test_stock_preset.py`

**Step 1: Write the failing tests**

```python
# blog/tests/test_stock_preset.py
"""Integration tests for StockAnalysisPreset."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import tempfile

SAMPLE_DATA = {
    "company_profile": {"name": "삼성전자", "symbol": "005930", "sector": "반도체"},
    "indicators": {
        "rsi14": 57.16,
        "macd_histogram": -527,
        "macd_signal": "매도 신호",
        "adx": 16.37,
        "plus_di": 22.5,
        "minus_di": 18.3,
        "stoch_rsi_k": 0.78,
        "stoch_rsi_d": 0.65,
    },
    "valuation": {
        "per": 30.38,
        "pbr": 1.82,
        "roe": 6.01,
        "consensus_target": 85000,
        "current_price": 65800,
    },
    "financials": {
        "annual_earnings": [
            {"year": "2021", "operating_income": 51_633_000_000_000},
            {"year": "2022", "operating_income": 43_376_000_000_000},
            {"year": "2023", "operating_income": 6_567_000_000_000},
            {"year": "2024E", "operating_income": 32_700_000_000_000},
        ],
        "quarterly_margins": [
            {"quarter": "Q1", "margin": 0.05},
            {"quarter": "Q2", "margin": 0.08},
            {"quarter": "Q3", "margin": 0.12},
            {"quarter": "Q4E", "margin": 0.15},
        ],
    },
    "investor_trends": {
        "foreign_net": -15234,
        "institution_net": 8721,
        "individual_net": 6513,
        "foreign_consecutive_sell_days": 5,
    },
    "investment_opinions": {
        "opinions": [
            {"firm": "삼성증권", "rating": "매수", "target": 90000},
            {"firm": "NH투자", "rating": "매수", "target": 85000},
            {"firm": "미래에셋", "rating": "중립", "target": 70000},
        ],
        "consensus": {"rating": "매수", "avg_target": 82000},
    },
    "support_resistance": {
        "supports": [62000, 58000, 55000],
        "resistances": [68000, 72000, 75000],
        "current_price": 65800,
    },
    "sector_peers": [
        {"name": "삼성전자", "market_cap": "350조", "per": "30.38"},
        {"name": "SK하이닉스", "market_cap": "140조", "per": "25.12"},
    ],
    "ohlcv": [
        {"date": f"2024-01-{d:02d}", "open": 65000 + d * 100, "high": 66000 + d * 100,
         "low": 64000 + d * 100, "close": 65500 + d * 100, "volume": 10000000 + d * 100000}
        for d in range(2, 22)
    ],
}


class TestStockAnalysisPreset:
    def test_generate_returns_five_paths(self) -> None:
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        with tempfile.TemporaryDirectory() as tmpdir:
            preset = StockAnalysisPreset("005930", SAMPLE_DATA, output_dir=Path(tmpdir))
            paths = preset.generate_svgs()
            assert len(paths) == 5
            assert all(p.suffix == ".svg" for p in paths)
            assert all(p.exists() for p in paths)

    def test_generated_filenames(self) -> None:
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        with tempfile.TemporaryDirectory() as tmpdir:
            preset = StockAnalysisPreset("005930", SAMPLE_DATA, output_dir=Path(tmpdir))
            paths = preset.generate_svgs()
            names = {p.stem for p in paths}
            expected = {
                "005930_thumbnail",
                "005930_technical",
                "005930_fundamental",
                "005930_supply_demand",
                "005930_conclusion",
            }
            assert names == expected

    def test_each_svg_is_valid(self) -> None:
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        with tempfile.TemporaryDirectory() as tmpdir:
            preset = StockAnalysisPreset("005930", SAMPLE_DATA, output_dir=Path(tmpdir))
            paths = preset.generate_svgs()
            for p in paths:
                content = p.read_text()
                assert content.startswith("<?xml")
                assert "<svg" in content
                assert "</svg>" in content

    def test_thumbnail_contains_company_name(self) -> None:
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        with tempfile.TemporaryDirectory() as tmpdir:
            preset = StockAnalysisPreset("005930", SAMPLE_DATA, output_dir=Path(tmpdir))
            paths = preset.generate_svgs()
            thumb = next(p for p in paths if "thumbnail" in p.stem)
            assert "삼성전자" in thumb.read_text()

    def test_technical_contains_indicators(self) -> None:
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        with tempfile.TemporaryDirectory() as tmpdir:
            preset = StockAnalysisPreset("005930", SAMPLE_DATA, output_dir=Path(tmpdir))
            paths = preset.generate_svgs()
            tech = next(p for p in paths if "technical" in p.stem)
            content = tech.read_text()
            assert "RSI" in content
            assert "MACD" in content
```

**Step 2: Implement `stock_analysis.py`**

```python
# blog/tools/presets/stock_analysis.py
"""Stock analysis image set preset.

Generates 5 SVG images from MCP API response data:
1. Thumbnail (1200×630) — company name + key metrics
2. Technical (1400×800) — price chart + indicator dashboard + support/resistance
3. Fundamental (1400×800) — valuation cards + earnings chart + sector comparison
4. Supply & Demand (1400×800) — investor flow + opinion table + timeline
5. Conclusion (1400×800) — multi-perspective summary card
"""

from __future__ import annotations

from pathlib import Path

from blog.tools.components import SVGComponent, ThumbnailTemplate
from blog.tools.stock import (
    ConclusionCard,
    EarningsChart,
    IndicatorDashboard,
    InvestorFlow,
    OpinionTable,
    PriceChart,
    SupportResistance,
    ValuationCards,
)


class StockAnalysisPreset:
    """MCP data → 5-image stock analysis set."""

    def __init__(
        self,
        symbol: str,
        data: dict,
        output_dir: Path | None = None,
    ) -> None:
        self.symbol = symbol
        self.data = data
        self.output_dir = output_dir or Path(__file__).parent.parent.parent / "images"
        self.output_dir.mkdir(exist_ok=True)

        self.company_name = data.get("company_profile", {}).get("name", symbol)

    def generate_svgs(self) -> list[Path]:
        """Generate 5 SVG files and return their paths."""
        images = [
            ("thumbnail", 1200, 630, self._create_thumbnail),
            ("technical", 1400, 800, self._create_technical),
            ("fundamental", 1400, 800, self._create_fundamental),
            ("supply_demand", 1400, 800, self._create_supply_demand),
            ("conclusion", 1400, 800, self._create_conclusion),
        ]

        paths: list[Path] = []
        for name, _w, _h, create_fn in images:
            svg_content = create_fn()
            file_path = self.output_dir / f"{self.symbol}_{name}.svg"
            file_path.write_text(svg_content, encoding="utf-8")
            paths.append(file_path)

        return paths

    async def generate_pngs(self) -> list[Path]:
        """Generate SVGs then convert to PNGs via SVGConverter."""
        from blog.tools.svg_converter import SVGConverter

        svg_paths = self.generate_svgs()
        converter = SVGConverter(self.output_dir)

        files = []
        for svg_path in svg_paths:
            png_name = svg_path.stem + ".png"
            width = 1200 if "thumbnail" in svg_path.stem else 1400
            files.append((svg_path.name, png_name, width))

        return await converter.convert_all(files)

    def _create_thumbnail(self) -> str:
        profile = self.data.get("company_profile", {})
        valuation = self.data.get("valuation", {})

        sector = profile.get("sector", "")
        current_price = valuation.get("current_price", 0)

        icons = [
            ("📊", "기술적 분석", "#2196F3"),
            ("💰", "펀더멘탈", "#4CAF50"),
            ("📈", "수급 분석", "#FF9800"),
            ("🎯", "종합 결론", "#9C27B0"),
        ]

        from blog.tools.components.base import format_price

        return ThumbnailTemplate.create(
            title_line1=f"{self.company_name} 종합 분석",
            title_line2=f"현재가 {format_price(current_price)}원",
            subtitle=f"{sector} | {self.symbol}",
            icons=icons,
            tech_stack="기술적 분석 • 펀더멘탈 • 수급 분석 • AI 종합 판단",
            accent_color="#4CAF50",
        )

    def _create_technical(self) -> str:
        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800)
        svg += SVGComponent.title(1400, f"{self.company_name} — 기술적 분석")

        # Price chart (left, ~60% width)
        ohlcv = self.data.get("ohlcv", [])
        if ohlcv:
            svg += PriceChart.create(x=60, y=95, width=780, height=350, ohlcv=ohlcv)

        # Indicator dashboard (right, ~35% width)
        indicators = self.data.get("indicators", {})
        svg += IndicatorDashboard.create(x=880, y=95, width=480, height=350, indicators=indicators)

        # Support/resistance (bottom)
        sr = self.data.get("support_resistance", {})
        if sr:
            svg += SupportResistance.create(x=60, y=480, width=1300, height=280, **sr)

        svg += SVGComponent.footer()
        return svg

    def _create_fundamental(self) -> str:
        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800)
        svg += SVGComponent.title(1400, f"{self.company_name} — 펀더멘탈 분석")

        # Valuation cards (top)
        valuation = self.data.get("valuation", {})
        svg += ValuationCards.create(x=60, y=95, width=1300, height=180, valuation=valuation)

        # Earnings chart (middle)
        financials = self.data.get("financials", {})
        if financials:
            svg += EarningsChart.create(x=60, y=310, width=1300, height=300, financials=financials)

        svg += SVGComponent.footer()
        return svg

    def _create_supply_demand(self) -> str:
        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800)
        svg += SVGComponent.title(1400, f"{self.company_name} — 수급 분석")

        # Investor flow (top)
        investor = self.data.get("investor_trends", {})
        svg += InvestorFlow.create(x=60, y=95, width=1300, height=250, investor_trends=investor)

        # Opinion table (bottom)
        opinions = self.data.get("investment_opinions", {})
        svg += OpinionTable.create(x=60, y=380, width=1300, height=380, opinions=opinions)

        svg += SVGComponent.footer()
        return svg

    def _create_conclusion(self) -> str:
        svg = SVGComponent.header(1400, 800)
        svg += SVGComponent.background(1400, 800)
        svg += SVGComponent.title(1400, f"{self.company_name} — 종합 분석 결론")

        svg += ConclusionCard.create(
            x=60, y=95, width=1300, height=660,
            data=self.data,
            company_name=self.company_name,
        )

        svg += SVGComponent.footer()
        return svg
```

**Step 3: Run tests**

```bash
uv run python -m pytest blog/tests/test_stock_preset.py -v --no-header
```

**Step 4: Commit**

```bash
git add blog/tools/presets/ blog/tests/test_stock_preset.py
git commit -m "feat(blog): add StockAnalysisPreset — MCP data to 5-image SVG set pipeline"
```

---

## Task 20: Backward Compatibility Verification

**Files:** None modified — read-only verification.

**Step 1: Verify all 4 existing image scripts can be imported**

```bash
uv run python -c "
import sys; sys.path.insert(0, '.')
from blog.images.kis_trading_images import KISTradingImages
from blog.images.python314_images import Python314Images
from blog.images.openclaw_images import OpenClawImages
from blog.images.mcp_server_images import MCPServerImages
print('All 4 image scripts import OK')
"
```

**Step 2: Verify `blog.tools` public API unchanged**

```bash
uv run python -c "
import sys; sys.path.insert(0, '.')
from blog.tools import BlogImageGenerator, SVGConverter
from blog.tools.image_generator import BlogImageGenerator, ThumbnailTemplate
print('Public API OK')
print('BlogImageGenerator:', BlogImageGenerator)
print('ThumbnailTemplate:', ThumbnailTemplate)
print('SVGConverter:', SVGConverter)
"
```

**Step 3: Run full blog test suite**

```bash
uv run python -m pytest blog/tests/ -v --no-header
```

**Step 4: Optionally generate one existing script to verify end-to-end**

```bash
uv run python blog/images/python314_images.py
```

Verify: 3 SVGs + 3 PNGs generated in `blog/images/` without errors.

**Step 5: Final commit if any fixups were needed**

```bash
git add -A && git commit -m "fix(blog): backward compatibility fixups for SVG component migration" || echo "Nothing to commit — all clean"
```

---

## Summary

| Task | Component | Dependencies |
|------|-----------|-------------|
| 1 | `base.py` — Colors, formatting, SVGComponent | None |
| 2 | `thumbnail.py` — ThumbnailTemplate migration | Task 1 |
| 3 | `card.py` — InfoCard | Task 1 |
| 4 | `bar_chart.py` — BarChart | Task 1 |
| 5 | `table.py` — ComparisonTable | Task 1 |
| 6 | `timeline.py` — EventTimeline | Task 1 |
| 7 | `flow_diagram.py` — FlowDiagram | Task 1 |
| 8 | `code_block.py` — CodeBlock | Task 1 |
| 9 | `components/__init__.py` exports | Tasks 1–8 |
| 10 | `indicator_dashboard.py` | Task 3 (InfoCard) |
| 11 | `price_chart.py` | Task 1 |
| 12 | `support_resistance.py` | Task 1 |
| 13 | `valuation_cards.py` | Task 3 (InfoCard) |
| 14 | `earnings_chart.py` | Task 4 (BarChart) |
| 15 | `investor_flow.py` | Task 4 (BarChart) |
| 16 | `opinion_table.py` | Task 5 (Table) |
| 17 | `conclusion_card.py` | Task 3 (InfoCard) |
| 18 | `stock/__init__.py` exports | Tasks 10–17 |
| 19 | `StockAnalysisPreset` | Tasks 9, 18 |
| 20 | Backward compat verification | Task 19 |

**Parallelizable groups:**
- Tasks 3–8 are independent (all depend only on Task 1)
- Tasks 10–17 can be parallelized after their generic component dependency is done
