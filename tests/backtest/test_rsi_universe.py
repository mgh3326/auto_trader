"""Tests for universe selection by rolling trade value."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.universe import select_universe


def _make_candles(market: str, values: list[float]) -> pd.DataFrame:
    """Helper: create candle DataFrame with given hourly values."""
    n = len(values)
    datetimes = [f"2024-01-01T{h:02d}:00:00" for h in range(n)]
    return pd.DataFrame({
        "datetime": datetimes,
        "open": [100.0] * n,
        "high": [100.0] * n,
        "low": [100.0] * n,
        "close": [100.0] * n,
        "volume": [1.0] * n,
        "value": values,
    })


class TestSelectUniverse:
    """Tests for top-N universe selection."""

    def test_selects_top_n_by_value(self):
        all_data = {
            "KRW-BTC": _make_candles("KRW-BTC", [1000, 2000, 3000]),
            "KRW-ETH": _make_candles("KRW-ETH", [500, 600, 700]),
            "KRW-XRP": _make_candles("KRW-XRP", [100, 200, 300]),
        }
        result = select_universe(all_data, "2024-01-01T02:00:00", top_n=2, window=3)
        assert len(result) == 2
        assert result[0] == "KRW-BTC"
        assert result[1] == "KRW-ETH"

    def test_top_n_larger_than_available(self):
        all_data = {
            "KRW-BTC": _make_candles("KRW-BTC", [1000]),
        }
        result = select_universe(all_data, "2024-01-01T00:00:00", top_n=5, window=1)
        assert len(result) == 1

    def test_skips_markets_without_data_at_timestamp(self):
        btc = _make_candles("KRW-BTC", [1000, 2000])
        # ETH only has data at T00, not T01
        eth = pd.DataFrame({
            "datetime": ["2024-01-01T00:00:00"],
            "open": [100], "high": [100], "low": [100], "close": [100],
            "volume": [1], "value": [9999],
        })
        all_data = {"KRW-BTC": btc, "KRW-ETH": eth}
        result = select_universe(all_data, "2024-01-01T01:00:00", top_n=2, window=2)
        assert "KRW-BTC" in result

    def test_window_rolls_correctly(self):
        # 5 hours of data, window=3, check at T04
        btc_values = [100, 200, 300, 400, 500]
        eth_values = [600, 100, 100, 100, 100]
        all_data = {
            "KRW-BTC": _make_candles("KRW-BTC", btc_values),
            "KRW-ETH": _make_candles("KRW-ETH", eth_values),
        }
        # At T04: BTC rolling 3 = 300+400+500=1200, ETH rolling 3 = 100+100+100=300
        result = select_universe(all_data, "2024-01-01T04:00:00", top_n=1, window=3)
        assert result == ["KRW-BTC"]
