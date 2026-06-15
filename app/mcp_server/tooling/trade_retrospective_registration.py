# app/mcp_server/tooling/trade_retrospective_registration.py
"""ROB-474 — MCP registration for trade retrospective tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.trade_retrospective_tools import (
    get_retrospective_aggregate,
    get_trade_retrospectives,
    save_trade_retrospective,
)

TRADE_RETROSPECTIVE_TOOL_NAMES: set[str] = {
    "save_trade_retrospective",
    "get_trade_retrospectives",
    "get_retrospective_aggregate",
}


def register_trade_retrospective_tools(mcp: Any) -> None:
    _ = mcp.tool(
        name="save_trade_retrospective",
        description=(
            "Store a structured trade retrospective (outcome, absolute realized_pnl, "
            "fill/plan price, pnl_pct, rationale/result/lesson/next_strategy) for a "
            "trade. account_mode in {kis_mock, kiwoom_mock, kis_live, toss_live, "
            "alpaca_paper, upbit_live}. Idempotent per correlation_id (omit it to "
            "append). "
            "kiwoom_mock cannot supply realized_pnl/fill_price (no fill evidence, "
            "ROB-460). realized_pnl is caller-supplied, or derived from journal_id "
            "when entry/exit/qty are present. ROB-568: accepts US FX PnL fields "
            "(buy_fx_rate, sell_fx_rate, security_pnl_usd, security_pnl_krw, "
            "fx_pnl_krw, total_pnl_krw, fx_rate_source, fx_pnl_accuracy)."
        ),
    )(save_trade_retrospective)
    _ = mcp.tool(
        name="get_trade_retrospectives",
        description=(
            "List structured trade retrospectives with filters "
            "(symbol/account_mode/strategy_key/market/correlation_id/days). Read-only."
        ),
    )(get_trade_retrospectives)
    _ = mcp.tool(
        name="get_retrospective_aggregate",
        description=(
            "Aggregate retrospectives by strategy_key or KST day over a KST date "
            "window: win_rate_pct, avg_pnl_pct, absolute realized_pnl sum (per "
            "currency), wins/misses. Only rows with fill evidence are counted "
            "(excluded_no_fill_evidence reported). Read-only. Complements "
            "get_mock_loop_retrospective (KST-day x watch-loop x percent)."
        ),
    )(get_retrospective_aggregate)


__all__ = ["TRADE_RETROSPECTIVE_TOOL_NAMES", "register_trade_retrospective_tools"]
