"""MCP registration for the ROB-1351 v2 pre-arming witness evaluator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.buy_gate_ab_shadow_v2 import (
    evaluate_buy_gate_ab_shadow_v2_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


BUY_GATE_AB_SHADOW_V2_TOOL_NAMES: set[str] = {"evaluate_buy_gate_ab_shadow_v2"}

_TOOL_DESCRIPTION = (
    "ROB-1351 v2 pre-arming buy-gate A/B evaluator for an explicit reviewed "
    "candidate set. Variant A is the operator-decided moderate live gate; "
    "variant B is the weak-support shadow counterfactual. All three shared "
    "review bits must be actual booleans for experiment_sample=true; missing "
    "or non-boolean bits reject the gate and force a non-sample witness. "
    "Observation-only: it never creates a proposal, order, watch, collection "
    "epoch, or database row. It returns forecast_save kwargs only; a caller "
    "chooses whether to persist the pre-arming witness. Do not use a witness "
    "for policy change, promotion, or a winner declaration."
)


def register_buy_gate_ab_shadow_v2_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="evaluate_buy_gate_ab_shadow_v2",
        description=_TOOL_DESCRIPTION,
    )
    def evaluate_buy_gate_ab_shadow_v2(
        candidates: list[dict[str, Any]],
        evaluation_as_of: str,
        created_by: str,
    ) -> dict[str, Any]:
        return evaluate_buy_gate_ab_shadow_v2_impl(
            candidates,
            evaluation_as_of=evaluation_as_of,
            created_by=created_by,
        )


__all__ = [
    "BUY_GATE_AB_SHADOW_V2_TOOL_NAMES",
    "register_buy_gate_ab_shadow_v2_tools",
]
