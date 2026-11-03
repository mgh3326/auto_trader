"""KRX eligibility gaps must reach the run-start briefing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp_server.tooling import operating_briefing as ob


@pytest.fixture
def briefing_stubs(monkeypatch):
    session_factory = MagicMock()
    monkeypatch.setattr(ob, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(ob, "_get_portfolio_summary_impl", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ob,
        "collect_pending_orders_snapshot",
        AsyncMock(
            return_value=SimpleNamespace(
                orders=[], as_of=None, freshness_status="test", unavailable_reason=None
            )
        ),
    )
    for name in (
        "_recent_session_context",
        "_recent_analysis_artifacts",
        "list_active_watches_impl",
    ):
        monkeypatch.setattr(ob, name, AsyncMock(return_value={}))
    monkeypatch.setattr(ob, "_latest_report_summary", AsyncMock(return_value=None))
    monkeypatch.setattr(ob, "get_account_costs_setting", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ob,
        "load_negative_class_health",
        AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {})),
    )
    from app.services.trade_journal import aggregates

    monkeypatch.setattr(
        aggregates, "build_trading_scoreboard", AsyncMock(return_value={})
    )


@pytest.mark.asyncio
async def test_stale_list_is_visible_to_kr_session(briefing_stubs, monkeypatch):
    health = {"status": "stale", "requires_attention": True, "row_count": 2766}
    loader = AsyncMock(return_value=health)
    monkeypatch.setattr(ob, "load_krx_after_market_health", loader)
    result = await ob.get_operating_briefing_impl(market="kr")
    assert result["success"] is True
    assert result["staleness"]["krx_after_market_eligibility"] == health
    loader.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_probe_is_attention_without_raw_error(briefing_stubs, monkeypatch):
    monkeypatch.setattr(
        ob,
        "load_krx_after_market_health",
        AsyncMock(side_effect=RuntimeError("private diagnostic detail")),
    )
    result = await ob.get_operating_briefing_impl(market="kr")
    assert result["success"] is True
    section = result["staleness"]["krx_after_market_eligibility"]
    assert section["status"] == "unavailable"
    assert section["requires_attention"] is True
    assert "private diagnostic detail" not in str(result)


@pytest.mark.parametrize("market", ["us", "crypto"])
@pytest.mark.asyncio
async def test_other_markets_do_not_query_krx(briefing_stubs, monkeypatch, market):
    loader = AsyncMock()
    monkeypatch.setattr(ob, "load_krx_after_market_health", loader)
    result = await ob.get_operating_briefing_impl(market=market)
    assert "krx_after_market_eligibility" not in result["staleness"]
    loader.assert_not_called()
