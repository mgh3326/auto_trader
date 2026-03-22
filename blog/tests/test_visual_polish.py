"""Tests for Phase 3 visual polish features.

Tests for fonts, themes, icons, and background patterns.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestFontFamilyConsistency:
    """Tests for consistent font usage across generated images."""

    def test_font_family_constant_exists(self) -> None:
        from blog.tools.components.base import FONT_FAMILY

        assert "Noto Sans KR" in FONT_FAMILY
        assert "Inter" in FONT_FAMILY

    def test_font_family_exported_from_package(self) -> None:
        from blog.tools.components import FONT_FAMILY

        assert "Noto Sans KR" in FONT_FAMILY

    def test_font_family_consistency_on_target_generators(self) -> None:
        """Verify target generators use FONT_FAMILY, not hardcoded Arial."""
        import tempfile
        from pathlib import Path

        from blog.images.kis_trading_images import KISTradingImages
        from blog.images.mcp_server_images import MCPServerImages
        from blog.images.openclaw_images import OpenClawImages
        from blog.images.python314_images import Python314Images
        from blog.tools.components.base import FONT_FAMILY

        for cls, prefix in [
            (KISTradingImages, "kis_trading"),
            (MCPServerImages, "mcp_server"),
            (OpenClawImages, "openclaw"),
            (Python314Images, "python314"),
        ]:
            with tempfile.TemporaryDirectory() as tmpdir:
                gen = cls(prefix, images_dir=Path(tmpdir))
                for path in gen.generate_svgs():
                    content = path.read_text(encoding="utf-8")
                    assert '"Arial, sans-serif"' not in content
                    assert FONT_FAMILY in content


class TestIconSystem:
    """Tests for Lucide-based icon system."""

    def test_icon_render(self) -> None:
        from blog.tools.components.icons import Icon

        svg = Icon.render("chart-line", 0, 0, size=48, color="#ff0000")
        assert 'stroke="#ff0000"' in svg
        assert "scale(2" in svg

    def test_icon_unknown_returns_empty(self) -> None:
        from blog.tools.components.icons import Icon

        assert Icon.render("nonexistent", 0, 0) == ""

    def test_icon_render_with_path_data(self) -> None:
        from blog.tools.components.icons import Icon

        svg = Icon.render("chart-line", 100, 200, size=64, color="#2196F3")
        assert "<path" in svg
        assert 'd="' in svg
        assert 'transform="translate(100, 200)' in svg

    def test_icon_available_lists_expected_keys(self) -> None:
        from blog.tools.components.icons import Icon

        names = Icon.available()
        assert "chart-line" in names
        assert "database" in names
        assert "refresh-cw" in names

    def test_chart_line_and_chart_bar_have_distinct_paths(self) -> None:
        from blog.tools.components.icons import ICON_PATHS

        assert ICON_PATHS["chart-line"] != ICON_PATHS["chart-bar"]

    def test_icon_render_supports_multiple_paths(self) -> None:
        from blog.tools.components.icons import Icon

        svg = Icon.render("database", 0, 0)
        assert svg.count("<path") >= 1


class TestThumbnailWithIcons:
    """Tests for thumbnail template with Lucide icons."""

    def test_thumbnail_with_lucide_icons(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(
            title_line1="Test",
            icons=[("chart-line", "차트", "#2196F3"), ("database", "DB", "#4CAF50")],
        )
        assert "<path" in svg
        assert "차트" in svg
        assert "DB" in svg

    def test_thumbnail_with_mixed_icons_and_emoji(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(
            title_line1="Test",
            icons=[("chart-line", "차트", "#2196F3"), ("📊", "이전 아이콘", "#666")],
        )
        assert "<path" in svg
        assert "차트" in svg


class TestThemeSystem:
    """Tests for theme definitions."""

    def test_theme_dark_exists(self) -> None:
        from blog.tools.components.base import THEMES

        assert THEMES["dark"].bg_fill == "#1b263b"
        assert THEMES["dark"].text_primary == "#e0e1dd"
        assert THEMES["dark"].text_secondary == "#778da9"
        assert THEMES["dark"].text_muted == "#415a77"
        assert THEMES["dark"].accent == "#00b4d8"
        assert THEMES["dark"].card_bg == "#0d1b2a"
        assert THEMES["dark"].header_bg == "#1b263b"

    def test_theme_light_exists(self) -> None:
        from blog.tools.components.base import THEMES

        assert THEMES["light"].bg_fill == "#f8f9fa"
        assert THEMES["light"].bg_gradient == ("#ffffff", "#f1f5f9", "#e2e8f0")
        assert THEMES["light"].text_primary == "#1e293b"
        assert THEMES["light"].text_secondary == "#475569"
        assert THEMES["light"].text_muted == "#94a3b8"
        assert THEMES["light"].accent == "#3b82f6"
        assert THEMES["light"].header_bg == "#f1f5f9"

    def test_theme_terminal_exists(self) -> None:
        from blog.tools.components.base import THEMES

        assert THEMES["terminal"].bg_gradient == ("#0c0c0c", "#111111", "#1a1a1a")
        assert THEMES["terminal"].bg_fill == "#0c0c0c"
        assert THEMES["terminal"].text_primary == "#00ff00"
        assert THEMES["terminal"].text_secondary == "#00cc00"
        assert THEMES["terminal"].text_muted == "#006600"
        assert THEMES["terminal"].accent == "#00ff00"
        assert THEMES["terminal"].card_bg == "#111111"
        assert THEMES["terminal"].header_bg == "#1a1a1a"

    def test_theme_crisis_exists(self) -> None:
        from blog.tools.components.base import THEMES

        assert THEMES["crisis"].bg_gradient == ("#1a0000", "#2d0000", "#450a0a")
        assert THEMES["crisis"].bg_fill == "#1a0000"
        assert THEMES["crisis"].text_primary == "#fecaca"
        assert THEMES["crisis"].text_secondary == "#f87171"
        assert THEMES["crisis"].text_muted == "#991b1b"
        assert THEMES["crisis"].accent == "#ef4444"
        assert THEMES["crisis"].card_bg == "#2d0000"
        assert THEMES["crisis"].card_border == "#991b1b"
        assert THEMES["crisis"].header_bg == "#450a0a"

    def test_theme_data_exists(self) -> None:
        from blog.tools.components.base import THEMES

        assert THEMES["data"].bg_fill == "#f0fdf4"
        assert THEMES["data"].bg_gradient == ("#f0fdf4", "#ecfdf5", "#d1fae5")
        assert THEMES["data"].text_primary == "#166534"
        assert THEMES["data"].text_secondary == "#15803d"
        assert THEMES["data"].text_muted == "#86efac"
        assert THEMES["data"].accent == "#22c55e"
        assert THEMES["data"].card_bg == "#ffffff"
        assert THEMES["data"].card_border == "#bbf7d0"
        assert THEMES["data"].header_bg == "#ecfdf5"


class TestThumbnailWithThemes:
    """Tests for thumbnail with theme support."""

    def test_thumbnail_with_theme(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(title_line1="Test", theme="crisis")
        assert "#1a0000" in svg or "#2d0000" in svg

    def test_thumbnail_dark_theme(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(title_line1="Test", theme="dark")
        assert "#1b263b" in svg

    def test_thumbnail_theme_applies_default_accent_when_not_overridden(self) -> None:
        from blog.tools.components.base import THEMES
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(title_line1="Test", title_line2="Accent", theme="terminal")
        assert THEMES["terminal"].accent in svg

    def test_thumbnail_explicit_accent_overrides_theme_accent(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(
            title_line1="Test",
            title_line2="Accent",
            theme="terminal",
            accent_color="#ff00ff",
        )
        assert "#ff00ff" in svg


class TestThumbnailBackgroundPatterns:
    """Tests for background pattern support."""

    def test_thumbnail_candlestick_pattern(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(title_line1="Test", bg_pattern="candlestick")
        assert 'opacity="0.' in svg
        assert "<rect" in svg

    def test_thumbnail_grid_pattern(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(title_line1="Test", bg_pattern="grid")
        assert 'opacity="0.' in svg

    def test_thumbnail_dots_pattern(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(title_line1="Test", bg_pattern="dots")
        assert 'opacity="0.' in svg

    def test_thumbnail_wave_pattern(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        svg = ThumbnailTemplate.create(title_line1="Test", bg_pattern="wave")
        assert 'opacity="0.' in svg

    def test_thumbnail_pattern_none_is_default(self) -> None:
        from blog.tools.components.thumbnail import ThumbnailTemplate

        default = ThumbnailTemplate.create(title_line1="Test")
        explicit_none = ThumbnailTemplate.create(title_line1="Test", bg_pattern="none")
        assert default == explicit_none


class TestAllThumbnailsNoTechStack:
    """Tests that all image generators no longer use tech_stack in thumbnails."""

    def test_all_thumbnails_no_tech_stack(self) -> None:
        import tempfile
        from blog.images.kis_trading_images import KISTradingImages
        from blog.images.mcp_server_images import MCPServerImages
        from blog.images.openclaw_images import OpenClawImages
        from blog.images.python314_images import Python314Images

        for cls, prefix in [
            (KISTradingImages, "kis_trading"),
            (MCPServerImages, "mcp_server"),
            (OpenClawImages, "openclaw"),
            (Python314Images, "python314"),
        ]:
            with tempfile.TemporaryDirectory() as tmpdir:
                gen = cls(prefix, images_dir=Path(tmpdir))
                paths = gen.generate_svgs()
                for path in paths:
                    if "thumbnail" in path.name:
                        content = path.read_text()
                        # Tech stack bullet format should not appear with 20px font
                        assert "•" not in content or 'font-size="20"' not in content


class TestSVGConverterFontLoading:
    """Tests for SVG to PNG font loading."""

    def test_svg_converter_waits_for_fonts(self) -> None:
        """Verify that SVGConverter uses document.fonts.ready."""
        content = Path("blog/tools/svg_converter.py").read_text(encoding="utf-8")
        assert "document.fonts.ready" in content
