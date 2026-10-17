"""ROB-1382: one RSI source + formula for analyze_stock_batch(quick) and
get_ohlcv(include_indicators=True).

Regression suite for the RSI provenance unification:

* both surfaces compute rsi_14 from the canonical ``daily_candles`` store via
  the shared Wilder series ``_calculate_rsi_series``;
* both responses disclose ``rsi_source`` and ``bars_used``;
* a smoothing mutant on either side breaks parity (assertion RED);
* intraday/historical windows never claim daily-DB provenance.
"""

import datetime
from datetime import date
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
from app.mcp_server.tooling import (
    analysis_quick,
    market_data_indicators,
    market_data_quotes,
)
from app.services.daily_candles.read_service import rows_to_frame
from app.services.daily_candles.repository import DailyCandleRow
from tests._mcp_tooling_support import build_tools

# Realistic daily return stream (ETF-shaped: mixed ±0.1-1.2% sessions, a
# pullback cluster mid-window, recovery tail). Deterministic — no RNG.
_DAILY_RETURNS_PCT = [
    0.42,
    -0.31,
    0.87,
    -0.12,
    0.55,
    -0.78,
    0.34,
    1.12,
    -0.45,
    0.62,
    -0.18,
    0.29,
    -0.93,
    0.71,
    0.38,
    -0.27,
    0.84,
    -0.36,
    0.19,
    0.66,
    -0.52,
    0.44,
    -0.61,
    0.97,
    -0.24,
    0.31,
    0.58,
    -0.83,
    0.47,
    0.22,
    -0.15,
    0.69,
    -0.38,
    0.53,
    -0.29,
    0.76,
    -0.44,
    0.35,
    0.91,
    -0.57,
    0.48,
    -0.22,
    0.64,
    -0.41,
    0.33,
    0.57,
    -0.68,
    0.29,
    0.81,
    -0.36,
]

_FIXTURE_START = date(2024, 5, 6)


def _daily_frame(extra_days: int = 0) -> pd.DataFrame:
    """Provider-shaped daily OHLCV frame (date/open/high/low/close/volume/value).

    ``extra_days`` appends bars *past* the canonical window — the shape a live
    provider returns when it carries a still-forming session the store lacks.
    """
    closes = [205.0]
    for pct in _DAILY_RETURNS_PCT:
        closes.append(closes[-1] * (1 + pct / 100.0))
    closes.extend(closes[-1] * (1 + pct / 100.0) for pct in (0.51, -0.33)[:extra_days])

    records = []
    previous_close = closes[0]
    for index, close in enumerate(closes):
        session_date = _FIXTURE_START + datetime.timedelta(days=index)
        open_ = previous_close
        high = max(open_, close) * 1.004
        low = min(open_, close) * 0.996
        volume = float(28_000_000 + index * 137_777)
        records.append(
            {
                "date": session_date,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "value": volume * close,
            }
        )
        previous_close = close
    return pd.DataFrame(records)


def _daily_rows(
    frame: pd.DataFrame, symbol: str, partition: str
) -> list[DailyCandleRow]:
    """The same bars as ``DailyCandleRow``s — what ``daily_candles`` stores."""
    rows = []
    for record in frame.to_dict("records"):
        session_date = record["date"]
        rows.append(
            DailyCandleRow(
                time_utc=datetime.datetime(
                    session_date.year,
                    session_date.month,
                    session_date.day,
                    tzinfo=datetime.UTC,
                ),
                symbol=symbol,
                partition=partition,
                open=record["open"],
                high=record["high"],
                low=record["low"],
                close=record["close"],
                adj_close=None,
                volume=record["volume"],
                value=record["value"],
                source="yahoo",
            )
        )
    return rows


def _patch_canonical(
    monkeypatch: pytest.MonkeyPatch, frame: pd.DataFrame | None
) -> AsyncMock:
    mock = AsyncMock(return_value=frame)
    monkeypatch.setattr(market_data_quotes, "_canonical_daily_frame", mock)
    return mock


def _cutler_rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    """Mutant: the pre-ROB-1382 rolling-SMA (Cutler) smoothing."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


@pytest.mark.asyncio
async def test_quick_and_ohlcv_day_report_identical_rsi(monkeypatch):
    """Core AC: same canonical candles → same rsi_14 on both surfaces."""
    tools = build_tools()
    provider_frame = _daily_frame()
    canonical_rows = _daily_rows(provider_frame, "QQQM", "NASD")
    canonical = rows_to_frame(canonical_rows)

    monkeypatch.setattr(
        yahoo_service, "fetch_ohlcv", AsyncMock(return_value=provider_frame)
    )
    _patch_canonical(monkeypatch, canonical)

    ohlcv = await tools["get_ohlcv"](
        "QQQM", count=25, period="day", include_indicators=True, market="us"
    )
    quick = analysis_quick._project_symbol("QQQM", "equity_us", canonical_rows)

    assert quick["rsi_14"] is not None
    assert ohlcv["rows"][-1]["rsi_14"] == quick["rsi_14"]
    assert ohlcv["rsi_source"] == "daily_candles_db"
    assert ohlcv["bars_used"] == len(provider_frame)
    assert quick["rsi_source"] == "daily_candles_db"
    assert quick["bars_used"] == len(canonical_rows)


@pytest.mark.asyncio
async def test_ohlcv_day_canonical_maps_per_row_not_just_last(monkeypatch):
    """Every returned row takes the RSI the canonical store reports for its
    date — not a recompute over the (shorter) provider window."""
    tools = build_tools()
    provider_frame = _daily_frame().tail(25).reset_index(drop=True)
    canonical_rows = _daily_rows(_daily_frame(), "QQQM", "NASD")
    canonical = rows_to_frame(canonical_rows)

    monkeypatch.setattr(
        yahoo_service, "fetch_ohlcv", AsyncMock(return_value=provider_frame)
    )
    _patch_canonical(monkeypatch, canonical)

    ohlcv = await tools["get_ohlcv"](
        "QQQM", count=25, period="day", include_indicators=True, market="us"
    )

    expected = market_data_indicators._calculate_rsi_series(canonical["close"]).round(2)
    expected_by_date = dict(zip(canonical["date"], expected, strict=False))
    for row in ohlcv["rows"]:
        session_date = date.fromisoformat(row["date"])
        assert row["rsi_14"] == (
            None
            if pd.isna(expected_by_date[session_date])
            else float(expected_by_date[session_date])
        )


@pytest.mark.asyncio
async def test_mutant_smoothing_on_ohlcv_side_breaks_parity(monkeypatch):
    """Mutant RED: swapping only the get_ohlcv smoothing kernel back to
    rolling-SMA must break the parity assertion — proves both surfaces share
    the same Wilder implementation, not merely same-shaped outputs."""
    tools = build_tools()
    provider_frame = _daily_frame()
    canonical_rows = _daily_rows(provider_frame, "QQQM", "NASD")
    canonical = rows_to_frame(canonical_rows)

    monkeypatch.setattr(
        yahoo_service, "fetch_ohlcv", AsyncMock(return_value=provider_frame)
    )
    _patch_canonical(monkeypatch, canonical)
    monkeypatch.setattr(market_data_quotes, "_calculate_rsi_series", _cutler_rsi_series)

    ohlcv = await tools["get_ohlcv"](
        "QQQM", count=25, period="day", include_indicators=True, market="us"
    )
    quick = analysis_quick._project_symbol("QQQM", "equity_us", canonical_rows)

    assert ohlcv["rows"][-1]["rsi_14"] != quick["rsi_14"]


@pytest.mark.asyncio
async def test_ohlcv_day_falls_back_to_provider_frame_when_canonical_missing(
    monkeypatch,
):
    tools = build_tools()
    provider_frame = _daily_frame()
    monkeypatch.setattr(
        yahoo_service, "fetch_ohlcv", AsyncMock(return_value=provider_frame)
    )
    _patch_canonical(monkeypatch, None)

    ohlcv = await tools["get_ohlcv"](
        "QQQM", count=25, period="day", include_indicators=True, market="us"
    )

    expected = round(
        float(
            market_data_indicators._calculate_rsi_series(
                provider_frame["close"].astype(float)
            ).iloc[-1]
        ),
        2,
    )
    assert ohlcv["rsi_source"] == "yahoo"
    assert ohlcv["bars_used"] == len(provider_frame)
    assert ohlcv["rows"][-1]["rsi_14"] == expected


@pytest.mark.asyncio
async def test_ohlcv_day_forming_bar_gets_null_rsi(monkeypatch):
    """A provider row whose date the canonical store lacks (still-forming
    session) reports rsi_14=None — the canonical series has no value for it."""
    tools = build_tools()
    canonical_rows = _daily_rows(_daily_frame(), "QQQM", "NASD")
    canonical = rows_to_frame(canonical_rows)
    provider_frame = _daily_frame(extra_days=1)

    monkeypatch.setattr(
        yahoo_service, "fetch_ohlcv", AsyncMock(return_value=provider_frame)
    )
    _patch_canonical(monkeypatch, canonical)

    ohlcv = await tools["get_ohlcv"](
        "QQQM", count=52, period="day", include_indicators=True, market="us"
    )

    rows = ohlcv["rows"]
    assert len(rows) == len(provider_frame)
    assert rows[-1]["rsi_14"] is None
    expected = float(
        market_data_indicators._calculate_rsi_series(canonical["close"])
        .round(2)
        .iloc[-1]
    )
    assert rows[-2]["rsi_14"] == expected


@pytest.mark.asyncio
async def test_ohlcv_non_day_period_never_touches_canonical(monkeypatch):
    """week/month are not stored in daily_candles — RSI comes from the
    returned frame and names the provider as its source."""
    tools = build_tools()
    provider_frame = _daily_frame()
    monkeypatch.setattr(
        yahoo_service, "fetch_ohlcv", AsyncMock(return_value=provider_frame)
    )
    canonical_mock = _patch_canonical(monkeypatch, None)

    ohlcv = await tools["get_ohlcv"](
        "QQQM", count=25, period="week", include_indicators=True, market="us"
    )

    canonical_mock.assert_not_awaited()
    assert ohlcv["rsi_source"] == "yahoo"
    assert ohlcv["bars_used"] == len(provider_frame)
    assert ohlcv["rows"][-1]["rsi_14"] is not None


@pytest.mark.asyncio
async def test_ohlcv_end_date_skips_canonical(monkeypatch):
    """Historical windows cannot be answered by the latest-N store — the
    canonical read must not even be attempted."""
    tools = build_tools()
    provider_frame = _daily_frame()
    monkeypatch.setattr(
        yahoo_service, "fetch_ohlcv", AsyncMock(return_value=provider_frame)
    )
    canonical_mock = _patch_canonical(monkeypatch, None)

    ohlcv = await tools["get_ohlcv"](
        "QQQM",
        count=25,
        period="day",
        end_date="2024-06-20",
        include_indicators=True,
        market="us",
    )

    canonical_mock.assert_not_awaited()
    assert ohlcv["rsi_source"] == "yahoo"
    assert ohlcv["bars_used"] == len(provider_frame)


@pytest.mark.asyncio
async def test_ohlcv_crypto_day_uses_canonical_and_matches_quick(monkeypatch):
    tools = build_tools()
    provider_frame = _daily_frame()
    canonical_rows = _daily_rows(provider_frame, "KRW-BTC", "upbit_krw")
    canonical = rows_to_frame(canonical_rows)

    monkeypatch.setattr(
        upbit_service, "fetch_ohlcv", AsyncMock(return_value=provider_frame)
    )
    _patch_canonical(monkeypatch, canonical)

    ohlcv = await tools["get_ohlcv"](
        "KRW-BTC", count=25, period="day", include_indicators=True
    )
    quick = analysis_quick._project_symbol("KRW-BTC", "crypto", canonical_rows)

    assert ohlcv["rsi_source"] == "daily_candles_db"
    assert ohlcv["bars_used"] == len(provider_frame)
    assert ohlcv["rows"][-1]["rsi_14"] == quick["rsi_14"]
    assert quick["bars_used"] == len(canonical_rows)


@pytest.mark.asyncio
async def test_ohlcv_intraday_frame_names_provider_not_db(monkeypatch):
    """Intraday RSI is computed over the provider frame — rsi_source must not
    claim daily_candles provenance it never used."""
    tools = build_tools()
    provider_frame = _daily_frame()
    provider_frame["time"] = [datetime.time(9, 30)] * len(provider_frame)
    provider_frame["datetime"] = [
        pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=30)
        for d in provider_frame["date"]
    ]
    monkeypatch.setattr(
        upbit_service, "fetch_ohlcv", AsyncMock(return_value=provider_frame)
    )
    canonical_mock = _patch_canonical(monkeypatch, None)

    ohlcv = await tools["get_ohlcv"](
        "KRW-BTC", count=25, period="1m", include_indicators=True
    )

    canonical_mock.assert_not_awaited()
    assert ohlcv["rsi_source"] == "upbit"
    assert ohlcv["bars_used"] == len(provider_frame)


def test_canonical_window_and_partition_match_quick():
    """The canonical read is pinned to the exact quick window — the constants
    can never silently drift apart."""
    assert (
        market_data_quotes._CANONICAL_DAILY_COUNT == analysis_quick.QUICK_CANDLE_COUNT
    )
    assert market_data_quotes._CANONICAL_PARTITION == {
        "equity_kr": "KRX",
        "crypto": "upbit_krw",
    }
    assert market_data_quotes._CANONICAL_PARTITION.get("equity_us") is None
    assert (
        market_data_quotes._CANONICAL_MARKET_KEY == analysis_quick._MARKET_KEY_BY_TYPE
    )


def test_quick_projection_reports_rsi_provenance():
    frame = _daily_frame()
    rows = _daily_rows(frame, "QQQM", "NASD")
    result = analysis_quick._project_symbol("QQQM", "equity_us", rows)

    assert result["rsi_source"] == "daily_candles_db"
    assert result["bars_used"] == len(rows)
    assert result["rsi_14"] is not None


def test_quick_projection_missing_data_keeps_provenance_fields():
    result = analysis_quick._empty_projection("QQQM", "equity_us")

    assert result["rsi_source"] == "daily_candles_db"
    assert result["bars_used"] == 0
    assert result["rsi_14"] is None


def test_quick_projection_halted_suspect_keeps_provenance_fields():
    base = datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)
    rows = [
        DailyCandleRow(
            time_utc=base - datetime.timedelta(days=offset),
            symbol="000880",
            partition="KRX",
            open=83800.0,
            high=83800.0,
            low=83800.0,
            close=83800.0,
            adj_close=None,
            volume=0.0,
            value=0.0,
            source="kis",
        )
        for offset in range(5, 0, -1)
    ]
    result = analysis_quick._project_symbol("000880", "equity_kr", rows)

    assert result["data_state"] == "halted_suspect"
    assert result["rsi_14"] is None
    assert result["supports"] is None
    assert result["resistances"] is None
    assert result["rsi_source"] == "daily_candles_db"
    assert result["bars_used"] == len(rows)


def test_shared_series_matches_quick_scalar_for_same_closes():
    """The get_ohlcv enrichment kernel and the quick indicator kernel are the
    same Wilder series — the scalar projection equals the series' last value."""
    close = _daily_frame()["close"].astype(float)
    series = market_data_indicators._calculate_rsi_series(close)
    scalar = market_data_indicators._calculate_rsi(close)

    assert scalar["14"] == round(float(series.iloc[-1]), 2)
