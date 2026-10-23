"""ROB-1397: RSI requires 14 real deltas (15 closes), on every surface.

Only provider and repository I/O is replaced; RSI, canonical
mapping, quick projection and payload serialization execute their real code.
"""

import socket
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling import (
    analysis_quick,
)
from app.mcp_server.tooling import (
    market_data_indicators as indicators,
)
from app.mcp_server.tooling import (
    market_data_quotes as quotes,
)
from app.services.daily_candles.repository import DailyCandleRow, MarketKey

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _forbid_real_connections(monkeypatch):
    """Even loopback DB/Redis sockets are outside this regression's scope."""

    def forbidden(*args, **kwargs):
        raise AssertionError("ROB-1397 tests must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


# Synthetic, deterministic closes; the first 15 alternate, followed by mixed
# magnitudes and a flat session. No provider data or randomness is involved.
_CLOSES = [float(101 - i % 2) for i in range(15)] + [
    102,
    100,
    103,
    101,
    101,
    104,
    102,
    99,
    100,
    103,
    105,
    104,
    106,
    103,
    102,
    105,
    107,
    106,
    104,
    108,
    105,
    109,
    107,
    108,
    106,
    110,
    109,
    106,
    108,
    111,
    109,
    112,
    108,
    107,
    110,
    113,
    111,
    110,
    114,
    112,
    115,
    111,
    109,
    112,
    116,
    114,
    113,
    117,
    115,
]
# Independently evaluated recursive EWMA: g_t=(13*g_(t-1)+max(delta,0))/14,
# l_t=(13*l_(t-1)+max(-delta,0))/14, seeded with g_0=l_0=0.
# Keep the existing >=15-close Wilder contract; fixing output eligibility must
# not silently change that seed or replace smoothing with a rolling mean.
_RAW_CHECKPOINTS = {
    15: 51.85185185185185,
    20: 50.394778222919165,
    32: 60.45849774140933,
    64: 56.04808969184218,
}
_LAST = {13: None, 14: None, 15: 51.85, 64: 56.05}


def _frame(count: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(count)],
            "open": _CLOSES[:count],
            "high": [value + 2 for value in _CLOSES[:count]],
            "low": [value - 2 for value in _CLOSES[:count]],
            "close": _CLOSES[:count],
            "volume": [1000.0 + i for i in range(count)],
        }
    )


def _rows(frame: pd.DataFrame) -> list[DailyCandleRow]:
    return [
        DailyCandleRow(
            time_utc=datetime.combine(row.date, datetime.min.time(), tzinfo=UTC),
            symbol="QQQM",
            partition="NASD",
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            adj_close=None,
            volume=row.volume,
            value=row.volume * row.close,
            source="yahoo",
        )
        for row in frame.itertuples(index=False)
    ]


@pytest.mark.parametrize("count", [13, 14, 15, 64])
def test_scalar_and_quick_warmup(count):
    frame = _frame(count)
    assert indicators._calculate_rsi(frame["close"])["14"] == _LAST[count]
    quick = analysis_quick._project_symbol("QQQM", "equity_us", _rows(frame))
    assert quick["rsi_14"] == _LAST[count]
    assert quick["rsi_source"] == "daily_candles_db"
    assert quick["bars_used"] == count
    assert quick["data_state"] == "stale"
    assert quick["data_state_reason"] == "db_only_projection"


@pytest.mark.parametrize("count", [13, 14, 15, 64])
def test_shared_series_last_value_warmup(count):
    value = indicators._calculate_rsi_series(_frame(count)["close"]).iloc[-1]
    if _LAST[count] is None:
        assert pd.isna(value), f"{count} closes must not produce RSI: {value}"
    else:
        assert round(float(value), 2) == _LAST[count]


def test_shared_series_first_valid_position_and_long_fixture():
    close = pd.Series(_CLOSES, index=pd.date_range("2024-01-01", periods=64))
    series = indicators._calculate_rsi_series(close)
    # The first diff is a pseudo-delta, not a real price change. Fourteen real
    # deltas first exist at iloc[14] (the fifteenth close).
    assert series.first_valid_index() == close.index[14]
    assert series.iloc[:14].isna().all()
    assert series.index.equals(close.index)


@pytest.mark.parametrize("count, expected", _RAW_CHECKPOINTS.items())
def test_long_fixture_preserves_existing_wilder_values(count, expected):
    series = indicators._calculate_rsi_series(_frame(64)["close"])
    assert series.iloc[count - 1] == pytest.approx(expected, rel=0, abs=1e-12)
    assert indicators._calculate_rsi(_frame(count)["close"])["14"] == round(expected, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [13, 14, 15, 64])
@pytest.mark.parametrize("path", ["canonical", "missing", "db_error", "toss", "upbit"])
async def test_get_ohlcv_warmup_and_provenance(monkeypatch, count, path):
    frame = _frame(count)
    rows = _rows(frame)
    fetch = AsyncMock(return_value={"QQQM": rows} if path == "canonical" else {})
    if path == "db_error":
        fetch.side_effect = RuntimeError("synthetic repository failure")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, *args, **kwargs):
            raise AssertionError("unexpected database I/O")

    monkeypatch.setattr(quotes, "AsyncSessionLocal", Session)
    monkeypatch.setattr(quotes.DailyCandlesRepository, "fetch_recent_batch", fetch)
    yahoo = AsyncMock(return_value=frame.copy())
    toss = AsyncMock(return_value=frame.copy())
    upbit = AsyncMock(return_value=frame.copy())
    if path == "toss":
        yahoo.side_effect = RuntimeError("synthetic Yahoo failure")
    monkeypatch.setattr(quotes.yahoo_service, "fetch_ohlcv", yahoo)
    monkeypatch.setattr(quotes, "fetch_daily_toss_frame", toss)
    monkeypatch.setattr(quotes.upbit_service, "fetch_ohlcv", upbit)

    crypto = path == "upbit"
    result = await quotes._get_ohlcv_impl(
        "KRW-BTC" if crypto else "QQQM",
        market="crypto" if crypto else "us",
        count=count,
        period="day",
        include_indicators=True,
    )
    assert "error" not in result
    expected_source = (
        "daily_candles_db"
        if path == "canonical"
        else ("upbit" if crypto else "toss" if path == "toss" else "yahoo")
    )
    assert result["rsi_source"] == expected_source
    assert result["source"] == (
        "upbit" if crypto else "toss" if path == "toss" else "yahoo"
    )
    assert result["bars_used"] == count
    assert len(result["rows"]) == count
    fetch.assert_awaited_once_with(
        market=MarketKey.CRYPTO if crypto else MarketKey.US,
        symbols=["KRW-BTC" if crypto else "QQQM"],
        partition="upbit_krw" if crypto else None,
        count=250,
    )
    assert yahoo.await_count == (0 if crypto else 1)
    assert toss.await_count == (1 if path == "toss" else 0)
    assert upbit.await_count == (1 if crypto else 0)
    assert result["rows"][-1]["rsi_14"] == _LAST[count]


@pytest.mark.parametrize("count", [13, 14, 15, 64])
def test_halted_suspect_still_suppresses_rsi(count):
    frame = _frame(count)
    frame.loc[count - 3 :, "volume"] = 0.0
    # Even a computable RSI must be hidden when the final three sessions halt.
    result = analysis_quick._project_symbol("QQQM", "equity_us", _rows(frame))
    assert result["data_state"] == "halted_suspect"
    assert result["rsi_14"] is None
    assert result["supports"] is None
    assert result["resistances"] is None
    assert result["halt_suspect"]
    assert result["rsi_source"] == "daily_candles_db"
    assert result["bars_used"] == count


@pytest.mark.parametrize("values", [[100.0] * 15, list(range(100, 115))])
def test_zero_loss_contract_stays_null(values):
    close = pd.Series(values, dtype=float)
    assert indicators._calculate_rsi(close)["14"] is None
    assert indicators._calculate_rsi_series(close).isna().all()


@pytest.mark.parametrize("period", [1, 2, 3, 7, 14, 21])
@pytest.mark.parametrize("extra", [-1, 0, 1, 8])
def test_custom_period_boundary(period, extra):
    """Eligibility follows the requested period, including period=1."""
    count = period + extra
    close = pd.Series(_CLOSES[:count], dtype=float)
    series = indicators._calculate_rsi_series(close, period)
    assert len(series) == count
    assert series.iloc[:period].isna().all()
    scalar = indicators._calculate_rsi(close, period)[str(period)]
    if count <= period:
        assert series.isna().all()
        assert scalar is None
    else:
        assert series.first_valid_index() == period
        expected = (
            None if pd.isna(series.iloc[-1]) else round(float(series.iloc[-1]), 2)
        )
        assert scalar == expected


@pytest.mark.parametrize(
    "index",
    [
        pd.RangeIndex(100, 164),
        pd.Index(range(63, -1, -1)),
        pd.Index(["duplicate"] * 64),
        pd.date_range("2024-01-01", periods=64, tz="UTC"),
    ],
    ids=["offset", "descending", "duplicates", "timezone"],
)
def test_mask_is_positional_and_does_not_mutate_input(index):
    close = pd.Series(_CLOSES, index=index, name="prices")
    original = close.copy(deep=True)
    series = indicators._calculate_rsi_series(close)
    assert series.index.equals(index)
    assert series.iloc[:14].isna().all()
    assert series.iloc[14] == pytest.approx(_RAW_CHECKPOINTS[15], rel=0, abs=1e-12)
    pd.testing.assert_series_equal(close, original)


def test_canonical_warmup_uses_history_position_not_returned_row_count():
    """A short returned window may map to already eligible canonical history."""
    canonical = _frame(64)
    provider = canonical.iloc[[13, 14, 63]].copy()
    provider.index = pd.Index([80, 10, 80])
    rows = quotes._normalize_ohlcv_rows(
        provider, period="day", include_indicators=True, canonical_daily=canonical
    )
    assert [row["rsi_14"] for row in rows] == [None, 51.85, 56.05]
    assert quotes._rsi_provenance("yahoo", provider, canonical) == (
        "daily_candles_db",
        64,
    )
