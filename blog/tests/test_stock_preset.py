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
        {
            "date": f"2024-01-{d:02d}",
            "open": 65000 + d * 100,
            "high": 66000 + d * 100,
            "low": 64000 + d * 100,
            "close": 65500 + d * 100,
            "volume": 10000000 + d * 100000,
        }
        for d in range(2, 22)
    ],
    # Lightweight deterministic overlay values for full renderer coverage
    "ema_values": {
        "EMA20": [65200 + d * 100 for d in range(20)],
        "EMA60": [65100 + d * 100 for d in range(20)],
    },
    "bollinger": {
        "upper": [66500 + d * 100 for d in range(20)],
        "lower": [63500 + d * 100 for d in range(20)],
        "middle": [65000 + d * 100 for d in range(20)],
    },
}


class TestStockAnalysisPresetHybridMode:
    """Tests for StockAnalysisPreset hybrid screenshot mode."""

    def test_screenshot_path_none_keeps_svg_only_behavior(self) -> None:
        """screenshot_path=None should keep existing SVG-only technical behavior."""
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        with tempfile.TemporaryDirectory() as tmpdir:
            preset = StockAnalysisPreset(
                "005930", SAMPLE_DATA, output_dir=Path(tmpdir), screenshot_path=None
            )
            paths = preset.generate_svgs()
            tech = next(p for p in paths if "technical" in p.stem)
            content = tech.read_text()

            # Should contain vector chart elements (candlestick chart)
            assert "<rect" in content  # Candle bodies
            # Should not contain embedded PNG
            assert "data:image/png;base64," not in content

    def test_valid_screenshot_switches_to_hybrid_mode(self, tmp_path: Path) -> None:
        """Valid screenshot_path should switch technical image to hybrid mode."""
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        # Create a minimal valid PNG file
        png_data = bytes([
            0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
            0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
            0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
            0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
            0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41,
            0x54, 0x78, 0x9C, 0x63, 0x60, 0x00, 0x00, 0x00,
            0x02, 0x00, 0x01, 0x73, 0x75, 0x01, 0x18, 0x00,
            0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
            0x42, 0x60, 0x82,
        ])
        screenshot_path = tmp_path / "test_screenshot.png"
        screenshot_path.write_bytes(png_data)

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        preset = StockAnalysisPreset(
            "005930", SAMPLE_DATA, output_dir=output_dir, screenshot_path=screenshot_path
        )
        paths = preset.generate_svgs()
        tech = next(p for p in paths if "technical" in p.stem)
        content = tech.read_text()

        # Should contain embedded PNG in hybrid mode
        assert "data:image/png;base64," in content
        # Should still contain indicator dashboard
        assert "RSI" in content
        # Should still contain support/resistance
        assert "지지선" in content or "저항선" in content or "support" in content.lower()

    def test_hybrid_mode_keeps_indicator_and_sr_fragments(self, tmp_path: Path) -> None:
        """Hybrid mode should include both indicator and support/resistance fragments."""
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        png_data = bytes([
            0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
            0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
            0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
            0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
            0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41,
            0x54, 0x78, 0x9C, 0x63, 0x60, 0x00, 0x00, 0x00,
            0x02, 0x00, 0x01, 0x73, 0x75, 0x01, 0x18, 0x00,
            0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
            0x42, 0x60, 0x82,
        ])
        screenshot_path = tmp_path / "test_screenshot.png"
        screenshot_path.write_bytes(png_data)

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        preset = StockAnalysisPreset(
            "005930", SAMPLE_DATA, output_dir=output_dir, screenshot_path=screenshot_path
        )
        paths = preset.generate_svgs()
        tech = next(p for p in paths if "technical" in p.stem)
        content = tech.read_text()

        # Check indicator elements are present
        assert "RSI" in content
        assert "MACD" in content

        # Check support/resistance section is present
        sr_data = SAMPLE_DATA["support_resistance"]
        assert str(sr_data["supports"][0]) in content or "지지" in content


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
            # Candlestick chart elements
            assert "<rect" in content  # Candle bodies and volume bars
            assert "opacity=" in content  # Volume bars use opacity
            # Bollinger and EMA overlays
            assert "<polygon" in content  # Bollinger fill area

    def test_stock_thumbnail_uses_theme_and_icons(self) -> None:
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        preset = StockAnalysisPreset("005930", SAMPLE_DATA)
        svg = preset._create_thumbnail()
        assert "<path" in svg

    def test_stock_preset_thumbnail_uses_font_family(self) -> None:
        from blog.tools.components.base import FONT_FAMILY
        from blog.tools.presets.stock_analysis import StockAnalysisPreset

        preset = StockAnalysisPreset("005930", SAMPLE_DATA)
        svg = preset._create_thumbnail()
        assert FONT_FAMILY in svg


class TestSamsungAnalysisImages:
    def test_import_and_instantiate(self) -> None:
        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        gen = SamsungAnalysisImages()
        assert gen.prefix == "samsung_analysis"
        assert hasattr(gen, "get_images")

    def test_get_images_returns_five(self) -> None:
        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        gen = SamsungAnalysisImages()
        images = gen.get_images()
        assert len(images) == 5
        names = [name for name, _, _, _ in images]
        assert "thumbnail" in names
        assert "technical" in names

    def test_default_data_populated(self) -> None:
        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        gen = SamsungAnalysisImages()
        assert gen.data["company_profile"]["name"] == "삼성전자"
        assert "indicators" in gen.data
        assert "valuation" in gen.data

    def test_custom_data_override(self) -> None:
        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        custom = {
            "company_profile": {
                "name": "SK하이닉스",
                "symbol": "000660",
                "sector": "반도체",
            }
        }
        gen = SamsungAnalysisImages(data=custom)
        assert gen.data["company_profile"]["name"] == "SK하이닉스"

    def test_generate_svgs_produces_valid_files(self) -> None:
        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        with tempfile.TemporaryDirectory() as tmpdir:
            gen = SamsungAnalysisImages(images_dir=Path(tmpdir))
            paths = gen.generate_svgs()
            assert len(paths) == 5
            for p in paths:
                assert p.exists()
                content = p.read_text()
                assert content.startswith("<?xml")
                assert "</svg>" in content
