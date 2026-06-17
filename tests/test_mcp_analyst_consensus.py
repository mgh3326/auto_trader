from __future__ import annotations

import pytest

from app.mcp_server.tooling.fundamentals import _analyst_consensus as mod
from app.mcp_server.tooling.fundamentals._valuation import (
    handle_get_investment_opinions,
)
from app.mcp_server.tooling.fundamentals_handlers import FUNDAMENTALS_TOOL_NAMES

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_handle_get_analyst_consensus_ok(monkeypatch):
    async def mock_fetch_analyst_consensus(code: str):
        assert code == "005930"
        return {
            "recomm_mean": 4.2,
            "price_target_mean": 92000,
            "warnings": [],
        }

    monkeypatch.setattr(mod, "fetch_analyst_consensus", mock_fetch_analyst_consensus)

    # 6-digit stock code
    res = await mod.handle_get_analyst_consensus("005930")
    assert res["status"] == "ok"
    assert res["recomm_mean"] == 4.2
    assert res["price_target_mean"] == 92000
    assert res["symbol"] == "005930"
    assert res["source"] == "naver_integration"

    # A-prefixed 7-digit code
    res_a = await mod.handle_get_analyst_consensus("A005930")
    assert res_a["status"] == "ok"
    assert res_a["recomm_mean"] == 4.2
    assert res_a["price_target_mean"] == 92000
    assert res_a["symbol"] == "A005930"
    assert res_a["source"] == "naver_integration"


@pytest.mark.asyncio
async def test_handle_get_analyst_consensus_error(monkeypatch):
    async def mock_fetch_error(code: str):
        raise RuntimeError("Naver API down")

    monkeypatch.setattr(mod, "fetch_analyst_consensus", mock_fetch_error)

    res = await mod.handle_get_analyst_consensus("005930")
    assert "error" in res
    assert "Naver API down" in res["error"]
    assert res["source"] == "naver_integration"


@pytest.mark.asyncio
async def test_handle_get_analyst_consensus_validation():
    with pytest.raises(ValueError, match="only available for Korean stocks"):
        await mod.handle_get_analyst_consensus("AAPL")


def test_tool_collision_prevention():
    assert "get_investment_opinions" in FUNDAMENTALS_TOOL_NAMES
    assert "get_analyst_consensus" in FUNDAMENTALS_TOOL_NAMES
    assert mod.handle_get_analyst_consensus is not handle_get_investment_opinions
