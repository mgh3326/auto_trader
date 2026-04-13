"""Paper Journal MCP tool registration — compare_strategies, recommend_go_live."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.paper_journal_bridge import (
    compare_strategies,
    recommend_go_live,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

PAPER_JOURNAL_TOOL_NAMES: set[str] = {
    "compare_strategies",
    "recommend_go_live",
}


def register_paper_journal_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="compare_strategies",
        description=(
            "Compare paper trading strategy performance over a given period. "
            "Shows per-account/per-strategy metrics such as win rate, realized return, "
            "and best/worst trade. All metrics are based on closed journals only. "
            "If include_live_comparison=True, also compares same-symbol live vs paper "
            "journal outcomes within the same period."
        ),
    )(compare_strategies)

    _ = mcp.tool(
        name="recommend_go_live",
        description=(
            "Evaluate whether a paper trading account meets criteria for live trading. "
            "Checks total trades, win rate, and realized return against thresholds "
            "(default: 20 trades, 50% win rate, positive return). "
            "All metrics are based on closed journals only."
        ),
    )(recommend_go_live)


__all__ = ["PAPER_JOURNAL_TOOL_NAMES", "register_paper_journal_tools"]
