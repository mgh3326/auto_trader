"""ROB-711 — analyze_stock_batch decision_history injection.

Verifies the batched, fail-open post-pass that injects per-symbol
``decision_history`` context into analyze_stock_batch compact responses.
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import analysis_tool_handlers as h


@pytest.mark.asyncio
async def test_attach_decision_history_injects_when_context_exists(monkeypatch):
    async def _fake_build(db, symbol, market, setup_tag=None, account_mode=None):
        return {"symbol": symbol, "market": market, "prior_decisions": [{"x": 1}]}

    monkeypatch.setattr(h, "build_decision_context", _fake_build, raising=False)

    results = {"005930": {"symbol": "005930", "market_type": "kr"}}
    await h._attach_decision_history(results, market="kr")

    assert results["005930"]["decision_history"]["prior_decisions"] == [{"x": 1}]


@pytest.mark.asyncio
async def test_attach_decision_history_fail_open_on_error(monkeypatch):
    async def _boom(db, symbol, market, setup_tag=None, account_mode=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(h, "build_decision_context", _boom, raising=False)

    results = {"005930": {"symbol": "005930", "market_type": "kr"}}
    await h._attach_decision_history(results, market="kr")  # must not raise

    assert "decision_history" not in results["005930"]  # fail-open: untouched


@pytest.mark.asyncio
async def test_attach_decision_history_skips_error_rows(monkeypatch):
    async def _fake_build(db, symbol, market, setup_tag=None, account_mode=None):
        return {"symbol": symbol}

    monkeypatch.setattr(h, "build_decision_context", _fake_build, raising=False)

    results = {"BADSYM": {"error": "not found"}}
    await h._attach_decision_history(results, market="kr")

    assert "decision_history" not in results["BADSYM"]


@pytest.mark.asyncio
async def test_attach_decision_history_passes_mock_account_mode(monkeypatch):
    seen: list[str | None] = []

    async def _fake_build(db, symbol, market, setup_tag=None, account_mode=None):
        seen.append(account_mode)
        return {"symbol": symbol, "market": market, "account_mode": account_mode}

    monkeypatch.setattr(h, "build_decision_context", _fake_build, raising=False)

    results = {"005930": {"symbol": "005930", "market_type": "kr"}}
    await h._attach_decision_history(
        results, market="kr", decision_history_account_mode="kis_mock"
    )

    assert seen == ["kis_mock"]
    assert results["005930"]["decision_history"]["account_mode"] == "kis_mock"
