# tests/test_mcp_news_symbol_mapping_tool.py
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server import AVAILABLE_TOOL_NAMES
from app.mcp_server.tooling.news_handlers import NEWS_TOOL_NAMES
from tests._mcp_tooling_support import build_tools

NOW = datetime(2026, 6, 9, 3, 0, tzinfo=UTC)


@pytest.mark.unit
def test_tool_registered():
    assert "get_symbol_news_mapping" in NEWS_TOOL_NAMES
    assert "get_symbol_news_mapping" in AVAILABLE_TOOL_NAMES
    tools = build_tools()
    assert "get_symbol_news_mapping" in tools


@pytest.mark.asyncio
@pytest.mark.unit
async def test_tool_invokes_handler():
    tools = build_tools()
    fake_resp = {
        "symbol": "035420",
        "market": "kr",
        "data_state": "fresh",
        "latest_as_of": None,
        "articles": [],
        "warnings": [],
    }
    with patch(
        "app.mcp_server.tooling.news_symbol_mapping.handle_get_symbol_news_mapping",
        new=AsyncMock(return_value=fake_resp),
    ) as mock_handle:
        result = await tools["get_symbol_news_mapping"](symbol="035420", market="kr")
    assert result["symbol"] == "035420"
    mock_handle.assert_awaited_once()
