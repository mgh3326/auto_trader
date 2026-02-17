import pytest

from app.mcp_server import AVAILABLE_TOOL_NAMES, register_all_tools
from app.mcp_server.tooling import (
    WATCH_ALERT_TOOL_NAMES,
    register_watch_alert_tools,
)
from app.mcp_server.tooling.analysis_registration import (
    ANALYSIS_TOOL_NAMES,
    register_analysis_tools,
)
from app.mcp_server.tooling.fundamentals_registration import (
    FUNDAMENTALS_TOOL_NAMES,
    register_fundamentals_tools,
)
from app.mcp_server.tooling.market_data_registration import (
    MARKET_DATA_TOOL_NAMES,
    register_market_data_tools,
)
from app.mcp_server.tooling.orders_registration import (
    ORDER_TOOL_NAMES,
    register_order_tools,
)
from app.mcp_server.tooling.portfolio_registration import (
    PORTFOLIO_TOOL_NAMES,
    register_portfolio_tools,
)


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        _ = description

        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def test_register_all_tools_registers_all_available_tools() -> None:
    mcp = DummyMCP()

    register_all_tools(mcp)

    assert set(mcp.tools) == set(AVAILABLE_TOOL_NAMES)


def test_removed_dca_tools_are_not_registered() -> None:
    mcp = DummyMCP()

    register_all_tools(mcp)

    assert "create_dca_plan" not in mcp.tools
    assert "get_dca_status" not in mcp.tools


def test_available_tool_names_exclude_removed_dca_tools() -> None:
    assert "create_dca_plan" not in AVAILABLE_TOOL_NAMES
    assert "get_dca_status" not in AVAILABLE_TOOL_NAMES


def test_domain_registration_is_incremental_and_recoverable() -> None:
    mcp = DummyMCP()

    register_market_data_tools(mcp)
    assert set(mcp.tools) == MARKET_DATA_TOOL_NAMES

    register_portfolio_tools(mcp)
    assert set(mcp.tools) == MARKET_DATA_TOOL_NAMES | PORTFOLIO_TOOL_NAMES

    register_order_tools(mcp)
    assert set(mcp.tools) == (
        MARKET_DATA_TOOL_NAMES | PORTFOLIO_TOOL_NAMES | ORDER_TOOL_NAMES
    )

    register_fundamentals_tools(mcp)
    assert set(mcp.tools) == (
        MARKET_DATA_TOOL_NAMES
        | PORTFOLIO_TOOL_NAMES
        | ORDER_TOOL_NAMES
        | FUNDAMENTALS_TOOL_NAMES
    )

    register_analysis_tools(mcp)
    assert set(mcp.tools) == (
        MARKET_DATA_TOOL_NAMES
        | PORTFOLIO_TOOL_NAMES
        | ORDER_TOOL_NAMES
        | FUNDAMENTALS_TOOL_NAMES
        | ANALYSIS_TOOL_NAMES
    )

    register_watch_alert_tools(mcp)
    assert set(mcp.tools) == (
        MARKET_DATA_TOOL_NAMES
        | PORTFOLIO_TOOL_NAMES
        | ORDER_TOOL_NAMES
        | FUNDAMENTALS_TOOL_NAMES
        | ANALYSIS_TOOL_NAMES
        | WATCH_ALERT_TOOL_NAMES
    )

    assert set(mcp.tools) == set(AVAILABLE_TOOL_NAMES)


@pytest.mark.parametrize(
    "registrar",
    [
        register_market_data_tools,
        register_portfolio_tools,
        register_order_tools,
        register_fundamentals_tools,
        register_analysis_tools,
        register_watch_alert_tools,
    ],
)
def test_domain_registration_is_idempotent(registrar: object) -> None:
    mcp = DummyMCP()

    registrar(mcp)
    first_count = len(mcp.tools)
    registrar(mcp)

    assert len(mcp.tools) == first_count
