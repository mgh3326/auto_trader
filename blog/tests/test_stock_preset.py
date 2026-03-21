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
