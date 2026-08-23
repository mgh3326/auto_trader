"""MCP registration for bounded buy-candidate fan-out discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.buy_candidate_fanout import (
    discover_buy_candidates_fanout_impl,
)
from app.services.screener_pick_log import maybe_record_fanout_picks

if TYPE_CHECKING:
    from fastmcp import FastMCP


BUY_CANDIDATE_FANOUT_TOOL_NAMES: set[str] = {"discover_buy_candidates_fanout"}


def register_buy_candidate_fanout_tools(mcp: FastMCP) -> None:
    """Register the KR-only, read-only discovery fan-out surface."""

    @mcp.tool(
        name="discover_buy_candidates_fanout",
        description=(
            "Read-only KR buy-candidate discovery across five bounded source families: "
            "RSI ordering (no max_rsi prefilter), pullback change_rate, trade_amount, "
            "snapshot support/flow, and snapshot value/catalyst. Each source is capped "
            "at 10 rows; snapshot groups contain at most 5 presets; only the top 10 "
            "deduped symbols receive full-analysis price/support/consensus/restriction "
            "checks plus top-level data_state freshness proof. A missing freshness key "
            "is recorded as undetermined observation only, never as eligibility. "
            "Returns observation-only funnel evidence, never a proposal or order. It "
            "does not query broker or account state, so budget remains deferred. Do not "
            "use this output for PnL scoring or immediate threshold tuning. The fan-out "
            "itself performs no writes; when SCREENER_PICK_LOG_ENABLED an outer "
            "fail-open observer records the returned picks for prospective scoring."
        ),
    )
    async def discover_buy_candidates_fanout() -> dict[str, Any]:
        result = await discover_buy_candidates_fanout_impl()
        await maybe_record_fanout_picks(result)
        return result


__all__ = ["BUY_CANDIDATE_FANOUT_TOOL_NAMES", "register_buy_candidate_fanout_tools"]
