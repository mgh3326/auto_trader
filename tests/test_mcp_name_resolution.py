from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from app.mcp_server.tooling.name_resolution import resolve_names
import app.mcp_server.tooling.name_resolution as name_resolution

@pytest.mark.asyncio
async def test_resolve_names_kr(monkeypatch):
    mock_get_kr = AsyncMock(return_value={"005930": "삼성전자"})
    monkeypatch.setattr(name_resolution, "get_kr_names_by_symbols", mock_get_kr)

    result = await resolve_names(["005930", "000660"], "equity_kr")
    assert result == {
        "005930": {"name": "삼성전자", "name_resolved": True},
        "000660": {"name": "000660", "name_resolved": False},
    }
    mock_get_kr.assert_called_once_with(["005930", "000660"])

@pytest.mark.asyncio
async def test_resolve_names_us(monkeypatch):
    mock_get_us = AsyncMock(return_value={"AAPL": "Apple Inc."})
    monkeypatch.setattr(name_resolution, "get_us_names_by_symbols", mock_get_us)

    result = await resolve_names(["AAPL", "MSFT"], "equity_us")
    assert result == {
        "AAPL": {"name": "Apple Inc.", "name_resolved": True},
        "MSFT": {"name": "MSFT", "name_resolved": False},
    }
    mock_get_us.assert_called_once_with(["AAPL", "MSFT"])

@pytest.mark.asyncio
async def test_resolve_names_crypto(monkeypatch):
    mock_get_upbit = AsyncMock(return_value={"KRW-BTC": {"korean_name": "비트코인", "english_name": "Bitcoin"}})
    monkeypatch.setattr(name_resolution, "get_upbit_market_display_names", mock_get_upbit)

    result = await resolve_names(["KRW-BTC", "KRW-ETH"], "crypto")
    assert result == {
        "KRW-BTC": {"name": "비트코인", "name_resolved": True},
        "KRW-ETH": {"name": "KRW-ETH", "name_resolved": False},
    }
    mock_get_upbit.assert_called_once_with(["KRW-BTC", "KRW-ETH"])

@pytest.mark.asyncio
async def test_resolve_names_fallback_on_exception(monkeypatch):
    mock_get_kr = AsyncMock(side_effect=RuntimeError("Database connection failed"))
    monkeypatch.setattr(name_resolution, "get_kr_names_by_symbols", mock_get_kr)

    result = await resolve_names(["005930"], "equity_kr")
    assert result == {
        "005930": {"name": "005930", "name_resolved": False},
    }
