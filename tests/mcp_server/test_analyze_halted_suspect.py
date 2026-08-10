"""ROB-1236 — ``analyze_stock_impl`` must not call an inert series "fresh".

Incident: 000880 한화 sat in a 인적분할 매매거래정지 for eight sessions with
``volume == 0`` and OHLC frozen at 83,800. ``analyze_stock_batch`` returned
``data_state: "fresh"`` plus RSI 35.40, supports/resistances and an +84% upside
— every one of them arithmetic over dead candles — and a live session ranked it
buy candidate #2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_analyze import analyze_stock_impl
from app.mcp_server.tooling.analysis_tool_handlers import _summarize_analysis_result

pytestmark = pytest.mark.unit

FROZEN_PRICE = 83800.0
SYMBOL = "000880"


def _frame(*, frozen_sessions: int, live_sessions: int = 190) -> pd.DataFrame:
    """Live history, then ``frozen_sessions`` zero-volume frozen bars."""
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
        # The final live session closes exactly where the halt freezes.
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
            "value": [c * 1000 for c in closes],
        }
    )


async def _analyze(frame: pd.DataFrame) -> dict:
    """Run the KR analyze pipeline against ``frame`` with everything else stubbed.

    Every stub is deliberately *healthy* — populated indicators, populated
    support/resistance, a provider envelope with real evidence and a current
    timestamp — so the baseline freshness verdict is literally ``"fresh"``.
    That is what the 000880 session saw, and it is what makes these tests fail
    loudly if the halt gate is removed rather than merely shifting the state.
    """
    with (
        patch(
            "app.mcp_server.tooling.analysis_analyze._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=frame,
        ),
        patch(
            "app.mcp_server.tooling.analysis_analyze._resolve_kr_quote",
            new_callable=AsyncMock,
            return_value={
                "symbol": SYMBOL,
                "instrument_type": "equity_kr",
                "price": FROZEN_PRICE,
                "source": "kis",
                "data_state": "fresh",
                "price_usable": True,
                "is_stale_price": False,
            },
        ),
        patch(
            "app.mcp_server.tooling.analysis_analyze._get_indicators_impl",
            new_callable=AsyncMock,
            return_value={"rsi": {"14": 35.4}, "bb": {"upper": 1.0, "lower": 0.5}},
        ),
        patch(
            "app.mcp_server.tooling.analysis_analyze._get_support_resistance_impl",
            new_callable=AsyncMock,
            return_value={
                "supports": [{"price": 80000.0}],
                "resistances": [{"price": 90000.0}],
            },
        ),
        patch(
            "app.mcp_server.tooling.analysis_analyze._fetch_kr_snapshot_cached",
            new_callable=AsyncMock,
            return_value={
                "payload": {},
                "evidence_present": True,
                "status": "ok",
                "cache_hit": False,
                "fetched_at": datetime.now(UTC).isoformat(),
            },
        ),
    ):
        return await analyze_stock_impl(SYMBOL, market="kr")


# ---------------------------------------------------------------------------
# The incident fixture: before the fix this returned data_state "fresh"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_000880_frozen_series_is_not_fresh():
    analysis = await _analyze(_frame(frozen_sessions=8))

    # Identical stubs to the healthy-symbol test below, which asserts "fresh" —
    # the only difference is the eight inert bars.
    assert analysis["data_state"] == "halted_suspect"
    assert analysis["data_state"] != "fresh"
    assert analysis["quote"]["data_state"] == "halted_suspect"
    assert analysis["quote"]["data_state"] != "fresh"
    assert analysis["quote"]["price_usable"] is False


@pytest.mark.asyncio
async def test_000880_bar_derived_indicators_are_null_not_estimated():
    analysis = await _analyze(_frame(frozen_sessions=8))

    assert analysis["indicators"] is None
    assert analysis["support_resistance"] is None


@pytest.mark.asyncio
async def test_000880_recommendation_is_floored_and_carries_no_rsi():
    analysis = await _analyze(_frame(frozen_sessions=8))

    recommendation = analysis["recommendation"]
    assert recommendation["rsi14"] is None
    assert recommendation["action"] == "hold"
    assert recommendation["confidence"] == "low"
    assert recommendation["insufficient_inputs"]


@pytest.mark.asyncio
async def test_000880_evidence_is_attached_as_a_suspicion():
    analysis = await _analyze(_frame(frozen_sessions=8))

    evidence = analysis["halt_suspect"]
    assert evidence["suspected"] is True
    assert evidence["frozen_sessions"] == 8
    assert evidence["krx_halt_master"] == "unavailable"
    assert "not a confirmed trading halt" in evidence["note"]


@pytest.mark.asyncio
async def test_000880_compact_batch_summary_carries_the_suspicion():
    """The batch contract is what the live session actually read."""
    analysis = await _analyze(_frame(frozen_sessions=8))

    summary = _summarize_analysis_result(SYMBOL, analysis)

    assert summary["data_state"] == "halted_suspect"
    assert summary["halt_suspect"]["suspected"] is True
    assert summary["rsi_14"] is None
    assert summary["supports"] == []
    assert summary["resistances"] == []


# ---------------------------------------------------------------------------
# No false positives — a flagged live symbol is silently un-buyable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actively_traded_symbol_stays_fresh_with_indicators():
    analysis = await _analyze(_frame(frozen_sessions=0))

    assert analysis["data_state"] == "fresh"
    assert "halt_suspect" not in analysis
    assert analysis["indicators"] is not None
    assert analysis["support_resistance"] is not None

    summary = _summarize_analysis_result(SYMBOL, analysis)
    assert summary["rsi_14"] == 35.4
    assert [level["price"] for level in summary["supports"]] == [80000.0]


@pytest.mark.asyncio
async def test_two_frozen_sessions_are_below_the_threshold():
    analysis = await _analyze(_frame(frozen_sessions=2))

    assert analysis["data_state"] == "fresh"
    assert analysis["indicators"] is not None
