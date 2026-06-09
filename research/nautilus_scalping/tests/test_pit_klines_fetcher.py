import pit_klines_fetcher as f
import pytest


def test_kline_url_monthly_um():
    url = f.kline_url("EOSUSDT", "1d", 2024, 1, market="um", cadence="monthly")
    assert url == (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        "EOSUSDT/1d/EOSUSDT-1d-2024-01.zip"
    )


def test_kline_url_daily_1h_zero_pads():
    url = f.kline_url("BTCUSDT", "1h", 2026, 3, market="um", cadence="daily", day=5)
    assert url.endswith("futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-03-05.zip")


def test_kline_url_monthly_5m():
    url = f.kline_url("ETHUSDT", "5m", 2024, 2, market="um", cadence="monthly")
    assert url == (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        "ETHUSDT/5m/ETHUSDT-5m-2024-02.zip"
    )


def test_kline_url_rejects_unknown_interval():
    with pytest.raises(ValueError, match="interval"):
        f.kline_url("BTCUSDT", "15s", 2024, 1)


def test_intervals_supported():
    assert set(f.SUPPORTED_INTERVALS) == {"1d", "1h", "5m"}
