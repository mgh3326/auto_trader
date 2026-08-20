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
decides what exists, and :func:`provisioned_tool_names` lets the
orchestrator compare the *provisioned* registry with the allowlist by
closed equality (:func:`watch_repricing_tool_names` answers the same
question for a registration stand-in, which has no served surface to ask).

The set is deliberately the same object as
:data:`app.services.watch_trigger_repricing.capability.PROPOSAL_ONLY_TOOLS`
-- one definition, two consumers -- so the profile and the capability
boundary cannot drift apart.

Why the process boundary, not an in-process guard
-------------------------------------------------
ROB-1290 r3 measured the in-process approach and found it cannot close:
a subclass, an injected judge or a swapped module global defeats capability
tokens, ``@final`` and in-process allowlists alike, because the raw callable
stays reachable inside the interpreter. The boundary that does hold is the
one the session cannot reach across -- the set of tools an MCP *server*
hands out. :func:`build_watch_repricing_server` is that provisioning step,
and :func:`assert_provisioned_surface` is the closed-equality check against
the surface the server actually serves rather than the one it was asked
for. A name outside that surface is not filtered by the client: the server
answers ``tools/call`` for it with ``Unknown tool``.

Provisioning is not arming. These helpers build an in-process server object
and read its tool surface; they start no transport and spawn no session.
Wiring a live spawner onto them is a separate change.
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
from app.services.watch_trigger_repricing.live_contract import assert_exact_grant

if TYPE_CHECKING:
    from fastmcp import FastMCP

_F = TypeVar("_F", bound=Callable[..., Any])

__all__ = [
    "WATCH_REPRICING_TOOL_NAMES",
    "assert_provisioned_surface",
    "build_watch_repricing_server",
    "provisioned_tool_names",
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
    """The names a registration target collected, for the exact comparison.

    Reads a name mapping, so it answers for registration stand-ins. A real
    ``FastMCP`` keeps no such mapping -- ask it with
    :func:`provisioned_tool_names` instead.
    """
    tools = getattr(mcp, "tools", None)
    if tools is None:
        raise TypeError("server object exposes no tool registry to attest")
    return frozenset(tools)


def build_watch_repricing_server(*, name: str = "auto_trader-mcp") -> FastMCP:
    """Provision a real MCP server carrying exactly the proposal-only surface.

    Goes through :func:`~app.mcp_server.tooling.registry.register_all_tools`
    rather than calling :func:`register_watch_repricing_tools` directly, so
    the registry's profile branch -- the early return that keeps the broad
    "Always" block from running -- is part of what gets provisioned and
    therefore part of what the attestation proves. ``on_duplicate="error"``
    matches production ``main.py``.

    The result is an unstarted, unauthenticated in-process object: transport,
    auth and lifespan stay ``main.py``'s job.
    """
    # Deferred: ``registry`` imports this module, so a module-level import
    # would close the cycle. ``fastmcp`` follows it for symmetry.
    from fastmcp import FastMCP as _FastMCP

    from app.mcp_server.profiles import McpProfile
    from app.mcp_server.tooling.registry import register_all_tools

    mcp = _FastMCP(name=name, on_duplicate="error")
    register_all_tools(mcp, profile=McpProfile.WATCH_REPRICING)
    return cast("FastMCP", mcp)


async def provisioned_tool_names(mcp: Any) -> frozenset[str]:
    """The names a built server actually *serves*, asked the way a client asks.

    ``tools/list`` is the only surface a spawned session can see, so it is
    the surface the comparison has to be made against. Reading a registrar's
    bookkeeping instead would attest what registration intended rather than
    what the server ended up serving.
    """
    lister = getattr(mcp, "list_tools", None)
    if lister is None:
        raise TypeError("server object does not serve tools/list")
    return frozenset(str(tool.name) for tool in await lister())


async def assert_provisioned_surface(mcp: Any) -> frozenset[str]:
    """Refuse a built server whose served surface is not the allowlist.

    Closed equality, both directions, via the same
    :func:`~app.services.watch_trigger_repricing.live_contract.assert_exact_grant`
    the live-spawner contract uses: an extra tool is a capability escape, a
    missing one is a session that cannot finish its job. Returns the served
    set so a caller can log the surface it accepted.
    """
    served = await provisioned_tool_names(mcp)
    assert_exact_grant(served, who=f"MCP server {getattr(mcp, 'name', mcp)!r}")
    return served
