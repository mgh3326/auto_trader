"""The ROB-3796 fetcher admits only USD-M futures kline archive files."""

from __future__ import annotations

import pytest

from research.crypto_trend.archive import zip_url
from research.crypto_trend.http import DisallowedUrlError, check_url

KLINE = "https://data.binance.vision/data/futures/um/monthly/klines"


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        f"{KLINE}/BTCUSDT/4h/BTCUSDT-4h-2026-09.zip",
        f"{KLINE}/BTCUSDT/4h/BTCUSDT-4h-2026-09.zip.CHECKSUM",
        f"{KLINE}/XRPUSDT/1d/XRPUSDT-1d-2023-06.zip",
        zip_url("SOLUSDT", "1d", "2024-02"),
    ],
)
def test_admits_futures_um_kline_archive_files(url: str) -> None:
    check_url(url)


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        # spot archive: the crypto_universe allowlist, never this one
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2026-09.zip",
        # other intervals and products
        f"{KLINE}/BTCUSDT/1m/BTCUSDT-1m-2026-09.zip",
        "https://data.binance.vision/data/futures/cm/monthly/klines/BTCUSD_PERP/1d/BTCUSD_PERP-1d-2026-09.zip",
        "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/4h/BTCUSDT-4h-2026-09-01.zip",
        # wrong file shape
        f"{KLINE}/BTCUSDT/4h/BTCUSDT-4h-2026-09.csv",
        f"{KLINE}/BTCUSDT/4h/BTCUSDT-1d-2026-09.zip",
        # traversal, encoding, query, userinfo, port, host, scheme
        f"{KLINE}/../../../spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2026-09.zip",
        f"{KLINE}/BTCUSDT%2F..%2F4h/BTCUSDT-4h-2026-09.zip",
        f"{KLINE}/BTCUSDT/4h/BTCUSDT-4h-2026-09.zip?x=1",
        "https://user@data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/4h/BTCUSDT-4h-2026-09.zip",
        "https://data.binance.vision:8443/data/futures/um/monthly/klines/BTCUSDT/4h/BTCUSDT-4h-2026-09.zip",
        "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=4h",
        "https://demo-fapi.binance.com/fapi/v1/order",
        "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/futures/um/",
        "http://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/4h/BTCUSDT-4h-2026-09.zip",
    ],
)
def test_refuses_everything_outside_the_kline_archive(url: str) -> None:
    with pytest.raises(DisallowedUrlError):
        check_url(url)


@pytest.mark.unit
def test_truncated_body_is_retried_not_fatal() -> None:
    # http.client.IncompleteRead is an HTTPException, not an OSError, on 3.13.
    import http.client

    from research.crypto_trend.http import FetchResponse, with_retries

    calls: list[str] = []

    def flaky(url: str) -> FetchResponse:
        calls.append(url)
        if len(calls) == 1:
            raise http.client.IncompleteRead(b"partial")
        return FetchResponse(200, b"complete")

    fetch = with_retries(flaky, attempts=3, base_delay=0.0)
    assert fetch("https://data.binance.vision/x") == FetchResponse(200, b"complete")
    assert len(calls) == 2
