from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from tests._mcp_tooling_support import _patch_runtime_attr, build_tools

_CORRELATION_COMPANY_NAME_ERROR = (
    "get_correlation does not support company-name inputs because it has no "
    "market parameter. Use ticker/code inputs directly."
)


def _price_frame(closes: Sequence[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=len(closes), freq="D").date,
            "close": list(closes),
        }
    )


def _fetch_mock_for_symbols(
    payloads: dict[str, pd.DataFrame | Exception],
) -> AsyncMock:
    async def _fetch(symbol: str, market_type: str, count: int) -> pd.DataFrame:
        del market_type, count
        payload = payloads[symbol]
        if isinstance(payload, Exception):
            raise payload
        return payload

    return AsyncMock(side_effect=_fetch)


@pytest.mark.asyncio
async def test_get_correlation_supports_mixed_kr_and_us_symbols(monkeypatch):
    tools = build_tools()
    fetch_mock = _fetch_mock_for_symbols(
        {
            "005930": _price_frame([100.0, 101.0, 102.0, 103.0]),
            "AAPL": _price_frame([200.0, 202.0, 204.0, 206.0]),
        }
    )
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch_mock)

    result = await tools["get_correlation"](["005930", "AAPL"], period=45)

    assert result["success"] is True
    assert result["symbols"] == ["005930", "AAPL"]
    assert result["metadata"]["period_days"] == 45
    assert result["metadata"]["market_types"] == {
        "005930": "equity_kr",
        "AAPL": "equity_us",
    }
    assert result["metadata"]["sources"] == {
        "005930": "kis",
        "AAPL": "yahoo",
    }
    assert result["correlation_matrix"] == [[1.0, 1.0], [1.0, 1.0]]


@pytest.mark.asyncio
async def test_get_correlation_rejects_korean_company_name_with_validation_error(
    monkeypatch,
):
    tools = build_tools()
    fetch_mock = _fetch_mock_for_symbols(
        {"AAPL": _price_frame([200.0, 201.0, 202.0, 203.0])}
    )
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch_mock)

    result = await tools["get_correlation"](["삼성전자", "AAPL"])

    assert result == {
        "success": False,
        "error": _CORRELATION_COMPANY_NAME_ERROR,
        "errors": [
            f"삼성전자: {_CORRELATION_COMPANY_NAME_ERROR}",
        ],
    }
    fetch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_correlation_rejects_us_company_name_with_validation_error(
    monkeypatch,
):
    tools = build_tools()
    fetch_mock = _fetch_mock_for_symbols(
        {"005930": _price_frame([100.0, 99.0, 98.0, 97.0])}
    )
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch_mock)

    result = await tools["get_correlation"](["Apple Inc.", "005930"])

    assert result == {
        "success": False,
        "error": _CORRELATION_COMPANY_NAME_ERROR,
        "errors": [
            f"Apple Inc.: {_CORRELATION_COMPANY_NAME_ERROR}",
        ],
    }
    fetch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_correlation_keeps_partial_success_with_invalid_company_name(
    monkeypatch,
):
    tools = build_tools()
    fetch_mock = _fetch_mock_for_symbols(
        {
            "005930": _price_frame([100.0, 101.0, 102.0, 103.0]),
            "AAPL": _price_frame([300.0, 301.0, 302.0, 303.0]),
        }
    )
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch_mock)

    result = await tools["get_correlation"](["005930", "AAPL", "삼성전자"])

    assert result["success"] is True
    assert result["symbols"] == ["005930", "AAPL"]
    assert result["errors"] == [
        f"삼성전자: {_CORRELATION_COMPANY_NAME_ERROR}",
    ]
    assert result["correlation_matrix"] == [[1.0, 1.0], [1.0, 1.0]]


@pytest.mark.asyncio
async def test_get_correlation_keeps_generic_failure_for_non_company_resolution_errors(
    monkeypatch,
):
    tools = build_tools()
    fetch_mock = _fetch_mock_for_symbols(
        {"AAPL": _price_frame([200.0, 201.0, 202.0, 203.0])}
    )
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch_mock)

    result = await tools["get_correlation"](["!@#$", "AAPL"])

    assert result == {
        "success": False,
        "error": "Insufficient data to calculate correlation (need at least 2 symbols)",
    }
    fetch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_correlation_keeps_generic_failure_for_downstream_fetch_errors(
    monkeypatch,
):
    tools = build_tools()
    fetch_mock = _fetch_mock_for_symbols(
        {
            "005930": _price_frame([100.0, 101.0, 102.0, 103.0]),
            "AAPL": RuntimeError("yahoo down"),
        }
    )
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch_mock)

    result = await tools["get_correlation"](["005930", "AAPL"])

    assert result == {
        "success": False,
        "error": "Insufficient data to calculate correlation (need at least 2 symbols)",
    }


@pytest.mark.asyncio
async def test_get_correlation_preserves_existing_market_inference(monkeypatch):
    tools = build_tools()
    fetch_mock = _fetch_mock_for_symbols(
        {
            "005930": _price_frame([100.0, 101.0, 102.0, 103.0]),
            "AAPL": _price_frame([200.0, 202.0, 204.0, 206.0]),
            "KRW-BTC": _price_frame([400.0, 398.0, 396.0, 394.0]),
        }
    )
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch_mock)

    result = await tools["get_correlation"](["005930", "AAPL", "KRW-BTC"])

    assert result["success"] is True
    assert result["symbols"] == ["005930", "AAPL", "KRW-BTC"]
    assert result["metadata"]["market_types"] == {
        "005930": "equity_kr",
        "AAPL": "equity_us",
        "KRW-BTC": "crypto",
    }
    assert result["metadata"]["sources"] == {
        "005930": "kis",
        "AAPL": "yahoo",
        "KRW-BTC": "upbit",
    }
    assert len(result["correlation_matrix"]) == 3
    assert all(len(row) == 3 for row in result["correlation_matrix"])
