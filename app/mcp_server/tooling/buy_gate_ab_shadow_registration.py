"""MCP registration for the ROB-1301 buy-gate A/B shadow evaluator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.buy_gate_ab_shadow import (
    evaluate_buy_gate_ab_shadow_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

BUY_GATE_AB_SHADOW_TOOL_NAMES: set[str] = {"evaluate_buy_gate_ab_shadow"}

_TOOL_DESCRIPTION = (
    "ROB-1301 shadow-only buy-gate A/B evaluator. Variant A is the live "
    "screening gate (strong support required). Variant B is the moderate+ "
    "support counterfactual. Both variants consume the same candidate snapshot "
    "and the same evaluation_as_of; only support_strength_min differs. "
    "Observation-only: it never creates a proposal, order, or watch, never "
    "writes a database row, and never changes the live gate. B-only survivors "
    "return forecast_save kwargs tagged shadow_buy / promote=false / "
    "calibration_exclude. Do not call forecast_save unless you intend a pure "
    "record. Do not use this output for PnL scoring, threshold tuning, or "
    "policy change before the pre-registered 4-week collection completes."
)


def register_buy_gate_ab_shadow_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="evaluate_buy_gate_ab_shadow",
        description=_TOOL_DESCRIPTION,
    )
    def evaluate_buy_gate_ab_shadow(
        candidates: list[dict[str, Any]],
        evaluation_as_of: str,
        created_by: str,
    ) -> dict[str, Any]:
        return evaluate_buy_gate_ab_shadow_impl(
            candidates,
            evaluation_as_of=evaluation_as_of,
            created_by=created_by,
        )


__all__ = [
    "BUY_GATE_AB_SHADOW_TOOL_NAMES",
    "register_buy_gate_ab_shadow_tools",
]
