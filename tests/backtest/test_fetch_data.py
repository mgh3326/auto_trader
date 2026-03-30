"""Tests for backtest fetch_data module."""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

backtest_dir = Path(__file__).resolve().parent.parent.parent / "backtest"
spec = importlib.util.spec_from_file_location(
    "fetch_data", backtest_dir / "fetch_data.py"
)
if spec is None or spec.loader is None:
    raise ImportError("Unable to load backtest/fetch_data.py")
fetch_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_data)


class TestIntervalSupport:
    """Tests for --interval option and path routing."""

    def test_default_interval_is_1d(self):
        """Test that default interval is '1d'."""
        with mock.patch("sys.argv", ["fetch_data.py"]):
            args = fetch_data._parse_args()
            assert args.interval == "1d"

    def test_interval_1h_parses(self):
        """Test --interval 1h parses correctly."""
        with mock.patch("sys.argv", ["fetch_data.py", "--interval", "1h"]):
            args = fetch_data._parse_args()
            assert args.interval == "1h"

    def test_interval_4h_parses(self):
        """Test --interval 4h parses correctly."""
        with mock.patch("sys.argv", ["fetch_data.py", "--interval", "4h"]):
            args = fetch_data._parse_args()
            assert args.interval == "4h"

    def test_end_date_parses(self):
        """Test --end-date parses as a plain CLI string."""
        with mock.patch(
            "sys.argv",
            ["fetch_data.py", "--interval", "1d", "--end-date", "2026-03-22"],
        ):
            args = fetch_data._parse_args()
            assert args.end_date == "2026-03-22"

    def test_data_dir_for_1d(self):
        """Test data directory for 1d interval (backward compat: flat dir)."""
        result = fetch_data._data_dir_for_interval("1d")
        assert result == fetch_data.DATA_DIR

    def test_data_dir_for_1h(self):
        """Test data directory for 1h interval."""
        result = fetch_data._data_dir_for_interval("1h")
        assert result == fetch_data.DATA_DIR / "1h"

    def test_data_dir_for_4h(self):
        """Test data directory for 4h interval."""
        result = fetch_data._data_dir_for_interval("4h")
        assert result == fetch_data.DATA_DIR / "4h"


class TestMarketSelection:
    """Tests for market/symbol selection."""

    def test_krw_only_filtering(self):
        """Test that only KRW markets are selected."""
        markets = [
            {"market": "KRW-BTC", "korean_name": "비트코인", "english_name": "Bitcoin"},
            {
                "market": "KRW-ETH",
                "korean_name": "이더리움",
                "english_name": "Ethereum",
            },
            {
                "market": "BTC-ETH",
                "korean_name": "이더리움",
                "english_name": "Ethereum",
            },  # Should be filtered
            {
                "market": "USDT-BTC",
                "korean_name": "비트코인",
                "english_name": "Bitcoin",
            },  # Should be filtered
        ]

        result = fetch_data._filter_krw_markets(markets)

        assert len(result) == 2
        assert all(m["market"].startswith("KRW-") for m in result)

    def test_top_n_slicing(self):
        """Test top-N market slicing by 24h traded value."""
        markets = [
            {"market": "KRW-BTC", "acc_trade_price_24h": 1000000000000},
            {"market": "KRW-ETH", "acc_trade_price_24h": 500000000000},
            {"market": "KRW-XRP", "acc_trade_price_24h": 1000000000},
            {"market": "KRW-DOGE", "acc_trade_price_24h": 500000000},
            {"market": "KRW-SOL", "acc_trade_price_24h": 200000000000},
        ]

        result = fetch_data._select_top_n(markets, top_n=3)

        assert len(result) == 3
        # Should be sorted by acc_trade_price_24h descending
        assert result[0] == "KRW-BTC"
        assert result[1] == "KRW-ETH"
        assert result[2] == "KRW-SOL"

    def test_symbols_normalization(self):
        """Test --symbols argument normalization."""
        symbols = ["BTC", "ETH", "SOL"]

        result = fetch_data._normalize_symbols(symbols)

        assert result == ["KRW-BTC", "KRW-ETH", "KRW-SOL"]

    def test_main_ranks_full_candidate_set(self, monkeypatch):
        """Test that main ranks the full candidate set before slicing top-N."""
        markets = [
            {"market": "KRW-A", "acc_trade_price_24h": 10},
            {"market": "KRW-B", "acc_trade_price_24h": 20},
            {"market": "KRW-C", "acc_trade_price_24h": 30},
            {"market": "KRW-D", "acc_trade_price_24h": 40},
            {"market": "KRW-E", "acc_trade_price_24h": 50},
            {"market": "KRW-F", "acc_trade_price_24h": 60},
            {"market": "KRW-G", "acc_trade_price_24h": 1_000},
        ]
        ticker_values = {
            market["market"]: market["acc_trade_price_24h"] for market in markets
        }
        fetched_markets: list[str] = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None):
                assert params is not None
                market = params["markets"]
                return FakeResponse(
                    [
                        {"acc_trade_price_24h": ticker_values[market]},
                    ]
                )

        def fake_fetch_markets():
            return markets

        def fake_fetch_candles(market, days, *, to_date=None):
            fetched_markets.append(market)
            return []

        monkeypatch.setattr(fetch_data, "fetch_markets", fake_fetch_markets)
        monkeypatch.setattr(fetch_data.httpx, "Client", FakeClient)
        monkeypatch.setattr(fetch_data, "fetch_candles", fake_fetch_candles)
        monkeypatch.setattr(fetch_data.time, "sleep", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            sys,
            "argv",
            ["fetch_data.py", "--top-n", "3", "--days", "1"],
        )

        fetch_data.main()

        assert len(fetched_markets) == 3
        assert "KRW-G" in fetched_markets


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

        assert df.columns.tolist() == [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "value",
        ]
        assert df["date"].tolist() == ["2026-03-20", "2026-03-21"]
        assert df["close"].tolist() == [50500.0, 51200.0]


class TestDeterministicWindow:
    """Tests for reproducible backtest fetch windows."""

    def test_resolve_fetch_end_datetime_defaults_daily_to_backtest_horizon(self):
        result = fetch_data._resolve_fetch_end_datetime("1d", None)
        assert result == datetime(2026, 3, 23, 0, 0, 0)

    def test_resolve_fetch_end_datetime_uses_explicit_end_date(self):
        result = fetch_data._resolve_fetch_end_datetime("1d", "2026-03-22")
        assert result == datetime(2026, 3, 23, 0, 0, 0)

    def test_save_candles_can_replace_existing_snapshot(self, tmp_path):
        existing_df = pd.DataFrame(
            {
                "date": ["2025-06-10", "2025-06-11", "2025-06-12"],
                "open": [1.0, 2.0, 3.0],
                "high": [1.0, 2.0, 3.0],
                "low": [1.0, 2.0, 3.0],
                "close": [1.0, 2.0, 3.0],
                "volume": [1.0, 2.0, 3.0],
                "value": [1.0, 2.0, 3.0],
            }
        )
        new_df = pd.DataFrame(
            {
                "date": ["2025-06-10", "2025-06-11"],
                "open": [10.0, 20.0],
                "high": [10.0, 20.0],
                "low": [10.0, 20.0],
                "close": [10.0, 20.0],
                "volume": [10.0, 20.0],
                "value": [10.0, 20.0],
            }
        )
        parquet_path = tmp_path / "KRW-BTC.parquet"
        existing_df.to_parquet(parquet_path, index=False)

        fetch_data.save_candles(
            "KRW-BTC",
            new_df,
            data_dir=tmp_path,
            replace_existing=True,
        )

        saved = pd.read_parquet(parquet_path)
        pd.testing.assert_frame_equal(saved, new_df)


class TestDataQuality:
    def test_validate_1h_no_gaps(self):
        dates = pd.date_range("2026-03-20", periods=24, freq="h")
        df = pd.DataFrame(
            {
                "date": dates.strftime("%Y-%m-%dT%H:%M:%S"),
                "open": [1.0] * 24,
                "high": [1.0] * 24,
                "low": [1.0] * 24,
                "close": [1.0] * 24,
                "volume": [1.0] * 24,
                "value": [1.0] * 24,
            }
        )
        result = fetch_data._validate_data_quality(df, "1h")
        assert result["missing_pct"] == 0.0
        assert result["max_gap_hours"] == 0.0
        assert result["total_bars"] == 24

    def test_validate_1h_with_gap(self):
        dates = [
            "2026-03-20T00:00:00",
            "2026-03-20T01:00:00",
            "2026-03-20T02:00:00",
            "2026-03-20T09:00:00",
        ]
        df = pd.DataFrame(
            {
                "date": dates,
                "open": [1.0] * 4,
                "high": [1.0] * 4,
                "low": [1.0] * 4,
                "close": [1.0] * 4,
                "volume": [1.0] * 4,
                "value": [1.0] * 4,
            }
        )
        result = fetch_data._validate_data_quality(df, "1h")
        assert result["missing_pct"] > 0
        assert result["max_gap_hours"] >= 6.0

    def test_validate_1d_no_gaps(self):
        df = pd.DataFrame(
            {
                "date": ["2026-03-20", "2026-03-21", "2026-03-22"],
                "open": [1.0] * 3,
                "high": [1.0] * 3,
                "low": [1.0] * 3,
                "close": [1.0] * 3,
                "volume": [1.0] * 3,
                "value": [1.0] * 3,
            }
        )
        result = fetch_data._validate_data_quality(df, "1d")
        assert result["total_bars"] == 3

    def test_validate_single_bar(self):
        df = pd.DataFrame(
            {
                "date": ["2026-03-20T00:00:00"],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [1.0],
                "value": [1.0],
            }
        )
        result = fetch_data._validate_data_quality(df, "1h")
        assert result["total_bars"] == 1
        assert result["missing_pct"] == 0.0


class TestMinuteCandleFetch:
    def test_fetch_candles_minutes_builds_correct_url(self, monkeypatch):
        captured_urls: list[str] = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return [
                    {
                        "candle_date_time_utc": "2026-03-20T10:00:00",
                        "opening_price": 50000.0,
                        "high_price": 51000.0,
                        "low_price": 49000.0,
                        "trade_price": 50500.0,
                        "candle_acc_trade_volume": 10.5,
                        "candle_acc_trade_price": 500000.0,
                    }
                ]

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                captured_urls.append(url)
                return FakeResponse()

        monkeypatch.setattr(fetch_data.httpx, "Client", FakeClient)
        monkeypatch.setattr(fetch_data.time, "sleep", lambda *a: None)

        fetch_data.fetch_candles_minutes("KRW-BTC", unit=60, hours=1)

        assert any("/candles/minutes/60" in u for u in captured_urls)

    def test_fetch_candles_minutes_paginates(self, monkeypatch):
        call_count = [0]

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                call_count[0] += 1
                if call_count[0] <= 2:
                    base_time = datetime(2026, 3, 20 - call_count[0], 23, 0, 0)
                    return [
                        {
                            "candle_date_time_utc": (
                                base_time - timedelta(hours=i)
                            ).strftime("%Y-%m-%dT%H:%M:%S"),
                            "opening_price": 1.0,
                            "high_price": 1.0,
                            "low_price": 1.0,
                            "trade_price": 1.0,
                            "candle_acc_trade_volume": 1.0,
                            "candle_acc_trade_price": 1.0,
                        }
                        for i in range(200)
                    ]
                return []

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                return FakeResponse()

        monkeypatch.setattr(fetch_data.httpx, "Client", FakeClient)
        monkeypatch.setattr(fetch_data.time, "sleep", lambda *a: None)

        result = fetch_data.fetch_candles_minutes("KRW-BTC", unit=60, hours=400)
        assert len(result) == 400

    def test_normalize_minute_candles_datetime_format(self):
        api_rows = [
            {
                "candle_date_time_utc": "2026-03-20T14:00:00",
                "opening_price": 50000.0,
                "high_price": 51000.0,
                "low_price": 49000.0,
                "trade_price": 50500.0,
                "candle_acc_trade_volume": 10.5,
                "candle_acc_trade_price": 500000.0,
            }
        ]

        df = fetch_data._normalize_candles(api_rows, interval="1h")

        assert df["date"].iloc[0] == "2026-03-20T14:00:00"


class TestNormalizeCandlesInterval:
    def test_1d_normalization_date_only(self):
        api_rows = [
            {
                "candle_date_time_utc": "2026-03-20T00:00:00",
                "opening_price": 50000.0,
                "high_price": 51000.0,
                "low_price": 49000.0,
                "trade_price": 50500.0,
                "candle_acc_trade_volume": 100.5,
                "candle_acc_trade_price": 5000000.0,
            }
        ]

        df = fetch_data._normalize_candles(api_rows, interval="1d")

        assert df["date"].iloc[0] == "2026-03-20"


class TestMergeDedupe:
    """Tests for merge and dedupe functionality."""

    def test_merge_with_existing_data(self, tmp_path):
        """Test incremental merge behavior."""
        # Create existing parquet
        existing_df = pd.DataFrame(
            {
                "date": ["2026-03-18", "2026-03-19", "2026-03-20"],
                "open": [48000.0, 49000.0, 50000.0],
                "high": [49000.0, 50000.0, 51000.0],
                "low": [47000.0, 48000.0, 49000.0],
                "close": [49000.0, 50000.0, 50500.0],
                "volume": [80.0, 90.0, 100.0],
                "value": [4000000.0, 4500000.0, 5000000.0],
            }
        )
        parquet_path = tmp_path / "test.parquet"
        existing_df.to_parquet(parquet_path, index=False)

        # New fetched data (overlapping)
        new_df = pd.DataFrame(
            {
                "date": [
                    "2026-03-19",
                    "2026-03-20",
                    "2026-03-21",
                ],  # 03-19 and 03-20 overlap
                "open": [49500.0, 50500.0, 51500.0],  # Different prices
                "high": [50500.0, 51500.0, 52500.0],
                "low": [48500.0, 49500.0, 50500.0],
                "close": [50000.0, 51000.0, 52000.0],
                "volume": [95.0, 105.0, 115.0],
                "value": [4750000.0, 5250000.0, 5750000.0],
            }
        )

        result = fetch_data._merge_with_existing(new_df, parquet_path)

        # Should have 4 unique dates
        assert len(result) == 4
        assert result["date"].tolist() == [
            "2026-03-18",
            "2026-03-19",
            "2026-03-20",
            "2026-03-21",
        ]
        # Newer data should replace old for overlapping dates
        assert result[result["date"] == "2026-03-20"]["close"].iloc[0] == 51000.0

    def test_merge_new_data_only(self, tmp_path):
        """Test merge with no overlapping dates."""
        existing_df = pd.DataFrame(
            {
                "date": ["2026-03-18", "2026-03-19"],
                "open": [48000.0, 49000.0],
                "high": [49000.0, 50000.0],
                "low": [47000.0, 48000.0],
                "close": [49000.0, 50000.0],
                "volume": [80.0, 90.0],
                "value": [4000000.0, 4500000.0],
            }
        )
        parquet_path = tmp_path / "test.parquet"
        existing_df.to_parquet(parquet_path, index=False)

        new_df = pd.DataFrame(
            {
                "date": ["2026-03-20", "2026-03-21"],
                "open": [50000.0, 51000.0],
                "high": [51000.0, 52000.0],
                "low": [49000.0, 50000.0],
                "close": [50500.0, 51500.0],
                "volume": [100.0, 110.0],
                "value": [5000000.0, 5500000.0],
            }
        )

        result = fetch_data._merge_with_existing(new_df, parquet_path)

        assert len(result) == 4
        assert result["date"].tolist() == [
            "2026-03-18",
            "2026-03-19",
            "2026-03-20",
            "2026-03-21",
        ]

    def test_result_sorted_ascending(self, tmp_path):
        """Test that merged result is sorted ascending by date."""
        existing_df = pd.DataFrame(
            {
                "date": ["2026-03-21", "2026-03-22"],
                "open": [1.0, 2.0],
                "high": [1.0, 2.0],
                "low": [1.0, 2.0],
                "close": [1.0, 2.0],
                "volume": [1.0, 2.0],
                "value": [1.0, 2.0],
            }
        )
        parquet_path = tmp_path / "test.parquet"
        existing_df.to_parquet(parquet_path, index=False)

        new_df = pd.DataFrame(
            {
                "date": ["2026-03-19", "2026-03-20"],
                "open": [1.0, 2.0],
                "high": [1.0, 2.0],
                "low": [1.0, 2.0],
                "close": [1.0, 2.0],
                "volume": [1.0, 2.0],
                "value": [1.0, 2.0],
            }
        )

        result = fetch_data._merge_with_existing(new_df, parquet_path)

        assert result["date"].tolist() == [
            "2026-03-19",
            "2026-03-20",
            "2026-03-21",
            "2026-03-22",
        ]


class TestIncrementalRefresh:
    """Tests for overlap-window incremental refresh behavior."""

    def test_determine_refresh_days_uses_overlap_window(self):
        """Test that existing data triggers overlap-window refresh."""
        existing_df = pd.DataFrame(
            {
                "date": ["2026-03-19", "2026-03-20", "2026-03-21", "2026-03-22"],
                "open": [1.0, 1.0, 1.0, 1.0],
                "high": [1.0, 1.0, 1.0, 1.0],
                "low": [1.0, 1.0, 1.0, 1.0],
                "close": [1.0, 1.0, 1.0, 1.0],
                "volume": [1.0, 1.0, 1.0, 1.0],
                "value": [1.0, 1.0, 1.0, 1.0],
            }
        )

        assert (
            fetch_data._determine_refresh_days(
                existing_df,
                requested_days=365,
                today=datetime(2026, 3, 22),
            )
            == 7
        )

    def test_determine_refresh_days_covers_gap_since_last_stored_date(self):
        """Test that refresh window covers the stale gap plus overlap days."""
        existing_df = pd.DataFrame(
            {
                "date": ["2026-03-01"],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [1.0],
                "value": [1.0],
            }
        )

        refresh_days = fetch_data._determine_refresh_days(
            existing_df,
            requested_days=365,
            overlap_days=7,
            today=datetime(2026, 3, 22),
        )

        assert refresh_days == 28

    def test_main_uses_overlap_window_for_existing_parquet(self, tmp_path, monkeypatch):
        """Test that reruns fetch only the overlap window when parquet exists."""
        today = datetime.now().date()
        existing_df = pd.DataFrame(
            {
                "date": [
                    (today - timedelta(days=3)).strftime("%Y-%m-%d"),
                    (today - timedelta(days=2)).strftime("%Y-%m-%d"),
                    (today - timedelta(days=1)).strftime("%Y-%m-%d"),
                    today.strftime("%Y-%m-%d"),
                ],
                "open": [1.0, 1.0, 1.0, 1.0],
                "high": [1.0, 1.0, 1.0, 1.0],
                "low": [1.0, 1.0, 1.0, 1.0],
                "close": [1.0, 1.0, 1.0, 1.0],
                "volume": [1.0, 1.0, 1.0, 1.0],
                "value": [1.0, 1.0, 1.0, 1.0],
            }
        )
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        existing_df.to_parquet(data_dir / "KRW-BTC.parquet", index=False)

        seen_days: list[int] = []

        def fake_fetch_candles(market, days, *, to_date=None):
            seen_days.append(days)
            return []

        def fake_fetch_markets():
            return [{"market": "KRW-BTC", "acc_trade_price_24h": 1_000}]

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None):
                return FakeResponse([{"acc_trade_price_24h": 1_000}])

        monkeypatch.setattr(fetch_data, "DATA_DIR", data_dir)
        monkeypatch.setattr(fetch_data, "fetch_markets", fake_fetch_markets)
        monkeypatch.setattr(fetch_data.httpx, "Client", FakeClient)
        monkeypatch.setattr(fetch_data, "fetch_candles", fake_fetch_candles)
        monkeypatch.setattr(fetch_data.time, "sleep", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            sys,
            "argv",
            ["fetch_data.py", "--symbols", "BTC", "--days", "365"],
        )

        fetch_data.main()

        assert seen_days == [365]


class TestIntervalAwareMain:
    def test_main_1h_calls_fetch_candles_minutes(self, monkeypatch, tmp_path):
        called_with: list[dict[str, object]] = []

        def fake_fetch_candles_minutes(market, unit, hours, *, to_date=None):
            called_with.append(
                {"market": market, "unit": unit, "hours": hours, "to_date": to_date}
            )
            return []

        data_dir = tmp_path / "data" / "1h"
        data_dir.mkdir(parents=True)

        monkeypatch.setattr(fetch_data, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(
            fetch_data, "fetch_candles_minutes", fake_fetch_candles_minutes
        )
        monkeypatch.setattr(fetch_data.time, "sleep", lambda *a: None)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_data.py",
                "--interval",
                "1h",
                "--symbols",
                "BTC",
                "--days",
                "30",
            ],
        )

        fetch_data.main()

        assert len(called_with) == 1
        assert called_with[0]["unit"] == 60
        assert called_with[0]["hours"] == 30 * 24
        assert isinstance(called_with[0]["to_date"], datetime)

    def test_main_1d_calls_fetch_candles(self, monkeypatch, tmp_path):
        called_with: list[dict[str, object]] = []

        def fake_fetch_candles(market, days, *, to_date=None):
            called_with.append({"market": market, "days": days, "to_date": to_date})
            return []

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)

        monkeypatch.setattr(fetch_data, "DATA_DIR", data_dir)
        monkeypatch.setattr(fetch_data, "fetch_candles", fake_fetch_candles)
        monkeypatch.setattr(fetch_data.time, "sleep", lambda *a: None)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_data.py",
                "--interval",
                "1d",
                "--symbols",
                "BTC",
                "--days",
                "30",
            ],
        )

        fetch_data.main()

        assert len(called_with) == 1
        assert called_with[0]["days"] == 30
        assert called_with[0]["to_date"] == datetime(2026, 3, 23, 0, 0, 0)


class TestIncrementalRefreshHourly:
    def test_determine_refresh_hours_empty(self):
        hours = fetch_data._determine_refresh_hours(None, requested_hours=720)
        assert hours == 720

    def test_determine_refresh_hours_recent_data(self):
        existing_df = pd.DataFrame(
            {
                "date": [
                    "2026-03-22T10:00:00",
                    "2026-03-22T11:00:00",
                    "2026-03-22T12:00:00",
                ],
                "open": [1.0] * 3,
                "high": [1.0] * 3,
                "low": [1.0] * 3,
                "close": [1.0] * 3,
                "volume": [1.0] * 3,
                "value": [1.0] * 3,
            }
        )
        hours = fetch_data._determine_refresh_hours(
            existing_df,
            requested_hours=720,
            overlap_hours=48,
            now=datetime(2026, 3, 22, 13, 0, 0),
        )
        assert hours == 49


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
        with mock.patch(
            "sys.argv", ["fetch_data.py", "--symbols", "BTC", "ETH", "SOL"]
        ):
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
