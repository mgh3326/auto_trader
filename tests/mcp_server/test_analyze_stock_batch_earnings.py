"""ROB-722 — analyze_stock_batch earnings auto-inject attach pass.

Rows use the REAL ``market_type`` values that analyze_stock_batch produces
(``resolve_market_type`` → equity_kr / equity_us / crypto). The original tests
fabricated "us"/"kr" values that never occur in production, which let the
{"kr","us"}-only market gate ship as a permanent no-op (post-merge
verification blocker).
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import analysis_tool_handlers as h
from app.mcp_server.tooling import earnings_context as ec


@pytest.mark.asyncio
async def test_attach_earnings_injects_when_context_exists(monkeypatch):
    async def _fake_build(symbol, market, *, today=None, kr_freshness=None):
        return {
            "symbol": symbol,
            "market": market,
            "has_upcoming": False,
            "next_earnings": None,
        }

    async def _fake_kr_fresh(db):
        return ("fresh", "2026-07-06")

    monkeypatch.setattr(h, "build_earnings_context", _fake_build, raising=False)
    monkeypatch.setattr(h, "_kr_ingestion_freshness", _fake_kr_fresh, raising=False)

    results = {"NVDA": {"symbol": "NVDA", "market_type": "equity_us"}}
    await h._attach_earnings(results, market="us")

    assert results["NVDA"]["earnings"]["has_upcoming"] is False


@pytest.mark.asyncio
async def test_attach_earnings_fail_open_on_error(monkeypatch):
    async def _boom(symbol, market, *, today=None, kr_freshness=None):
        raise RuntimeError("finnhub down")

    monkeypatch.setattr(h, "build_earnings_context", _boom, raising=False)

    results = {"NVDA": {"symbol": "NVDA", "market_type": "equity_us"}}
    await h._attach_earnings(results, market="us")  # must not raise

    assert "earnings" not in results["NVDA"]


@pytest.mark.asyncio
async def test_attach_earnings_skips_error_and_crypto_rows(monkeypatch):
    # Real build_earnings_context with only the calendar fetch stubbed, so the
    # market gate itself is under test (the false-green trap was mocking build
    # entirely and feeding market values production never produces).
    async def _fake_calendar(symbol, from_date, to_date, market):
        return {"symbol": symbol, "source": "finnhub", "earnings": []}

    monkeypatch.setattr(ec, "handle_get_earnings_calendar", _fake_calendar)

    results = {
        "BADSYM": {"error": "not found"},
        "KRW-BTC": {"symbol": "KRW-BTC", "market_type": "crypto"},
        "NVDA": {"symbol": "NVDA", "market_type": "equity_us"},
    }
    await h._attach_earnings(results, market=None)

    assert "earnings" not in results["BADSYM"]  # error row skipped pre-build
    assert "earnings" not in results["KRW-BTC"]  # non-equity → build returns None
    earnings = results["NVDA"]["earnings"]  # real-gate regression (equity_us)
    assert earnings["market"] == "us"
    assert earnings["has_upcoming"] is False


@pytest.mark.asyncio
async def test_attach_earnings_equity_kr_triggers_freshness_once(monkeypatch):
    # equity_kr rows must hit the KR freshness gate (normalized, not == "kr")
    # and the batch must compute freshness at most once.
    calls = {"fresh": 0}

    async def _fake_calendar(symbol, from_date, to_date, market):
        assert market == "kr"  # normalized before the calendar dispatch
        return {"symbol": symbol, "source": "market_events", "earnings": []}

    async def _fake_kr_fresh(db):
        calls["fresh"] += 1
        return ("stale", "2026-07-01")

    monkeypatch.setattr(ec, "handle_get_earnings_calendar", _fake_calendar)
    monkeypatch.setattr(h, "_kr_ingestion_freshness", _fake_kr_fresh, raising=False)

    results = {
        "005930": {"symbol": "005930", "market_type": "equity_kr"},
        "000660": {"symbol": "000660", "market_type": "equity_kr"},
    }
    await h._attach_earnings(results, market="kr")

    assert calls["fresh"] == 1
    assert results["005930"]["earnings"]["market"] == "kr"
    assert results["005930"]["earnings"]["freshness"] == "stale"
    assert results["000660"]["earnings"]["data_as_of"] == "2026-07-01"
