"""ROB-722 — analyze_stock_batch earnings auto-inject attach pass."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import analysis_tool_handlers as h


@pytest.mark.asyncio
async def test_attach_earnings_injects_when_context_exists(monkeypatch):
    async def _fake_build(symbol, market, *, today=None, kr_freshness=None):
        return {"symbol": symbol, "market": market, "has_upcoming": False,
                "next_earnings": None}

    async def _fake_kr_fresh(db):
        return ("fresh", "2026-07-06")

    monkeypatch.setattr(h, "build_earnings_context", _fake_build, raising=False)
    monkeypatch.setattr(h, "_kr_ingestion_freshness", _fake_kr_fresh, raising=False)

    results = {"NVDA": {"symbol": "NVDA", "market_type": "us"}}
    await h._attach_earnings(results, market="us")

    assert results["NVDA"]["earnings"]["has_upcoming"] is False


@pytest.mark.asyncio
async def test_attach_earnings_fail_open_on_error(monkeypatch):
    async def _boom(symbol, market, *, today=None, kr_freshness=None):
        raise RuntimeError("finnhub down")

    monkeypatch.setattr(h, "build_earnings_context", _boom, raising=False)

    results = {"NVDA": {"symbol": "NVDA", "market_type": "us"}}
    await h._attach_earnings(results, market="us")  # must not raise

    assert "earnings" not in results["NVDA"]


@pytest.mark.asyncio
async def test_attach_earnings_skips_error_and_crypto_rows(monkeypatch):
    # Mirrors real build: None for crypto (skip), dict for equities. Error rows
    # are skipped by _attach_earnings before build is ever called.
    async def _fake_build(symbol, market, *, today=None, kr_freshness=None):
        if str(market).strip().lower() == "crypto":
            return None
        return {"symbol": symbol}

    monkeypatch.setattr(h, "build_earnings_context", _fake_build, raising=False)

    results = {
        "BADSYM": {"error": "not found"},
        "BTC": {"symbol": "BTC", "market_type": "crypto"},
        "NVDA": {"symbol": "NVDA", "market_type": "us"},
    }
    await h._attach_earnings(results, market=None)

    assert "earnings" not in results["BADSYM"]      # error row skipped pre-build
    assert "earnings" not in results["BTC"]         # build returns None → omit
    assert results["NVDA"]["earnings"] == {"symbol": "NVDA"}  # equity attached
