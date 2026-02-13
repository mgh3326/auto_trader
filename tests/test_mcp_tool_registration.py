import pytest

from app.mcp_server import AVAILABLE_TOOL_NAMES, register_all_tools
from app.mcp_server.tooling.analysis_screening import (
    ANALYSIS_TOOL_NAMES,
    register_analysis_tools,
)
from app.mcp_server.tooling.fundamentals import (
    FUNDAMENTALS_TOOL_NAMES,
    register_fundamentals_tools,
)
from app.mcp_server.tooling.market_data import (
    MARKET_DATA_TOOL_NAMES,
    register_market_data_tools,
)
from app.mcp_server.tooling.orders import (
    ORDER_TOOL_NAMES,
    register_order_tools,
)
from app.mcp_server.tooling.portfolio import (
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

    assert set(mcp.tools) == set(AVAILABLE_TOOL_NAMES)


@pytest.mark.parametrize(
    "registrar",
    [
        register_market_data_tools,
        register_portfolio_tools,
        register_order_tools,
        register_fundamentals_tools,
        register_analysis_tools,
    ],
)
def test_domain_registration_is_idempotent(registrar: object) -> None:
    mcp = DummyMCP()

    registrar(mcp)
    first_count = len(mcp.tools)
    registrar(mcp)

    assert len(mcp.tools) == first_count
