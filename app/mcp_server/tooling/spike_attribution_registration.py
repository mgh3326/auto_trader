"""MCP registration for the ROB-1303 spike attribution reader."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.spike_attribution import get_spike_attribution_impl

if TYPE_CHECKING:
    from fastmcp import FastMCP

SPIKE_ATTRIBUTION_TOOL_NAMES: set[str] = {"get_spike_attribution"}

_TOOL_DESCRIPTION = (
    "ROB-1303 read-only spike cause attribution. For each symbol it detects "
    "whether the session moved >=5% (close-to-close or intraday vs the previous "
    "close) and lists the documents that could have caused it: news articles "
    "mapped to the symbol, DART disclosures, and earnings events, each with its "
    "link and an eligibility ruling against the pre-move window (previous "
    "session close, this session close]. Anything published after the close is "
    "returned as after_move and is NOT a cause; anything whose feed clock is "
    "unconfirmed is returned as timestamp_unknown and is NOT a cause. When "
    "nothing is eligible the verdict is unattributed — report it as "
    "unattributed, never as '기타' or '시장 전반'. Multiple eligible candidates "
    "are all returned; do not collapse them to one cause. The catalyst_basis "
    "block is the momentum_spike_profit_ladder tier's evidence slot: it reports "
    "satisfies_catalyst_basis_requirement=false for an unattributed spike and "
    "never supplies flow_basis, so the tier's evidence pair is never complete "
    "from this tool alone. Observation-only: it writes no row, calls no broker, "
    "and reaches no order, approval, or watch surface. The returned "
    "prereg_forecast_save_kwargs are pre-registered follow-through records; "
    "forecast_save is yours to call, and this tool never calls it."
)


def register_spike_attribution_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="get_spike_attribution", description=_TOOL_DESCRIPTION)
    async def get_spike_attribution(
        symbols: list[str],
        session_date: str,
        market: str = "kr",
        created_by: str = "",
    ) -> dict[str, Any]:
        return await get_spike_attribution_impl(
            symbols,
            session_date=session_date,
            market=market,
            created_by=created_by,
        )


__all__ = [
    "SPIKE_ATTRIBUTION_TOOL_NAMES",
    "register_spike_attribution_tools",
]
