import pytest

import app.mcp_server.tooling.trading_scoreboard_tools as tool


@pytest.mark.asyncio
async def test_scoreboard_tool_shape_hermetic(monkeypatch):
    async def fake_board(db, **kw):
        return {
            "groups": [],
            "overall": None,
            "as_of": "x",
            "count": 0,
            "cohort": kw.get("cohort"),
        }

    monkeypatch.setattr(tool, "build_trading_scoreboard", fake_board)
    result = await tool.get_trading_scoreboard(cohort="mock_counterfactual")
    assert set(result) >= {"groups", "overall", "as_of", "count", "cohort"}
    assert result["cohort"] == "mock_counterfactual"


@pytest.mark.asyncio
async def test_scoreboard_tool_calls_counterfactual_delta(monkeypatch):
    seen = {}

    async def fake_delta(db, **kw):
        seen.update(kw)
        return {
            "paired_count": 5,
            "overall_delta": {},
            "pairing_health": {"status": "ok"},
            "pairing_diagnostics": {},
            "caveats": [],
        }

    monkeypatch.setattr(tool, "build_counterfactual_delta_scoreboard", fake_delta)
    result = await tool.get_trading_scoreboard(
        market="kr",
        account_mode="kis_mock",
        setup_tag="breakout",
        min_sample=3,
        min_pair_threshold=7,
        include_counterfactual_delta=True,
    )

    assert result["paired_count"] == 5
    assert seen["market"] == "kr"
    assert seen["account_mode"] == "kis_mock"
    assert seen["setup_tag"] == "breakout"
    assert seen["min_sample"] == 3
    assert seen["min_pair_threshold"] == 7
