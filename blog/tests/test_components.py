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
