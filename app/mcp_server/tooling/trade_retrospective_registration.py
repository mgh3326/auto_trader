# app/mcp_server/tooling/trade_retrospective_registration.py
"""ROB-474 — MCP registration for trade retrospective tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.trade_retrospective_tools import (
    get_retrospective_aggregate,
    get_trade_retrospectives,
    save_trade_retrospective,
    trade_retrospective_pending,
)

TRADE_RETROSPECTIVE_TOOL_NAMES: set[str] = {
    "save_trade_retrospective",
    "get_trade_retrospectives",
    "get_retrospective_aggregate",
    "trade_retrospective_pending",
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
            "fx_pnl_krw, total_pnl_krw, fx_rate_source, fx_pnl_accuracy). "
            "Postmortem taxonomy (ROB-647): root_cause_class in {user_input, "
            "analysis, policy, execution, harness} (NOT process_error/etc.); "
            "trigger_type in {fill, partial_fill, rejected_order, cancelled, "
            "expired, thesis_change, policy_violation, stale_evidence, "
            "guardrail_block, stop_loss}. When trigger_type is set, a non-empty next_actions "
            "list is required in the same call (each next_action needs a non-empty "
            "action; optional owner/issue_id/status/due_kst_date, status in "
            "{open, in_progress, done})."
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
            "Aggregate retrospectives by group_by in {strategy, day, trigger_type, "
            "root_cause} over a KST date window: win_rate_pct, avg_pnl_pct, "
            "absolute realized_pnl sum (per currency), wins/misses, plus "
            "by_outcome/by_trigger_type/by_root_cause_class breakdowns per group. "
            "PnL-oriented dims (strategy/day) count only fill-evidence rows "
            "(excluded_no_fill_evidence reported); process dims "
            "(trigger_type/root_cause) include no-evidence rows so "
            "rejected/cancelled postmortems are analyzed too. Read-only. "
            "Complements get_mock_loop_retrospective."
        ),
    )(get_retrospective_aggregate)
    _ = mcp.tool(
        name="trade_retrospective_pending",
        description=(
            "List lifecycle-terminal orders across the live ledgers (kis_live KR, "
            "generic live US/crypto, toss_live), paper_trades, and the kis_mock "
            "ledger (ROB-730 counterfactual loop) that still lack a trade "
            "retrospective, over a KST trade_date window (default: last 14 days). "
            "Defaults to actionable terminals only: filled / rejected / anomaly "
            "(kis_mock: fill/reconciled/failed/anomaly). Cancel-family rows "
            "(cancelled — which includes DAY expiry and strategic cancels — plus "
            "toss cancel_rejected/replace_rejected and kis_mock stale) are hidden "
            "by default and their count is reported in excluded_by_filter; pass "
            "include_cancelled=true to surface them. Each row carries a "
            "suggested_correlation_id to pass to save_trade_retrospective so it is "
            "marked covered next scan. Optional account_mode filter in "
            "{kis_live, upbit_live, toss_live, paper, kis_mock}. Read-only "
            "due-list — no broker/order mutation. (ROB-647, ROB-661, ROB-730)"
        ),
    )(trade_retrospective_pending)


__all__ = ["TRADE_RETROSPECTIVE_TOOL_NAMES", "register_trade_retrospective_tools"]
