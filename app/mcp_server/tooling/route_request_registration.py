"""route_request advisory lane router MCP tool (ROB-649).

DIVERGENCE FROM tradingcodex: the original has no route MCP tool — it injects
lane guidance via a hook and maps lane->role->tool indirectly. auto_trader
exposes a DIRECT lane->tool ADVISORY tool with NO enforcement. Blocking
middleware is a separate follow-up issue (mutation tools only; reads
unrestricted; caller-header-keyed because MCP session state resets on
reconnect — ROB-469).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.route_request_lanes import (
    INTENT_TO_LANE,
    LANE_TO_POLICY_LANE,
    VALID_MARKETS,
    build_registry_unavailable_plan,
    build_route_plan,
)
from app.services.trading_policy_service import (
    TradingPolicyKeyError,
    get_policy_for,
    policy_version_stamp,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

ROUTE_REQUEST_TOOL_NAMES: set[str] = {"route_request"}


@dataclass(frozen=True)
class _RegistrySnapshot:
    available: bool
    names: frozenset[str] = frozenset()


async def _live_registered_names(mcp: Any) -> _RegistrySnapshot:
    """Read the live FastMCP tool surface without inventing a fallback surface."""
    lister = getattr(mcp, "list_tools", None)
    if not callable(lister):
        return _RegistrySnapshot(available=False)
    try:
        result = lister()
        if inspect.isawaitable(result):
            result = await result
        names: set[str] = set()
        for tool in result:
            name = getattr(tool, "name", None)
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    except Exception:
        return _RegistrySnapshot(available=False)
    if not names:
        return _RegistrySnapshot(available=False)
    return _RegistrySnapshot(available=True, names=frozenset(names))


def register_route_request_tools(mcp: FastMCP) -> None:
    async def route_request(
        intent: str | None = None, market: str | None = None
    ) -> dict[str, Any]:
        # ROB-659: intent/market are optional in the schema so a MISSING arg
        # returns a deterministic success=false envelope instead of a FastMCP
        # input-schema error (which no operator flow can branch on). Present-but-
        # invalid values keep the existing unknown_* envelopes.
        if not intent:
            return {
                "success": False,
                "error": "missing_intent",
                "detail": f"intent is required; valid: {sorted(INTENT_TO_LANE)}",
            }
        if intent not in INTENT_TO_LANE:
            return {
                "success": False,
                "error": "unknown_intent",
                "detail": f"unknown intent {intent!r}; valid: {sorted(INTENT_TO_LANE)}",
            }
        if not market:
            return {
                "success": False,
                "error": "missing_market",
                "detail": f"market is required; valid: {sorted(VALID_MARKETS)}",
            }
        if market not in VALID_MARKETS:
            return {
                "success": False,
                "error": "unknown_market",
                "detail": f"unknown market {market!r}; valid: {sorted(VALID_MARKETS)}",
            }
        lane = INTENT_TO_LANE[intent]
        policy_lane = LANE_TO_POLICY_LANE[lane]
        version = policy_version_stamp()
        if policy_lane is None:
            verdict_thresholds: dict[str, Any] = {
                "market": market,
                "lane": None,
                **version,
                "thresholds": {},
            }
        else:
            try:
                verdict_thresholds = get_policy_for(market, policy_lane)
            except TradingPolicyKeyError as exc:
                return {
                    "success": False,
                    "error": "unknown_market",
                    "detail": str(exc),
                }
        registry_snapshot = await _live_registered_names(mcp)
        if not registry_snapshot.available:
            return build_registry_unavailable_plan(
                intent,
                market,
                verdict_thresholds=verdict_thresholds,
                policy_version=version,
            )
        return build_route_plan(
            intent,
            market,
            registered_tools=set(registry_snapshot.names),
            verdict_thresholds=verdict_thresholds,
            policy_version=version,
        )

    _ = mcp.tool(
        name="route_request",
        description=(
            "Advisory lane router: map a coarse intent to the standard tool "
            "sequence, allowed/blocked tools, policy thresholds + version stamp, "
            "and hard constraints for that decision lane. Args: intent in "
            "{buy_analysis, profit_taking, discovery, market_brief}, market in "
            "{kr, us, crypto} (required). Deterministic (same input -> same "
            "output). ADVISORY ONLY — it does not block anything; it echoes "
            "get_trading_policy (ROB-646) with policy_version so a verdict can "
            "cite the criteria. Buy/sell use the proposal-led-v1 contract: "
            "order_proposal_create is the only order-intent surface, Telegram "
            "human approval is required, proposal revalidation owns fresh "
            "preview/submit, and broker evidence reconciliation is required. "
            "This route contract is advisory and cannot enforce or disable "
            "auto-approval configuration. Discovery retains its legacy direct "
            "execution mapping. Registry introspection failure and a missing "
            "required proposal tool fail closed without a direct-order fallback. "
            "Missing or unknown intent/market returns a deterministic "
            "success=false envelope (error in {missing_intent, unknown_intent, "
            "missing_market, unknown_market})."
        ),
    )(route_request)


__all__ = [
    "ROUTE_REQUEST_TOOL_NAMES",
    "register_route_request_tools",
]
