"""Tests for backtest fetch_data module."""

import sys
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import fetch_data


class TestMarketSelection:
    """Tests for market/symbol selection."""

    def test_krw_only_filtering(self):
        """Test that only KRW markets are selected."""
        markets = [
            {"market": "KRW-BTC", "korean_name": "비트코인", "english_name": "Bitcoin"},
            {"market": "KRW-ETH", "korean_name": "이더리움", "english_name": "Ethereum"},
            {"market": "BTC-ETH", "korean_name": "이더리움", "english_name": "Ethereum"},  # Should be filtered
            {"market": "USDT-BTC", "korean_name": "비트코인", "english_name": "Bitcoin"},  # Should be filtered
        ]

        result = fetch_data._filter_krw_markets(markets)

        assert len(result) == 2
        assert all(m["market"].startswith("KRW-") for m in result)

    def test_top_n_slicing(self):
        """Test top-N market slicing."""
        markets = [
            {"market": "KRW-BTC", "trade_price": 50000},
            {"market": "KRW-ETH", "trade_price": 3000},
            {"market": "KRW-XRP", "trade_price": 0.5},
            {"market": "KRW-DOGE", "trade_price": 0.1},
            {"market": "KRW-SOL", "trade_price": 100},
        ]

        result = fetch_data._select_top_n(markets, top_n=3)

        assert len(result) == 3

    def test_symbols_normalization(self):
        """Test --symbols argument normalization."""
        symbols = ["BTC", "ETH", "SOL"]

        result = fetch_data._normalize_symbols(symbols)

        assert result == ["KRW-BTC", "KRW-ETH", "KRW-SOL"]


class TestCandleNormalization:
    """Tests for candle data normalization."""

    def test_candle_normalization(self):
        """Test conversion from Upbit API rows to target schema."""
        api_rows = [
            {
                "candle_date_time_utc": "2026-03-20T00:00:00",
                "opening_price": 50000.0,
                "high_price": 51000.0,
                "low_price": 49000.0,
                "trade_price": 50500.0,
                "candle_acc_trade_volume": 100.5,
                "candle_acc_trade_price": 5000000.0,
            },
            {
                "candle_date_time_utc": "2026-03-21T00:00:00",
                "opening_price": 50500.0,
                "high_price": 51500.0,
                "low_price": 50000.0,
                "trade_price": 51200.0,
                "candle_acc_trade_volume": 120.0,
                "candle_acc_trade_price": 6000000.0,
            },
        ]

        df = fetch_data._normalize_candles(api_rows)

        assert df.columns.tolist() == ["date", "open", "high", "low", "close", "volume", "value"]
        assert df["date"].tolist() == ["2026-03-20", "2026-03-21"]
        assert df["close"].tolist() == [50500.0, 51200.0]


class TestMergeDedupe:
    """Tests for merge and dedupe functionality."""

    def test_merge_with_existing_data(self, tmp_path):
        """Test incremental merge behavior."""
        # Create existing parquet
        existing_df = pd.DataFrame({
            "date": ["2026-03-18", "2026-03-19", "2026-03-20"],
            "open": [48000.0, 49000.0, 50000.0],
            "high": [49000.0, 50000.0, 51000.0],
            "low": [47000.0, 48000.0, 49000.0],
            "close": [49000.0, 50000.0, 50500.0],
            "volume": [80.0, 90.0, 100.0],
            "value": [4000000.0, 4500000.0, 5000000.0],
        })
        parquet_path = tmp_path / "test.parquet"
        existing_df.to_parquet(parquet_path, index=False)

        # New fetched data (overlapping)
        new_df = pd.DataFrame({
            "date": ["2026-03-19", "2026-03-20", "2026-03-21"],  # 03-19 and 03-20 overlap
            "open": [49500.0, 50500.0, 51500.0],  # Different prices
            "high": [50500.0, 51500.0, 52500.0],
            "low": [48500.0, 49500.0, 50500.0],
            "close": [50000.0, 51000.0, 52000.0],
            "volume": [95.0, 105.0, 115.0],
            "value": [4750000.0, 5250000.0, 5750000.0],
        })

        result = fetch_data._merge_with_existing(new_df, parquet_path)

        # Should have 4 unique dates
        assert len(result) == 4
        assert result["date"].tolist() == ["2026-03-18", "2026-03-19", "2026-03-20", "2026-03-21"]
        # Newer data should replace old for overlapping dates
        assert result[result["date"] == "2026-03-20"]["close"].iloc[0] == 51000.0

    def test_merge_new_data_only(self, tmp_path):
        """Test merge with no overlapping dates."""
        existing_df = pd.DataFrame({
            "date": ["2026-03-18", "2026-03-19"],
            "open": [48000.0, 49000.0],
            "high": [49000.0, 50000.0],
            "low": [47000.0, 48000.0],
            "close": [49000.0, 50000.0],
            "volume": [80.0, 90.0],
            "value": [4000000.0, 4500000.0],
        })
        parquet_path = tmp_path / "test.parquet"
        existing_df.to_parquet(parquet_path, index=False)

        new_df = pd.DataFrame({
            "date": ["2026-03-20", "2026-03-21"],
            "open": [50000.0, 51000.0],
            "high": [51000.0, 52000.0],
            "low": [49000.0, 50000.0],
            "close": [50500.0, 51500.0],
            "volume": [100.0, 110.0],
            "value": [5000000.0, 5500000.0],
        })

        result = fetch_data._merge_with_existing(new_df, parquet_path)

        assert len(result) == 4
        assert result["date"].tolist() == ["2026-03-18", "2026-03-19", "2026-03-20", "2026-03-21"]

    def test_result_sorted_ascending(self, tmp_path):
        """Test that merged result is sorted ascending by date."""
        existing_df = pd.DataFrame({
            "date": ["2026-03-21", "2026-03-22"],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [1.0, 2.0],
            "value": [1.0, 2.0],
        })
        parquet_path = tmp_path / "test.parquet"
        existing_df.to_parquet(parquet_path, index=False)

        new_df = pd.DataFrame({
            "date": ["2026-03-19", "2026-03-20"],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [1.0, 2.0],
            "value": [1.0, 2.0],
        })

        result = fetch_data._merge_with_existing(new_df, parquet_path)

        assert result["date"].tolist() == ["2026-03-19", "2026-03-20", "2026-03-21", "2026-03-22"]


class TestCLIOptions:
    """Tests for CLI argument parsing."""

    def test_no_args_parses(self):
        """Test that no arguments parses correctly."""
        with mock.patch("sys.argv", ["fetch_data.py"]):
            args = fetch_data._parse_args()
            assert args.symbols is None
            assert args.days == 730
            assert args.top_n == 100

    def test_symbols_arg(self):
        """Test --symbols argument."""
        with mock.patch("sys.argv", ["fetch_data.py", "--symbols", "BTC", "ETH", "SOL"]):
            args = fetch_data._parse_args()
            assert args.symbols == ["BTC", "ETH", "SOL"]

    def test_days_arg(self):
        """Test --days argument."""
        with mock.patch("sys.argv", ["fetch_data.py", "--days", "365"]):
            args = fetch_data._parse_args()
            assert args.days == 365

    def test_top_n_arg(self):
        """Test --top-n argument."""
        with mock.patch("sys.argv", ["fetch_data.py", "--top-n", "50"]):
            args = fetch_data._parse_args()
            assert args.top_n == 50
