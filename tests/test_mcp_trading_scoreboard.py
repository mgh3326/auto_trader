import pytest

from app.mcp_server.tooling.trading_scoreboard_tools import get_trading_scoreboard


@pytest.mark.asyncio
async def test_scoreboard_tool_empty_db_shape():
    result = await get_trading_scoreboard()
    assert set(result) >= {"groups", "overall", "as_of", "count"}
    assert result["count"] == 0
    assert result["groups"] == []
