from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import market_quote_snapshot_tools as quote_tools
from app.mcp_server.tooling.market_quote_snapshot_tools import (
    MARKET_QUOTE_SNAPSHOT_MUTATION_TOOL_NAMES,
    MARKET_QUOTE_SNAPSHOT_READONLY_TOOL_NAMES,
    US_FORECAST_MARKET_QUOTE_SNAPSHOT_TOOL_NAMES,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.route_request_lanes import (
    DB_PERSISTENCE_MUTATION_TOOLS,
    MUTATION_TOOLS,
    READ_ONLY_ADVISORY_TOOLS,
)
from app.models.trading import InstrumentType
from tests._mcp_tooling_support import DummyMCP, DummySessionManager

_TOOL_NAME = "us_forecast_market_quote_snapshot_ensure"
_FORECAST_ID = uuid.UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_CORRELATION_ID = "directional-lab:us:2026-07-30:AAPL"


def _forecast(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "forecast_id": _FORECAST_ID,
        "status": "open",
        "created_by": "directional-lab",
        "instrument_type": InstrumentType.equity_us,
        "correlation_id": _CORRELATION_ID,
        "symbol": "AAPL",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_forecast_lookup(
    monkeypatch: pytest.MonkeyPatch,
    forecast: SimpleNamespace | None,
) -> AsyncMock:
    monkeypatch.setattr(
        quote_tools,
        "AsyncSessionLocal",
        lambda: DummySessionManager(object()),
    )
    lookup = AsyncMock(return_value=forecast)
    monkeypatch.setattr(quote_tools, "get_forecast", lookup)
    return lookup


@pytest.mark.unit
@pytest.mark.asyncio
async def test_real_fastmcp_schema_exposes_only_forecast_binding_inputs() -> None:
    mcp = FastMCP(name="us-forecast-quote-schema", on_duplicate="error")
    register_all_tools(mcp, profile=McpProfile.US_PAPER)

    tool = await mcp.get_tool(_TOOL_NAME)
    schema = tool.parameters or {}

    assert schema.get("additionalProperties") is False
    assert set((schema.get("properties") or {}).keys()) == {
        "forecast_id",
        "correlation_id",
    }
    assert set(schema.get("required") or []) == {"forecast_id", "correlation_id"}
    for field_name in ("forecast_id", "correlation_id"):
        field = schema["properties"][field_name]
        assert field["minLength"] == 1
        assert field["pattern"] == r".*\S.*"
    for forbidden in {"market", "symbol", "price", "source", "profile"}:
        assert forbidden not in (schema.get("properties") or {})


@pytest.mark.unit
@pytest.mark.parametrize(
    "profile", [p for p in McpProfile if p is not McpProfile.US_PAPER]
)
def test_dedicated_tool_is_physically_absent_outside_us_paper(
    monkeypatch: pytest.MonkeyPatch,
    profile: McpProfile,
) -> None:
    monkeypatch.setattr(
        quote_tools,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("must not access DB")),
    )
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=profile)
    assert US_FORECAST_MARKET_QUOTE_SNAPSHOT_TOOL_NAMES.isdisjoint(mcp.tools)


@pytest.mark.unit
def test_dedicated_tool_stays_absent_from_default_when_alpaca_gate_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling import registry as registry_mod

    monkeypatch.setattr(
        registry_mod.settings,
        "alpaca_paper_default_tools_enabled",
        True,
        raising=False,
    )
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert _TOOL_NAME not in mcp.tools


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrong_runtime_profile_fails_before_db_or_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        quote_tools,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("must not access DB")),
    )
    build = AsyncMock()
    monkeypatch.setattr(quote_tools, "market_quote_snapshot_ensure", build)

    result = await quote_tools._ensure_us_forecast_market_quote_snapshot(
        str(_FORECAST_ID),
        _CORRELATION_ID,
        runtime_profile=McpProfile.DEFAULT,
    )

    assert result["success"] is False
    assert result["error"] == "wrong_mcp_profile"
    build.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("forecast", "correlation_id", "expected_error"),
    [
        (None, _CORRELATION_ID, "forecast_not_found"),
        (_forecast(status="closed"), _CORRELATION_ID, "forecast_not_open"),
        (
            _forecast(created_by="operator"),
            _CORRELATION_ID,
            "forecast_created_by_mismatch",
        ),
        (
            _forecast(instrument_type=InstrumentType.equity_kr),
            _CORRELATION_ID,
            "forecast_instrument_type_mismatch",
        ),
        (
            _forecast(),
            "directional-lab:us:2026-07-30:MSFT",
            "forecast_correlation_mismatch",
        ),
        (_forecast(symbol="KRW-BTC"), _CORRELATION_ID, "invalid_forecast_symbol"),
    ],
)
async def test_forecast_binding_failures_stop_before_snapshot_build(
    monkeypatch: pytest.MonkeyPatch,
    forecast: SimpleNamespace | None,
    correlation_id: str,
    expected_error: str,
) -> None:
    _patch_forecast_lookup(monkeypatch, forecast)
    build = AsyncMock()
    monkeypatch.setattr(quote_tools, "market_quote_snapshot_ensure", build)

    result = await quote_tools._ensure_us_forecast_market_quote_snapshot(
        str(_FORECAST_ID),
        correlation_id,
        runtime_profile=McpProfile.US_PAPER,
    )

    assert result["success"] is False
    assert result["error"] == expected_error
    build.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_derives_us_market_and_symbol_from_persisted_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_forecast_lookup(monkeypatch, _forecast(symbol="aapl"))
    ensure = AsyncMock(
        return_value={
            "success": True,
            "market": "us",
            "symbol": "AAPL",
            "id": 42,
            "reused": True,
        }
    )
    monkeypatch.setattr(quote_tools, "market_quote_snapshot_ensure", ensure)

    result = await quote_tools._ensure_us_forecast_market_quote_snapshot(
        str(_FORECAST_ID),
        _CORRELATION_ID,
        runtime_profile=McpProfile.US_PAPER,
    )

    ensure.assert_awaited_once_with(market="us", symbol="AAPL")
    assert result["success"] is True
    assert result["market"] == "us"
    assert result["symbol"] == "AAPL"
    assert result["forecast_id"] == str(_FORECAST_ID)
    assert result["correlation_id"] == _CORRELATION_ID
    assert result["forecast_bound"] is True


@pytest.mark.unit
def test_quote_ensure_tools_are_db_mutations_while_latest_stays_read_only() -> None:
    assert MARKET_QUOTE_SNAPSHOT_MUTATION_TOOL_NAMES == {
        "market_quote_snapshot_ensure",
        _TOOL_NAME,
    }
    assert MARKET_QUOTE_SNAPSHOT_MUTATION_TOOL_NAMES == DB_PERSISTENCE_MUTATION_TOOLS
    assert MARKET_QUOTE_SNAPSHOT_MUTATION_TOOL_NAMES <= MUTATION_TOOLS
    assert MARKET_QUOTE_SNAPSHOT_MUTATION_TOOL_NAMES.isdisjoint(
        READ_ONLY_ADVISORY_TOOLS
    )
    assert MARKET_QUOTE_SNAPSHOT_READONLY_TOOL_NAMES == {"market_quote_snapshot_latest"}
    assert MARKET_QUOTE_SNAPSHOT_READONLY_TOOL_NAMES <= READ_ONLY_ADVISORY_TOOLS
