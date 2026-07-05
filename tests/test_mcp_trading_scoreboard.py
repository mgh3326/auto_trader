import pytest

import app.mcp_server.tooling.trading_scoreboard_tools as tool


@pytest.mark.asyncio
async def test_scoreboard_tool_shape_hermetic(monkeypatch):
    async def fake_board(db, **kw):
        return {"groups": [], "overall": None, "as_of": "x", "count": 0}

    monkeypatch.setattr(tool, "build_trading_scoreboard", fake_board)
    result = await tool.get_trading_scoreboard()
    assert set(result) >= {"groups", "overall", "as_of", "count"}
    assert result["groups"] == []
    assert result["count"] == 0
