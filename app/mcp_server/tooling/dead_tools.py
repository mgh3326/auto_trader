"""Profile-specific D deregistration from Complete classification (2026-09-03).

The audit is frozen evidence, not a runtime config dependency. This proxy only
removes reviewed names; existing feature gates and profile allowlists still run.
Three DEFAULT registrations remain for the promoted lane contracts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from fastmcp import FastMCP

_SHARED_DEAD_TOOLS = frozenset(
    {
        "analysis_bundle_create",
        "analysis_bundle_get",
        "analyze_portfolio",
        "get_analyst_consensus",
        "get_dividends",
        "get_financials",
        "get_insider_transactions",
        "get_investor_trends",
        "get_market_reports",
        "get_retrospective_aggregate",
        "get_sector_peers",
        "get_short_interest",
        "get_toss_ai_signal",
        "get_toss_buy_balance",
        "get_trading_scoreboard",
        "get_user_setting",
        "investment_report_activate_watch",
        "investment_report_add_items",
        "investment_report_context_get",
        "investment_report_create_from_hermes_composition",
        "investment_report_decide_item",
        "investment_report_delta_get",
        "investment_report_list",
        "investment_report_prepare_intraday_context",
        "investment_report_set_status",
        "investment_report_update",
        "investment_watch_expire",
        "investment_watch_recommend",
        "investment_watch_void",
        "list_active_journals",
        "order_proposal_expire_sweep",
        "order_proposal_redispatch",
        "research_summary_get",
        "save_trade_journal",
        "set_user_setting",
        "stage_analysis_get",
        "sweep_expired_watches",
        "update_manual_holdings",
        "update_trade_journal",
    }
)

PROFILE_DEAD_TOOLS: dict[str, frozenset[str]] = {
    "us-paper": _SHARED_DEAD_TOOLS
    | frozenset(
        {
            "alpaca_paper_automated_preview_order",
            "alpaca_paper_reconcile_orders",
        }
    ),
    "db-paper": _SHARED_DEAD_TOOLS
    | frozenset(
        {
            "compare_paper_accounts",
            "compare_strategies",
            "create_paper_account",
            "delete_paper_account",
            "get_paper_performance",
            "get_paper_trade_log",
            "recommend_go_live",
            "reset_paper_account",
        }
    ),
}


class _DeadToolFilteredMCP:
    def __init__(self, inner: Any, removed: frozenset[str]) -> None:
        self._inner = inner
        self._removed = removed

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        direct = args[0] if args and callable(args[0]) else None
        name = kwargs.get("name")
        if name is None and args:
            name = direct.__name__ if direct is not None else args[0]
        if name not in self._removed:
            return self._inner.tool(*args, **kwargs)
        if direct is not None:
            return direct
        return lambda function: function

    def list_tools(self) -> Any:
        lister = getattr(self._inner, "list_tools", None)
        return [] if lister is None else lister()


def without_dead_tools(mcp: FastMCP, profile: str) -> FastMCP:
    removed = PROFILE_DEAD_TOOLS.get(profile, frozenset())
    if not removed:
        return mcp
    return cast("FastMCP", _DeadToolFilteredMCP(mcp, removed))
