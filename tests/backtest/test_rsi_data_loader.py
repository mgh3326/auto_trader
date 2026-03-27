"""Tests for 1h candle data loader."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.data_loader import (
    normalize_candles,
    merge_with_existing,
    load_candles,
    DATA_DIR,
)


class TestNormalizeCandles:
    """Tests for Upbit API response normalization."""

    def test_empty_list_returns_empty_df(self):
        df = normalize_candles([])
        assert len(df) == 0
        assert list(df.columns) == ["datetime", "open", "high", "low", "close", "volume", "value"]

    def test_maps_upbit_columns(self):
        raw = [
            {
                "candle_date_time_kst": "2024-01-01T09:00:00",
                "opening_price": 100.0,
                "high_price": 110.0,
                "low_price": 90.0,
                "trade_price": 105.0,
                "candle_acc_trade_volume": 50.0,
                "candle_acc_trade_price": 5000.0,
            }
        ]
        df = normalize_candles(raw)
        assert len(df) == 1
        assert df.iloc[0]["datetime"] == "2024-01-01T09:00:00"
        assert df.iloc[0]["close"] == 105.0
        assert df.iloc[0]["value"] == 5000.0

    def test_sorts_by_datetime_ascending(self):
        raw = [
            {"candle_date_time_kst": "2024-01-01T11:00:00", "opening_price": 100, "high_price": 100, "low_price": 100, "trade_price": 100, "candle_acc_trade_volume": 1, "candle_acc_trade_price": 100},
            {"candle_date_time_kst": "2024-01-01T09:00:00", "opening_price": 99, "high_price": 99, "low_price": 99, "trade_price": 99, "candle_acc_trade_volume": 1, "candle_acc_trade_price": 99},
        ]
        df = normalize_candles(raw)
        assert df.iloc[0]["datetime"] == "2024-01-01T09:00:00"
        assert df.iloc[1]["datetime"] == "2024-01-01T11:00:00"


class TestMergeWithExisting:
    """Tests for merging new data with cached parquet."""

    def test_no_existing_returns_new(self, tmp_path):
        new_df = pd.DataFrame({"datetime": ["2024-01-01T09:00:00"], "close": [100.0]})
        result = merge_with_existing(new_df, tmp_path / "nonexistent.parquet")
        assert len(result) == 1

    def test_deduplicates_by_datetime(self, tmp_path):
        existing = pd.DataFrame({"datetime": ["2024-01-01T09:00:00"], "close": [100.0]})
        parquet_path = tmp_path / "test.parquet"
        existing.to_parquet(parquet_path, index=False)

        new_df = pd.DataFrame({
            "datetime": ["2024-01-01T09:00:00", "2024-01-01T10:00:00"],
            "close": [101.0, 102.0],
        })
        result = merge_with_existing(new_df, parquet_path)
        assert len(result) == 2
        # New data should overwrite existing for same datetime
        row = result[result["datetime"] == "2024-01-01T09:00:00"]
        assert row.iloc[0]["close"] == 101.0


class TestLoadCandles:
    """Tests for loading cached candle data."""

    def test_returns_none_when_no_cache(self, tmp_path):
        result = load_candles("KRW-BTC", "2024-01-01", "2024-02-01", data_dir=tmp_path)
        assert result is None

    def test_filters_by_date_range(self, tmp_path):
        df = pd.DataFrame({
            "datetime": [
                "2024-01-01T09:00:00",
                "2024-01-15T09:00:00",
                "2024-02-15T09:00:00",
            ],
            "open": [100, 101, 102],
            "high": [100, 101, 102],
            "low": [100, 101, 102],
            "close": [100, 101, 102],
            "volume": [10, 10, 10],
            "value": [1000, 1010, 1020],
        })
        path = tmp_path / "KRW-BTC.parquet"
        df.to_parquet(path, index=False)

        result = load_candles("KRW-BTC", "2024-01-01", "2024-01-31", data_dir=tmp_path)
        assert result is not None
        assert len(result) == 2
