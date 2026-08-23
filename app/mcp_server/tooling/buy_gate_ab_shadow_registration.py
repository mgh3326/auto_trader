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
    "calibration_exclude. forecast_save appends the corresponding eligibility "
    "decision; do not call it unless you intend a pure "
    "record. Do not use this output for PnL scoring, threshold tuning, or "
    "policy change before the pre-registered 4-week collection completes. "
    "INPUT CONTRACT (exact keys; an unknown key is rejected, never ignored): "
    "required symbol, market ('kr'|'us'), current_price; optional "
    "support_strength ('weak'|'moderate'|'strong'), support_distance_pct, "
    "rsi, honest_upside_pct, other_gate_bits (booleans keyed liquid_midcap / "
    "concentration / overhang). Note rsi, NOT rsi_14; support_strength, NOT "
    "nearest_support_strength — those two typos silently voided a whole US "
    "collection day on 2026-08-21 (ROB-1315 §5-1). A rejection echoes "
    "input_contract naming the correct key. An omitted optional field is "
    "still a rejection: a gate cannot pass on absent evidence. "
    "US LIMITATION: the overhang bit has no US data source in this repo "
    "(get_disclosures is DART/KR-only), so a US caller that sets "
    "overhang=false is asserting an unverified gate and B-only stays at zero. "
    "See docs/superpowers/specs/2026-08-22-us-overhang-gate-design.md — do not "
    "invent a pass."
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
