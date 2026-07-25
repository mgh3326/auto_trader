"""ROB-713 — MCP registration for the trading scoreboard read tool."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.trading_scoreboard_tools import get_trading_scoreboard

TRADING_SCOREBOARD_TOOL_NAMES: set[str] = {"get_trading_scoreboard"}


def register_trading_scoreboard_tools(mcp: Any) -> None:
    _ = mcp.tool(
        name="get_trading_scoreboard",
        description=(
            "Setup-tagged trade-journal aggregates over closed round-trips "
            "reconstructed from live-order-ledger fills: per setup tag "
            "(strategy_key -> intent -> untagged) win-rate, expectancy (% and "
            "R-multiple), profit factor, average/worst MAE and MFE. Tags with "
            "n<10 are flagged insufficient_sample. Filters: market, "
            "account_mode, date_from/date_to (YYYY-MM-DD), setup_tag, "
            "min_sample. When include_counterfactual_delta=true, also accepts "
            "min_pair_threshold (default 20, only affects pairing_health; it "
            "does not filter rows). Read-only; deterministic."
        ),
    )(get_trading_scoreboard)


__all__ = ["TRADING_SCOREBOARD_TOOL_NAMES", "register_trading_scoreboard_tools"]
