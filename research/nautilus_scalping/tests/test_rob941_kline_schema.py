"""ROB-941 (AC4/AC5) — normalized 1m kline schema + fail-closed validation.

Real Binance USD-M kline CSV columns (confirmed via a live probe of
``BTCUSDT-1m-2025-07.csv``, header present):

    open_time,open,high,low,close,volume,close_time,quote_volume,count,
    taker_buy_volume,taker_buy_quote_volume,ignore

``close_time - open_time == 59999`` and ``open_time`` is 60000ms-grid-aligned for
every real row observed. Malformed duration (59000/61000ms) and grid misalignment
must both raise, not be silently coerced.
"""

import pytest
import rob941_kline_schema as ks

HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"


def _row(
    open_time=1751328000000,
    open_=100.0,
    high=101.0,
    low=99.0,
    close=100.5,
    volume=10.0,
    close_time=1751328059999,
    quote_volume=1000.0,
    count=5,
    taker_buy_volume=4.0,
    taker_buy_quote_volume=400.0,
):
    return (
        f"{open_time},{open_},{high},{low},{close},{volume},{close_time},"
        f"{quote_volume},{count},{taker_buy_volume},{taker_buy_quote_volume},0"
    )


def test_parse_kline_csv_happy_path_preserves_all_fields():
    text = HEADER + _row() + "\n"
    rows = ks.parse_kline_csv(
        "BTCUSDT", text, 1751328000000, 1751328000000 + 3 * 60_000
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "BTCUSDT"
    assert r.open_time_ms == 1751328000000
    assert (r.open, r.high, r.low, r.close) == (100.0, 101.0, 99.0, 100.5)
    assert r.base_volume == 10.0
    assert r.close_time_ms == 1751328059999
    assert r.quote_volume == 1000.0
    assert r.trade_count == 5
    assert r.taker_buy_volume == 4.0
    assert r.taker_buy_quote_volume == 400.0


def test_parse_kline_csv_skips_header_row():
    text = HEADER + _row() + "\n"
    rows = ks.parse_kline_csv(
        "BTCUSDT", text, 1751328000000, 1751328000000 + 3 * 60_000
    )
    assert all(r.open_time_ms != "open_time" for r in rows)
    assert len(rows) == 1


def test_parse_kline_csv_clips_rows_outside_frozen_window():
    inside = _row(open_time=1751328060000, close_time=1751328119999)
    before_window = _row(
        open_time=1751327940000, close_time=1751327999999
    )  # 1 min before start
    text = HEADER + before_window + "\n" + inside + "\n"
    rows = ks.parse_kline_csv(
        "BTCUSDT", text, 1751328000000, 1751328000000 + 3 * 60_000
    )
    assert [r.open_time_ms for r in rows] == [1751328060000]


# --------------------------------------------------------------------------- #
# fail-closed: invalid OHLCV (AC4)
# --------------------------------------------------------------------------- #
def test_high_below_low_is_invalid_ohlcv():
    text = HEADER + _row(high=90.0, low=99.0) + "\n"
    with pytest.raises(ks.InvalidOHLCVError):
        ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 60_000)


def test_high_below_close_is_invalid_ohlcv():
    text = HEADER + _row(high=100.0, close=100.5) + "\n"
    with pytest.raises(ks.InvalidOHLCVError):
        ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 60_000)


def test_negative_volume_is_invalid_ohlcv():
    text = HEADER + _row(volume=-1.0) + "\n"
    with pytest.raises(ks.InvalidOHLCVError):
        ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 60_000)


def test_taker_buy_volume_exceeding_total_volume_is_invalid_ohlcv():
    text = HEADER + _row(volume=10.0, taker_buy_volume=99.0) + "\n"
    with pytest.raises(ks.InvalidOHLCVError):
        ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 60_000)


def test_non_positive_price_is_invalid_ohlcv():
    text = HEADER + _row(open_=0.0) + "\n"
    with pytest.raises(ks.InvalidOHLCVError):
        ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 60_000)


# --------------------------------------------------------------------------- #
# fail-closed: 59s / 61s bar duration (AC4/AC5 grid validation)
# --------------------------------------------------------------------------- #
def test_59_second_bar_duration_is_rejected():
    # close_time - open_time == 59000ms instead of the required 59999ms
    text = HEADER + _row(close_time=1751328000000 + 59_000) + "\n"
    with pytest.raises(ks.InvalidOHLCVError, match="duration"):
        ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 60_000)


def test_61_second_bar_duration_is_rejected():
    # close_time - open_time == 61000ms instead of the required 59999ms
    text = HEADER + _row(close_time=1751328000000 + 61_000) + "\n"
    with pytest.raises(ks.InvalidOHLCVError, match="duration"):
        ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 60_000)


def test_open_time_not_grid_aligned_is_rejected():
    text = HEADER + _row(open_time=1751328000001, close_time=1751328060000) + "\n"
    with pytest.raises(ks.InvalidOHLCVError, match="grid"):
        ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 2 * 60_000)


# --------------------------------------------------------------------------- #
# fail-closed: conflicting duplicate timestamps (AC4); identical dup dedupes
# --------------------------------------------------------------------------- #
def test_conflicting_duplicate_open_time_is_fail_closed():
    # both rows individually valid OHLCV, but disagree on close -> conflict, not corruption
    text = HEADER + _row(close=100.5) + "\n" + _row(close=100.6) + "\n"
    with pytest.raises(ks.ConflictingDuplicateError):
        ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 60_000)


def test_identical_duplicate_open_time_is_deduped_not_rejected():
    text = HEADER + _row() + "\n" + _row() + "\n"
    rows = ks.parse_kline_csv("BTCUSDT", text, 1751328000000, 1751328000000 + 60_000)
    assert len(rows) == 1
