"""ROB-1059 H1 (spec §14.1/AC1) — spot archive URL builders + REST backfill
parsing, network-0 (fake in-memory openers only).
"""

import json

import pytest
import rob941_archive_fetch as af
import spot_archive_fetch as saf


def test_spot_kline_monthly_url_shape():
    url = saf.spot_kline_monthly_url("BTCUSDC", "1m", 2024, 6)
    assert url == (
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDC/1m/"
        "BTCUSDC-1m-2024-06.zip"
    )


def test_spot_kline_daily_url_shape():
    url = saf.spot_kline_daily_url("BTCUSDC", "1m", 2024, 6, 5)
    assert url == (
        "https://data.binance.vision/data/spot/daily/klines/BTCUSDC/1m/"
        "BTCUSDC-1m-2024-06-05.zip"
    )


def test_rest_klines_url_shape():
    url = saf.rest_klines_url("BTCUSDC", "1m", 1000, 2000, limit=500)
    assert url == (
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDC&interval=1m"
        "&startTime=1000&endTime=2000&limit=500"
    )


def test_rest_klines_url_rejects_out_of_range_limit():
    with pytest.raises(ValueError):
        saf.rest_klines_url("BTCUSDC", "1m", 0, 1000, limit=0)
    with pytest.raises(ValueError):
        saf.rest_klines_url("BTCUSDC", "1m", 0, 1000, limit=1001)


def _rest_entry(ts: int, close: float = 100.5):
    return [
        ts,
        "100.0",
        "101.0",
        "99.0",
        str(close),
        "10.0",
        ts + 59_999,
        "1000.0",
        5,
        "4.0",
        "400.0",
        "0",
    ]


def test_fetch_rest_klines_parses_json_array_and_clips_to_window():
    entries = [_rest_entry(m * 60_000) for m in range(5)]
    body = json.dumps(entries).encode()

    def opener(url):
        return body

    rows = saf.fetch_rest_klines("BTCUSDC", "1m", 60_000, 4 * 60_000, opener)
    # clipped to [60_000, 240_000): minutes 1,2,3 only (minute 0 and 4 excluded)
    assert [r.open_time_ms for r in rows] == [60_000, 120_000, 180_000]


def test_fetch_rest_klines_raises_on_404():
    def opener(url):
        return None

    with pytest.raises(af.ArchiveMissingError):
        saf.fetch_rest_klines("BTCUSDC", "1m", 0, 60_000, opener)


def test_fetch_rest_klines_raises_on_malformed_json():
    def opener(url):
        return b"not json{{{"

    with pytest.raises(saf.MalformedRestResponseError):
        saf.fetch_rest_klines("BTCUSDC", "1m", 0, 60_000, opener)


def test_fetch_rest_klines_raises_when_response_is_not_a_json_array():
    def opener(url):
        return json.dumps({"error": "bad request"}).encode()

    with pytest.raises(saf.MalformedRestResponseError):
        saf.fetch_rest_klines("BTCUSDC", "1m", 0, 60_000, opener)


def test_fetch_rest_klines_raises_on_short_entry():
    def opener(url):
        return json.dumps([[1, 2, 3]]).encode()  # only 3 fields, need >=11

    with pytest.raises(saf.MalformedRestResponseError):
        saf.fetch_rest_klines("BTCUSDC", "1m", 0, 60_000, opener)


def test_month_bounds_ms_half_open():
    start, end = saf.month_bounds_ms(2024, 2)  # leap Feb
    assert (end - start) // 86_400_000 == 29


def test_days_in_month_count_matches_calendar():
    days = saf.days_in_month(2024, 6)
    assert len(days) == 30
    assert days[0] == (2024, 6, 1)
    assert days[-1] == (2024, 6, 30)
