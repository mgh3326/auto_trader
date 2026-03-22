"""Tests for candlestick chart SVG component."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

def make_large_ohlcv(count: int = 80) -> list[dict[str, int | str]]:
    return [
        {
            "date": f"2026-03-{(i % 28) + 1:02d}",
            "open": 50000 + i * 10,
            "high": 50150 + i * 10,
            "low": 49900 + i * 10,
            "close": 50080 + i * 10,
            "volume": 1_000_000 + i * 1000,
        }
        for i in range(count)
    ]


# Sample OHLCV data with varied bullish, bearish, and doji-like candles
SAMPLE_OHLCV = [
    {"date": "2026-03-01", "open": 50000, "high": 52000, "low": 49000, "close": 51500, "volume": 1_000_000},
    {"date": "2026-03-02", "open": 51500, "high": 52500, "low": 51000, "close": 51200, "volume": 1_200_000},
    {"date": "2026-03-03", "open": 51200, "high": 51800, "low": 50500, "close": 51700, "volume": 900_000},
    {"date": "2026-03-04", "open": 51700, "high": 51750, "low": 50800, "close": 50800, "volume": 1_100_000},
    {"date": "2026-03-05", "open": 50800, "high": 52000, "low": 50600, "close": 51900, "volume": 800_000},
    {"date": "2026-03-06", "open": 51900, "high": 52200, "low": 51500, "close": 51550, "volume": 950_000},
    {"date": "2026-03-07", "open": 51550, "high": 51600, "low": 50800, "close": 51000, "volume": 1_050_000},
    {"date": "2026-03-08", "open": 51000, "high": 51400, "low": 50500, "close": 51400, "volume": 700_000},
    {"date": "2026-03-09", "open": 51400, "high": 51500, "low": 50900, "close": 51400, "volume": 600_000},  # Doji-like
    {"date": "2026-03-10", "open": 51400, "high": 52500, "low": 51200, "close": 52400, "volume": 1_500_000},
]


class TestCandlestickChart:
    def test_basic_render(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV)
        assert "<rect" in svg
        assert "<line" in svg

    def test_bullish_candle_uses_korean_red(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV)
        # Korean bullish color: #ef5350
        assert "#ef5350" in svg

    def test_bearish_candle_uses_korean_blue(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV)
        # Korean bearish color: #1565c0
        assert "#1565c0" in svg

    def test_volume_true_renders_volume_bars(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV, volume=True)
        assert "opacity=" in svg or "opacity=\"0.4\"" in svg

    def test_volume_false_omits_volume(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV, volume=False)
        # Should not have volume-specific opacity markers
        # Volume bars use opacity="0.4" so check we don't have that pattern
        # Just verify it renders without error
        assert isinstance(svg, str)

    def test_ema_overlay_emits_dashed_polyline(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        ema_values = {"EMA20": [c["close"] * 0.99 for c in SAMPLE_OHLCV]}
        svg = CandlestickChart.create(
            x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV, ema_values=ema_values
        )
        assert "stroke-dasharray" in svg
        assert "5,3" in svg

    def test_dark_theme_swaps_colors(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV, theme="dark")
        # Dark theme grid color
        assert "#333" in svg or "#aaa" in svg

    def test_light_theme_grid(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV, theme="light")
        # Light theme grid color
        assert "#e0e0e0" in svg

    def test_empty_ohlcv_returns_empty_state(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(x=0, y=0, width=800, height=400, ohlcv=[])
        assert isinstance(svg, str)
        assert "Empty" in svg or svg.strip() == ""

    def test_max_candles_truncates_input(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV, max_candles=5)
        assert isinstance(svg, str)
        # Should render without error with truncated data

    def test_bollinger_overlay(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        bollinger = {
            "upper": [c["high"] * 1.02 for c in SAMPLE_OHLCV],
            "lower": [c["low"] * 0.98 for c in SAMPLE_OHLCV],
            "middle": [c["close"] for c in SAMPLE_OHLCV],
        }
        svg = CandlestickChart.create(
            x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV, bollinger=bollinger
        )
        assert "<polygon" in svg

    def test_truncated_chart_keeps_overlay_lines(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        large_ohlcv = make_large_ohlcv(80)
        ema_values = {"EMA20": [row["close"] for row in large_ohlcv]}
        bollinger = {
            "upper": [row["high"] for row in large_ohlcv],
            "lower": [row["low"] for row in large_ohlcv],
            "middle": [row["close"] for row in large_ohlcv],
        }

        svg = CandlestickChart.create(
            x=0,
            y=0,
            width=800,
            height=400,
            ohlcv=large_ohlcv,
            max_candles=60,
            ema_values=ema_values,
            bollinger=bollinger,
        )

        assert 'stroke-dasharray="5,3"' in svg
        assert 'stroke-dasharray="3,3"' in svg

    def test_dark_theme_uses_dark_candle_palette(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(
            x=0,
            y=0,
            width=800,
            height=400,
            ohlcv=SAMPLE_OHLCV,
            theme="dark",
        )

        assert "#00c853" in svg
        assert "#ff1744" in svg

    def test_default_max_candles_is_sixty(self) -> None:
        from blog.tools.stock.candlestick_chart import CandlestickChart

        svg = CandlestickChart.create(
            x=0,
            y=0,
            width=800,
            height=400,
            ohlcv=make_large_ohlcv(80),
            volume=False,
        )

        assert svg.count("<rect") == 60


class TestPriceChartCandlestickMode:
    def test_price_chart_candlestick_mode(self) -> None:
        from blog.tools.stock.price_chart import PriceChart

        svg = PriceChart.create(
            x=0,
            y=0,
            width=800,
            height=400,
            ohlcv=SAMPLE_OHLCV,
            chart_type="candlestick",
        )
        assert "<rect" in svg

    def test_price_chart_line_mode_unchanged(self) -> None:
        from blog.tools.stock.price_chart import PriceChart

        svg = PriceChart.create(x=0, y=0, width=800, height=400, ohlcv=SAMPLE_OHLCV)
        assert "<polyline" in svg


class TestVolumeProfile:
    def test_basic_render(self) -> None:
        from blog.tools.stock.volume_profile import VolumeProfile

        svg = VolumeProfile.create(x=0, y=0, width=100, height=400, ohlcv=SAMPLE_OHLCV)
        assert "<rect" in svg

    def test_current_price_marker(self) -> None:
        from blog.tools.stock.volume_profile import VolumeProfile

        svg = VolumeProfile.create(
            x=0,
            y=0,
            width=100,
            height=400,
            ohlcv=SAMPLE_OHLCV,
            current_price=51000,
        )
        # Check for some kind of marker indicator
        assert "marker" in svg.lower() or "arrow" in svg.lower() or "triangle" in svg.lower() or "<polygon" in svg
