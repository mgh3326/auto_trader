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

    def test_background_supports_theme(self) -> None:
        from blog.tools.components.base import SVGComponent

        result = SVGComponent.background(1400, 800, theme="dark")
        assert 'fill="#1b263b"' in result


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


class TestInfoCard:
    """Tests for InfoCard component."""

    def test_create_basic_card(self) -> None:
        from blog.tools.components.card import InfoCard

        svg = InfoCard.create(
            x=0,
            y=0,
            width=300,
            height=160,
            title="PER",
            value="30.38",
            color="#4CAF50",
        )
        assert "<rect" in svg
        assert "30.38" in svg
        assert "PER" in svg

    def test_card_with_description(self) -> None:
        from blog.tools.components.card import InfoCard

        svg = InfoCard.create(
            x=100,
            y=200,
            width=280,
            height=180,
            title="RSI",
            value="57.16",
            description="중립 구간",
            color="#2196F3",
        )
        assert "57.16" in svg
        assert "중립 구간" in svg
        assert "RSI" in svg

    def test_card_highlight_mode(self) -> None:
        from blog.tools.components.card import InfoCard

        svg_normal = InfoCard.create(
            x=0,
            y=0,
            width=300,
            height=160,
            title="Test",
            value="100",
        )
        svg_highlight = InfoCard.create(
            x=0,
            y=0,
            width=300,
            height=160,
            title="Test",
            value="100",
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
            x=0,
            y=0,
            width=300,
            height=160,
            title="A & B",
            value="<100>",
        )
        assert "&amp;" in svg
        assert "&lt;100&gt;" in svg

    def test_card_sub_items(self) -> None:
        """InfoCard with multiple sub-items (e.g., indicator details)."""
        from blog.tools.components.card import InfoCard

        svg = InfoCard.create(
            x=0,
            y=0,
            width=300,
            height=200,
            title="MACD",
            value="-527",
            sub_items=[("Signal", "매도 신호"), ("Histogram", "음수")],
        )
        assert "Signal" in svg
        assert "매도 신호" in svg


class TestBarChart:
    """Tests for BarChart component."""

    def test_vertical_bar_chart(self) -> None:
        from blog.tools.components.bar_chart import BarChart

        svg = BarChart.create(
            x=60,
            y=100,
            width=600,
            height=300,
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
            x=60,
            y=100,
            width=500,
            height=200,
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
            x=0,
            y=0,
            width=400,
            height=200,
            data=[("A", 100, "#ccc"), ("B", 200, "#ddd")],
            direction="vertical",
            show_labels=True,
        )
        assert "A" in svg
        assert "B" in svg

    def test_bar_chart_with_title(self) -> None:
        from blog.tools.components.bar_chart import BarChart

        svg = BarChart.create(
            x=0,
            y=0,
            width=400,
            height=200,
            data=[("X", 50, "#aaa")],
            direction="vertical",
            chart_title="매출 추이",
        )
        assert "매출 추이" in svg

    def test_bar_chart_empty_data(self) -> None:
        from blog.tools.components.bar_chart import BarChart

        svg = BarChart.create(
            x=0,
            y=0,
            width=400,
            height=200,
            data=[],
            direction="vertical",
        )
        # Should return valid SVG fragment (just the axes/frame, no bars)
        assert isinstance(svg, str)


class TestComparisonTable:
    """Tests for ComparisonTable component."""

    def test_basic_table(self) -> None:
        from blog.tools.components.table import ComparisonTable

        svg = ComparisonTable.create(
            x=60,
            y=100,
            width=800,
            height=300,
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
            x=0,
            y=0,
            width=600,
            height=200,
            headers=["Name", "Value"],
            rows=[["A", "1"], ["B", "2"]],
            highlight_row=0,
        )
        # First data row should have a highlight background
        assert svg.count("<rect") >= 2  # At least header bg + highlight bg

    def test_table_with_title(self) -> None:
        from blog.tools.components.table import ComparisonTable

        svg = ComparisonTable.create(
            x=0,
            y=0,
            width=600,
            height=200,
            headers=["H1", "H2"],
            rows=[["a", "b"]],
            table_title="비교 테이블",
        )
        assert "비교 테이블" in svg


class TestEventTimeline:
    """Tests for EventTimeline component."""

    def test_basic_timeline(self) -> None:
        from blog.tools.components.timeline import EventTimeline

        svg = EventTimeline.create(
            x=60,
            y=100,
            width=800,
            height=200,
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


class TestFlowDiagram:
    """Tests for FlowDiagram component."""

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


class TestCodeBlock:
    """Tests for CodeBlock component."""

    def test_basic_code_block(self) -> None:
        from blog.tools.components.code_block import CodeBlock

        svg = CodeBlock.create(
            x=60,
            y=100,
            width=600,
            height=200,
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
            x=0,
            y=0,
            width=400,
            height=100,
            code="if a < b && c > d:",
        )
        assert "&lt;" in svg
        assert "&amp;" in svg
