"""ROB-1286 §101차 ④ — the watch-repricing MCP profile (closed world).

The session a repricing tick spawns gets *this* profile and nothing else.
Registration is allowlist-filtered and returns before the broad "Always"
block in :func:`~app.mcp_server.tooling.registry.register_all_tools`, so
research, settings, watch-mutation, reconcile, preview and every broker
order surface are **physically absent** rather than merely unused.

Why a real profile and not a list
---------------------------------
r2's B4 finding: the previous round validated the tool list carried on the
spawn request, while nothing provisioned a session from it. A stand-in that
requested a clean profile and granted ``toss_place_order`` passed. A profile
that a server is actually built from turns the list into the thing that
decides what exists, and :func:`watch_repricing_tool_names` lets the
orchestrator compare the *provisioned* registry with the allowlist by
closed equality.

The set is deliberately the same object as
:data:`app.services.watch_trigger_repricing.capability.PROPOSAL_ONLY_TOOLS`
-- one definition, two consumers -- so the profile and the capability
boundary cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from app.mcp_server.tooling.account_routing_registration import (
    register_account_routing_tools,
)
from app.mcp_server.tooling.analysis_registration import register_analysis_tools
from app.mcp_server.tooling.fundamentals_registration import register_fundamentals_tools
from app.mcp_server.tooling.investment_reports_handlers import (
    register_investment_report_tools,
)
from app.mcp_server.tooling.market_data_registration import register_market_data_tools
from app.mcp_server.tooling.operating_briefing_registration import (
    register_operating_briefing_tools,
)
from app.mcp_server.tooling.order_proposal_tools import register_order_proposal_tools
from app.mcp_server.tooling.portfolio_registration import register_portfolio_tools
from app.mcp_server.tooling.route_request_registration import (
    register_route_request_tools,
)
from app.mcp_server.tooling.trading_policy_registration import (
    register_trading_policy_tools,
)
from app.services.watch_trigger_repricing.capability import PROPOSAL_ONLY_TOOLS

if TYPE_CHECKING:
    from fastmcp import FastMCP

_F = TypeVar("_F", bound=Callable[..., Any])

__all__ = [
    "WATCH_REPRICING_TOOL_NAMES",
    "register_watch_repricing_tools",
    "watch_repricing_tool_names",
]

# One definition shared with the capability boundary.
WATCH_REPRICING_TOOL_NAMES: frozenset[str] = PROPOSAL_ONLY_TOOLS


class _AllowlistedMCP:
    """Proxy so existing group registrars physically register only allowed names."""

    def __init__(self, inner: Any, allowed_names: frozenset[str]) -> None:
        self._inner = inner
        self._allowed_names = allowed_names

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[_F], _F]:
        name = kwargs.get("name")
        if name is None and args:
            name = args[0]
        if str(name) in self._allowed_names:
            return cast(Callable[[_F], _F], self._inner.tool(*args, **kwargs))

        def decorator(func: _F) -> _F:
            return func

        return decorator

    def list_tools(self) -> Any:
        lister = getattr(self._inner, "list_tools", None)
        return [] if lister is None else lister()


def register_watch_repricing_tools(mcp: FastMCP) -> None:
    """Register exactly the proposal-only surface."""
    filtered = cast("FastMCP", _AllowlistedMCP(mcp, WATCH_REPRICING_TOOL_NAMES))
    register_market_data_tools(filtered)
    register_analysis_tools(filtered)
    register_fundamentals_tools(filtered)
    register_portfolio_tools(filtered)
    register_account_routing_tools(filtered)
    register_route_request_tools(filtered)
    register_trading_policy_tools(filtered)
    register_operating_briefing_tools(filtered)
    register_investment_report_tools(filtered, include_snapshot_generator=False)
    register_order_proposal_tools(filtered)


def watch_repricing_tool_names(mcp: Any) -> frozenset[str]:
    """The names a built server actually exposes, for the exact comparison."""
    tools = getattr(mcp, "tools", None)
    if tools is None:
        raise TypeError("server object exposes no tool registry to attest")
    return frozenset(tools)
