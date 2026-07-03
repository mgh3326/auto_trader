from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.mcp_server.tooling.fundamentals._market_index as mkt

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_current_only_kospi_skips_history_call(monkeypatch):
    current = AsyncMock(
        return_value={
            "symbol": "KOSPI",
            "name": "코스피",
            "current": 2450.5,
            "change": -45.3,
            "change_pct": -1.82,
            "open": 2390.0,
            "source": "naver",
        }
    )
    history = AsyncMock(return_value=[{"date": "2026-02-01", "close": 2450.5}])
    monkeypatch.setattr(mkt, "_fetch_index_kr_current", current)
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", history)

    result = await mkt.handle_get_market_index_current_only("KOSPI")

    assert "history" not in result
    assert result["indices"][0]["current"] == pytest.approx(2450.5)
    assert result["indices"][0]["data_state"]  # tagged by _tag_kr_index_data_state
    current.assert_awaited_once()
    history.assert_not_awaited()  # the wasted history page is never fetched


async def test_current_only_preserves_rob464_stale_override(monkeypatch):
    # change/change_pct == 0 but open != current on a FRESH clock -> ROB-464 marks
    # the KR index stale and stamps as_of. This must survive the current-only path.
    current = AsyncMock(
        return_value={
            "symbol": "KOSPI",
            "name": "코스피",
            "current": 8123.62,
            "change": 0,
            "change_pct": 0,
            "open": 8263.85,
            "source": "naver",
        }
    )
    monkeypatch.setattr(mkt, "_fetch_index_kr_current", current)
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", AsyncMock())
    monkeypatch.setattr(mkt, "kr_market_data_state", lambda *a, **k: "fresh")

    result = await mkt.handle_get_market_index_current_only("KOSPI")
    row = result["indices"][0]

    assert row["data_state"] == "stale"
    assert row["data_state_reason"] == mkt._KR_INDEX_LAGGING_REASON
    assert row["as_of"]  # stamped


async def test_current_only_unknown_symbol_raises():
    with pytest.raises(ValueError):
        await mkt.handle_get_market_index_current_only("NOPE")


async def test_current_only_leaf_failure_returns_error_payload(monkeypatch):
    monkeypatch.setattr(
        mkt, "_fetch_index_kr_current", AsyncMock(side_effect=RuntimeError("boom"))
    )
    result = await mkt.handle_get_market_index_current_only("KOSPI")
    assert "error" in result


async def test_shared_handler_still_returns_history(monkeypatch):
    # GUARD (constraint): the shared handle_get_market_index MUST keep fetching
    # history for its other callers. Task 3 must not touch it.
    monkeypatch.setattr(
        mkt,
        "_fetch_index_kr_current",
        AsyncMock(return_value={"symbol": "KOSPI", "current": 2450.5, "open": 2390.0}),
    )
    history = AsyncMock(return_value=[{"date": "2026-02-01", "close": 2450.5}])
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", history)

    result = await mkt.handle_get_market_index(symbol="KOSPI", count=1)

    assert "history" in result
    history.assert_awaited_once()
