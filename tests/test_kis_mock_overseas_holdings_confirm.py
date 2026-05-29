"""ROB-364 — pure helpers for the KIS mock **overseas/US** holdings-delta smoke.

Unit tests for the normalization helpers the overseas smoke layers on top of
the shared ``classify_fill_by_delta`` / ``derive_fill_price`` kernel:

* ``extract_overseas_holdings_qty`` — per-symbol qty from KIS overseas holdings
  rows (``ovrs_pdno`` / ``ovrs_cblc_qty``), symbol-normalized so a KIS-format
  ``BRK/B`` matches a DB-format ``BRK.B``;
* ``latest_close_from_minute_frame`` / ``latest_timestamp_from_minute_frame`` —
  read the most-recent candle from an ascending-sorted overseas minute frame;
* ``quote_is_fresh`` — fail-closed wall-clock staleness gate.

stdlib + pandas + fakes only; no broker / network / secrets.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from app.services.brokers.kis.mock_scalping_exec.overseas_holdings_confirm import (
    extract_overseas_holdings_qty,
    latest_close_from_minute_frame,
    latest_timestamp_from_minute_frame,
    quote_is_fresh,
)

# --- extract_overseas_holdings_qty -----------------------------------------


@pytest.mark.unit
def test_extract_holdings_qty_symbol_present():
    rows = [
        {"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "5"},
        {"ovrs_pdno": "TSLA", "ovrs_cblc_qty": "3"},
    ]
    assert extract_overseas_holdings_qty(rows, "AAPL") == Decimal("5")


@pytest.mark.unit
def test_extract_holdings_qty_symbol_absent_is_zero():
    # fetch_my_us_stocks pre-filters to nonzero holdings, so an absent symbol
    # means we hold zero of it (NOT a read failure — that raises upstream).
    rows = [{"ovrs_pdno": "TSLA", "ovrs_cblc_qty": "3"}]
    assert extract_overseas_holdings_qty(rows, "AAPL") == Decimal("0")


@pytest.mark.unit
def test_extract_holdings_qty_empty_rows_is_zero():
    assert extract_overseas_holdings_qty([], "AAPL") == Decimal("0")


@pytest.mark.unit
def test_extract_holdings_qty_normalizes_kis_slash_symbol():
    # KIS holdings return ovrs_pdno in KIS format (BRK/B); the smoke passes the
    # DB-format symbol (BRK.B). Both must normalize to the same key.
    rows = [{"ovrs_pdno": "BRK/B", "ovrs_cblc_qty": "2"}]
    assert extract_overseas_holdings_qty(rows, "BRK.B") == Decimal("2")


@pytest.mark.unit
def test_extract_holdings_qty_sums_duplicate_rows():
    rows = [
        {"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "5"},
        {"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "1"},
    ]
    assert extract_overseas_holdings_qty(rows, "AAPL") == Decimal("6")


# --- latest_close_from_minute_frame ----------------------------------------


def _minute_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["datetime", "close"])


@pytest.mark.unit
def test_latest_close_uses_last_row():
    # The overseas minute frame is sorted ascending by datetime, so the latest
    # candle is the LAST row.
    frame = _minute_frame(
        [
            {"datetime": dt.datetime(2026, 5, 29, 9, 30), "close": 100.0},
            {"datetime": dt.datetime(2026, 5, 29, 9, 31), "close": 101.5},
        ]
    )
    assert latest_close_from_minute_frame(frame) == Decimal("101.5")


@pytest.mark.unit
def test_latest_close_empty_frame_is_none():
    assert latest_close_from_minute_frame(_minute_frame([])) is None


@pytest.mark.unit
def test_latest_close_nonpositive_is_none():
    frame = _minute_frame([{"datetime": dt.datetime(2026, 5, 29, 9, 31), "close": 0.0}])
    assert latest_close_from_minute_frame(frame) is None


# --- latest_timestamp_from_minute_frame ------------------------------------


@pytest.mark.unit
def test_latest_timestamp_uses_last_row():
    frame = _minute_frame(
        [
            {"datetime": dt.datetime(2026, 5, 29, 9, 30), "close": 100.0},
            {"datetime": dt.datetime(2026, 5, 29, 9, 31), "close": 101.5},
        ]
    )
    assert latest_timestamp_from_minute_frame(frame) == dt.datetime(2026, 5, 29, 9, 31)


@pytest.mark.unit
def test_latest_timestamp_empty_frame_is_none():
    assert latest_timestamp_from_minute_frame(_minute_frame([])) is None


# --- quote_is_fresh --------------------------------------------------------


@pytest.mark.unit
def test_quote_is_fresh_within_window():
    now = dt.datetime(2026, 5, 29, 14, 0, tzinfo=dt.UTC)
    latest = now - dt.timedelta(minutes=2)
    assert quote_is_fresh(latest, now, max_staleness_seconds=600) is True


@pytest.mark.unit
def test_quote_is_stale_beyond_window():
    now = dt.datetime(2026, 5, 29, 14, 0, tzinfo=dt.UTC)
    # 14h stale: market closed, or an Eastern/UTC tz mismatch -> fail closed.
    latest = now - dt.timedelta(hours=14)
    assert quote_is_fresh(latest, now, max_staleness_seconds=600) is False


@pytest.mark.unit
def test_quote_freshness_uses_absolute_skew():
    # A bar timestamped in the future (clock/tz skew) is also not fresh.
    now = dt.datetime(2026, 5, 29, 14, 0, tzinfo=dt.UTC)
    latest = now + dt.timedelta(hours=5)
    assert quote_is_fresh(latest, now, max_staleness_seconds=600) is False
