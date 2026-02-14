from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import analysis_tool_handlers
from app.mcp_server.tooling.registry import register_all_tools


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        _ = description

        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    register_all_tools(mcp)
    return mcp.tools


@pytest.mark.asyncio
async def test_get_disclosures_strips_wrapping_quotes(monkeypatch):
    tools = build_tools()
    list_filings_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(analysis_tool_handlers, "list_filings", list_filings_mock)

    result = await tools["get_disclosures"](symbol="'005930'", days=30, limit=5)

    assert result["success"] is True
    assert result["filings"] == []
    list_filings_mock.assert_awaited_once_with("005930", 30, 5, None)


@pytest.mark.asyncio
async def test_get_disclosures_wraps_list_result(monkeypatch):
    tools = build_tools()
    list_filings_mock = AsyncMock(
        return_value=[{"date": "2026-02-13", "report_nm": "기타경영사항(자율공시)"}]
    )
    monkeypatch.setattr(analysis_tool_handlers, "list_filings", list_filings_mock)

    result = await tools["get_disclosures"](symbol="삼성전자", days=30, limit=5)

    assert result["success"] is True
    assert len(result["filings"]) == 1


@pytest.mark.asyncio
async def test_get_disclosures_passes_through_error_dict(monkeypatch):
    tools = build_tools()
    expected = {
        "success": False,
        "error_code": "symbol_not_resolved",
        "error": "Cannot resolve symbol",
        "filings": [],
    }
    list_filings_mock = AsyncMock(return_value=expected)
    monkeypatch.setattr(analysis_tool_handlers, "list_filings", list_filings_mock)

    result = await tools["get_disclosures"](symbol="없는회사", days=30, limit=5)

    assert result == expected
