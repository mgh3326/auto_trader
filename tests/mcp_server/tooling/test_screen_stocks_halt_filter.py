"""ROB-1236 — ``screen_stocks`` must not rank a symbol whose bars have gone inert.

The screener consumes the same daily candles ``analyze_stock_batch`` does, so
the 000880 contamination reaches a buy ranking through this path too. Exclusion
must also be *visible*: a false positive removes a real candidate, and an
operator has to be able to see it happen.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.screening.halt_filter import exclude_halt_suspect_rows

pytestmark = pytest.mark.unit

FROZEN_PRICE = 83800.0


def _frame(*, frozen_sessions: int, live_sessions: int = 15) -> pd.DataFrame:
    opens, highs, lows, closes, volumes = [], [], [], [], []
    price = 78000.0
    for i in range(live_sessions):
        price += 150.0 if i % 3 else -100.0
        opens.append(price - 50.0)
        highs.append(price + 400.0)
        lows.append(price - 350.0)
        closes.append(price)
        volumes.append(120000.0)
    if live_sessions:
        opens[-1] = FROZEN_PRICE - 200.0
        highs[-1] = FROZEN_PRICE + 900.0
        lows[-1] = FROZEN_PRICE - 700.0
        closes[-1] = FROZEN_PRICE
    for _ in range(frozen_sessions):
        opens.append(FROZEN_PRICE)
        highs.append(FROZEN_PRICE)
        lows.append(FROZEN_PRICE)
        closes.append(FROZEN_PRICE)
        volumes.append(0.0)
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def _response() -> dict:
    return {
        "results": [
            {"symbol": "005930", "market": "kr", "close": 70000.0},
            {"symbol": "000880", "market": "kr", "close": FROZEN_PRICE},
            {"symbol": "000660", "market": "kr", "close": 190000.0},
        ],
        "total_count": 3,
        "returned_count": 3,
        "market": "kr",
        "meta": {"rsi_enrichment": {}},
    }


def _patch_frames(frames: dict[str, pd.DataFrame]):
    async def fake_fetch(symbol, market_type, count=250, **kwargs):
        return frames[symbol]

    return patch(
        "app.mcp_server.tooling.screening.halt_filter._fetch_ohlcv_for_indicators",
        new=AsyncMock(side_effect=fake_fetch),
    )


DEFAULT_FRAMES = {
    "005930": _frame(frozen_sessions=0),
    "000880": _frame(frozen_sessions=8),
    "000660": _frame(frozen_sessions=0),
}


# ---------------------------------------------------------------------------
# Cost gate — the history read must not fan out across a whole screen page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rows_whose_latest_bar_traded_skip_the_history_read():
    """A newest bar with volume cannot be the end of a zero-volume frozen run.

    Without this gate a 100-row intraday screen fires 100 live KIS candle
    fetches, because the daily cache is deliberately bypassed while a KRX
    session is open.
    """
    response = _response()
    for row in response["results"]:
        row["volume"] = 1_500_000

    fetch = AsyncMock(side_effect=AssertionError("history must not be read"))
    with patch(
        "app.mcp_server.tooling.screening.halt_filter._fetch_ohlcv_for_indicators",
        new=fetch,
    ):
        result = await exclude_halt_suspect_rows(response, market="kr")

    assert fetch.await_count == 0
    assert len(result["results"]) == 3


@pytest.mark.asyncio
async def test_zero_volume_row_still_gets_the_full_history_read():
    response = _response()
    response["results"][0]["volume"] = 1_500_000  # traded — gated out
    response["results"][1]["volume"] = 0  # halted — must be checked
    response["results"][2]["volume"] = 900_000  # traded — gated out

    with _patch_frames(DEFAULT_FRAMES) as fetch:
        result = await exclude_halt_suspect_rows(response, market="kr")

    assert fetch.await_count == 1
    assert [row["symbol"] for row in result["results"]] == ["005930", "000660"]


@pytest.mark.asyncio
async def test_unparseable_volume_falls_through_to_the_history_read():
    """The cheap gate must never be the reason a halt slips through."""
    response = _response()
    for row in response["results"]:
        row["volume"] = "n/a"

    with _patch_frames(DEFAULT_FRAMES) as fetch:
        result = await exclude_halt_suspect_rows(response, market="kr")

    assert fetch.await_count == 3
    assert [row["symbol"] for row in result["results"]] == ["005930", "000660"]


@pytest.mark.asyncio
async def test_halted_symbol_is_removed_from_screener_results():
    with _patch_frames(DEFAULT_FRAMES):
        result = await exclude_halt_suspect_rows(_response(), market="kr")

    assert [row["symbol"] for row in result["results"]] == ["005930", "000660"]
    assert result["returned_count"] == 2
    assert result["total_count"] == 2


@pytest.mark.asyncio
async def test_exclusion_is_reported_not_silent():
    with _patch_frames(DEFAULT_FRAMES):
        result = await exclude_halt_suspect_rows(_response(), market="kr")

    excluded = result["meta"]["halted_suspect_excluded"]
    assert [entry["symbol"] for entry in excluded] == ["000880"]
    assert excluded[0]["frozen_sessions"] == 8
    assert excluded[0]["krx_halt_master"] == "unavailable"

    warnings = result["warnings"]
    assert any("000880" in w and "halted_suspect" in w for w in warnings)
    # Never restated as a confirmation.
    assert any("not a\nconfirmed halt".replace("\n", " ") in w for w in warnings)


@pytest.mark.asyncio
async def test_actively_traded_rows_are_all_kept():
    frames = {symbol: _frame(frozen_sessions=0) for symbol in DEFAULT_FRAMES}

    with _patch_frames(frames):
        result = await exclude_halt_suspect_rows(_response(), market="kr")

    assert [row["symbol"] for row in result["results"]] == [
        "005930",
        "000880",
        "000660",
    ]
    assert result["total_count"] == 3
    assert "halted_suspect_excluded" not in result.get("meta", {})


@pytest.mark.asyncio
async def test_history_read_failure_keeps_the_row_and_warns():
    """A DB hiccup is not evidence of a halt — never delete a candidate on it."""

    async def boom(symbol, market_type, count=250, **kwargs):
        if symbol == "000880":
            raise TimeoutError("candle store unreachable")
        return _frame(frozen_sessions=0)

    with patch(
        "app.mcp_server.tooling.screening.halt_filter._fetch_ohlcv_for_indicators",
        new=AsyncMock(side_effect=boom),
    ):
        result = await exclude_halt_suspect_rows(_response(), market="kr")

    assert [row["symbol"] for row in result["results"]] == [
        "005930",
        "000880",
        "000660",
    ]
    assert any("halt check skipped" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_unsupported_market_is_passed_through_untouched():
    response = _response()

    result = await exclude_halt_suspect_rows(response, market="forex")

    assert result is response


@pytest.mark.asyncio
async def test_screen_stocks_unified_wires_the_halt_gate():
    """The gate has to sit on the single funnel, not on one dispatch branch."""
    import app.mcp_server.tooling.screening.entrypoint as entrypoint

    source = entrypoint.screen_stocks_unified.__code__.co_names
    assert "exclude_halt_suspect_rows" in source
