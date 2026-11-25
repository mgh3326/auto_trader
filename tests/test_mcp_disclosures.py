import inspect
import json
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp_server.tooling import analysis_tool_handlers as handlers
from app.mcp_server.tooling.analysis_registration import register_analysis_tools


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}
        self.descriptions: dict[str, str] = {}

    def tool(self, name: str, description: str):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = func
            self.descriptions[name] = description
            return func

        return decorator


class TestGetDisclosuresImpl:
    @pytest.mark.asyncio
    async def test_passes_stock_code_directly_to_service(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        async def mock_list_filings(
            symbol: str,
            days: int,
            limit: int,
            report_type: str | None,
        ) -> dict[str, object]:
            captured.update(
                {
                    "symbol": symbol,
                    "days": days,
                    "limit": limit,
                    "report_type": report_type,
                }
            )
            return {"success": True, "filings": []}

        monkeypatch.setattr(handlers, "list_filings", mock_list_filings)

        result = await handlers.get_disclosures_impl(
            symbol="005930",
            days=30,
            limit=3,
            report_type="정기",
        )

        assert result == {"success": True, "filings": []}
        assert captured == {
            "symbol": "005930",
            "days": 30,
            "limit": 3,
            "report_type": "정기",
        }

    @pytest.mark.asyncio
    async def test_passes_company_name_directly_to_service(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[str] = []

        async def mock_list_filings(
            symbol: str,
            days: int,
            limit: int,
            report_type: str | None,
        ) -> dict[str, object]:
            captured.append(symbol)
            return {"success": True, "filings": []}

        monkeypatch.setattr(handlers, "list_filings", mock_list_filings)

        result = await handlers.get_disclosures_impl(symbol="삼성전자", days=7, limit=2)

        assert result == {"success": True, "filings": []}
        assert captured == ["삼성전자"]

    @pytest.mark.asyncio
    async def test_service_error_is_returned_without_handler_translation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def mock_list_filings(
            symbol: str,
            days: int,
            limit: int,
            report_type: str | None,
        ) -> dict[str, object]:
            return {
                "success": False,
                "error": 'could not find "없는회사"',
                "filings": [],
                "symbol": symbol,
            }

        monkeypatch.setattr(handlers, "list_filings", mock_list_filings)

        result = await handlers.get_disclosures_impl(symbol="없는회사", days=5, limit=1)

        assert result == {
            "success": False,
            "error": 'could not find "없는회사"',
            "filings": [],
            "symbol": "없는회사",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("symbol", "normalized_symbol"),
        [("", ""), ("   ", "")],
    )
    async def test_blank_symbol_error_payload_is_passed_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
        symbol: str,
        normalized_symbol: str,
    ) -> None:
        async def mock_list_filings(
            symbol: str,
            days: int,
            limit: int,
            report_type: str | None,
        ) -> dict[str, object]:
            del days, limit, report_type
            assert symbol == normalized_symbol or symbol.strip() == normalized_symbol
            return {
                "success": False,
                "error": "symbol is required",
                "filings": [],
                "symbol": normalized_symbol,
            }

        monkeypatch.setattr(handlers, "list_filings", mock_list_filings)

        result = await handlers.get_disclosures_impl(symbol=symbol, days=5, limit=1)

        assert result == {
            "success": False,
            "error": "symbol is required",
            "filings": [],
            "symbol": normalized_symbol,
        }

    @pytest.mark.asyncio
    async def test_missing_service_returns_generic_dart_unavailable_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(handlers, "list_filings", None)

        result = await handlers.get_disclosures_impl(symbol="005930", days=30)

        assert result == {
            "success": False,
            "error": "DART functionality not available",
            "filings": [],
            "symbol": "005930",
        }


def test_registers_get_disclosures_public_contract() -> None:
    mcp = DummyMCP()

    register_analysis_tools(cast(Any, mcp))

    tool = mcp.tools["get_disclosures"]
    signature = inspect.signature(tool)

    assert list(signature.parameters) == [
        "symbol",
        "days",
        "limit",
        "report_type",
        "include_values",
    ]
    assert signature.parameters["include_values"].default is False
    assert signature.parameters["days"].default == 30
    assert signature.parameters["limit"].default == 20
    assert signature.parameters["report_type"].default is None
    assert "direct 6-digit stock-code inputs" in mcp.descriptions["get_disclosures"]


@pytest.mark.asyncio
async def test_mcp_disclosure_default_bytes_and_opt_in_values(monkeypatch):
    from app.services.market_events.query_service import MarketEventsQueryService

    golden = (
        '{"success":true,"filings":[{"date":"2026-11-24","report_nm":"CB",'
        '"rcp_no":"20261124000001","corp_name":"Test"}],"symbol":"005930"}'
    )
    monkeypatch.setattr(
        handlers, "list_filings", AsyncMock(return_value=json.loads(golden))
    )
    session = MagicMock()
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", session)
    values = [{"metric_name": "worst_case_dilution", "actual": "12.5", "unit": "%"}]
    read = AsyncMock(return_value={"20261124000001": values})
    monkeypatch.setattr(MarketEventsQueryService, "dart_values_by_receipt", read)
    mcp = DummyMCP()
    register_analysis_tools(cast(Any, mcp))
    tool = mcp.tools["get_disclosures"]

    assert json.dumps(await tool("005930"), separators=(",", ":")) == golden
    session.assert_not_called()
    read.assert_not_awaited()
    enriched = await tool("005930", include_values=True)
    assert enriched["filings"] == [
        {**json.loads(golden)["filings"][0], "values": values}
    ]
    read.assert_awaited_once_with(["20261124000001"])
