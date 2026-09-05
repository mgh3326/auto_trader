"""Niche-tool isolation (MCP tool usage audit, 2026-09-03).

The audit split the 228 registered tools into A (used in the last 30 days),
B (used in the last 90 only), C (referenced somewhere, but never called), and
D (dead: no calls, no prompt/runbook/code reference). The D tools were removed.

C is different. "Referenced but never called" is exactly the shape of a tool
that a prompt names but no session reaches, *and* the shape of a tool that is
genuinely seasonal. Deleting on that evidence would be guessing. So the C tools
stay registered and are tagged instead: every call emits one
mcp.niche_tool_called warning and sets the Sentry tag mcp.niche=true,
so the next audit can answer "was it ever actually reached?" from telemetry
rather than from a reference count.

Five class-D tools are tagged alongside them. Each was kept for a stated
structural reason rather than because it showed usage, so the next audit needs
the same evidence for them:

- alpaca_paper_automated_preview_order mints the only approval_token its
  class-C partner alpaca_paper_automated_submit_order accepts.
- get_sector_peers is a step in the discovery lane of
  docs/playbooks/trading-decision-playbook.md, which route_request
  serves and two lane tests verify line-by-line.
- get_toss_ai_signal, get_toss_buy_balance and
  investment_report_create_from_hermes_composition are named by live lane
  manifests in config/mcp_lane_allowlists/.

This module changes registration and telemetry only. It does not wrap, gate,
delay, or alter any tool's arguments, return value, or exceptions.
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Class C in docs/mcp-tool-usage-audit-20260903.md: referenced, never called.
CLASS_C_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "alpaca_paper_automated_submit_order",
        "alpaca_paper_cancel_order",
        "alpaca_paper_get_order",
        "alpaca_paper_ledger_get",
        "alpaca_paper_list_assets",
        "alpaca_paper_preview_order",
        "alpaca_paper_roundtrip_report",
        "alpaca_paper_submit_order",
        "buy_ladder_fill_preview",
        "cancel_order",
        "discover_buy_candidates_fanout",
        "get_company_profile",
        "get_correlation",
        "get_crypto_funding_rate",
        "get_crypto_market_regime",
        "get_crypto_open_interest",
        "get_crypto_profile",
        "get_crypto_social",
        "get_execution_strength",
        "get_forecast_calibration",
        "get_investment_opinions",
        "get_latest_market_brief",
        "get_market_issues",
        "get_market_news",
        "get_mock_loop_retrospective",
        "get_order_history",
        "get_retail_sentiment",
        "get_theme_events",
        "get_upbit_index",
        "get_valuation",
        "investment_report_create",
        "investment_report_generate_from_bundle",
        "investment_report_get_hermes_context",
        "investment_report_prepare_bundle",
        "investment_snapshot_bundle_get",
        "investment_snapshot_bundle_list",
        "investment_snapshot_list",
        "investment_stage_artifacts_ingest_from_hermes",
        "kis_live_cancel_order",
        "kis_live_modify_order",
        "kis_live_place_order",
        "kis_live_reconcile_orders",
        "kis_mock_cancel_order",
        "kis_mock_mirror_execute_report",
        "kis_mock_modify_order",
        "kis_mock_place_order",
        "kis_mock_reconciliation_run",
        "kiwoom_mock_cancel_order",
        "kiwoom_mock_get_order_detail",
        "kiwoom_mock_modify_order",
        "kiwoom_mock_place_order",
        "kiwoom_mock_preview_order",
        "kiwoom_mock_us_cancel_order",
        "kiwoom_mock_us_modify_order",
        "kiwoom_mock_us_place_order",
        "kiwoom_mock_us_preview_order",
        "list_paper_accounts",
        "live_reconcile_orders",
        "market_quote_snapshot_ensure",
        "market_quote_snapshot_latest",
        "modify_journal_entry",
        "modify_order",
        "order_proposal_list_expired_defensive",
        "paper_cohort_kill_switch",
        "paper_list_pending_orders",
        "paper_place_limit_order",
        "paper_reconcile_orders",
        "place_order",
        "research_session_get",
        "research_session_list_recent",
        "support_reserve_net_consume",
        "toss_cancel_order",
        "toss_detect_manual_activity",
        "toss_modify_order",
        "toss_place_order",
        "us_dual_paper_account_states",
        "us_dual_paper_capability_matrix",
        "us_dual_paper_preview",
        "watch_downside_register_sweep",
    }
)

# Class D kept for a structural reason (see the module docstring), not usage.
RETAINED_CLASS_D_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "alpaca_paper_automated_preview_order",
        "get_sector_peers",
        "get_toss_ai_signal",
        "get_toss_buy_balance",
        "investment_report_create_from_hermes_composition",
    }
)

NICHE_TOOL_NAMES: frozenset[str] = CLASS_C_TOOL_NAMES | RETAINED_CLASS_D_TOOL_NAMES

NICHE_CALL_EVENT = "mcp.niche_tool_called"
NICHE_SENTRY_TAG = "mcp.niche"


def _mark_niche_call(tool_name: str) -> None:
    """Emit the observation. Telemetry must never break the tool it observes."""
    logger.warning("%s tool=%s", NICHE_CALL_EVENT, tool_name)
    try:
        import sentry_sdk

        sentry_sdk.set_tag(NICHE_SENTRY_TAG, "true")
    except Exception:  # noqa: BLE001 - a missing/failed Sentry SDK is not fatal
        logger.debug("could not set %s for tool=%s", NICHE_SENTRY_TAG, tool_name)


def wrap_niche_handler(tool_name: str, function: Any) -> Any:
    """Return function tagged as niche, or unchanged if it is not niche."""
    if tool_name not in NICHE_TOOL_NAMES:
        return function

    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def async_niche(*args: Any, **kwargs: Any) -> Any:
            _mark_niche_call(tool_name)
            return await function(*args, **kwargs)

        # functools.wraps copies __name__, so the marker -- not the name -- is
        # what proves a handler went through here.
        async_niche.__mcp_niche_tool__ = tool_name  # type: ignore[attr-defined]
        return async_niche

    @functools.wraps(function)
    def sync_niche(*args: Any, **kwargs: Any) -> Any:
        _mark_niche_call(tool_name)
        return function(*args, **kwargs)

    sync_niche.__mcp_niche_tool__ = tool_name  # type: ignore[attr-defined]
    return sync_niche


NICHE_MARKER_ATTRIBUTE = "__mcp_niche_tool__"


def is_niche_tagged(function: Any) -> bool:
    """True when ``function`` was returned by :func:`wrap_niche_handler`."""
    return getattr(function, NICHE_MARKER_ATTRIBUTE, None) is not None


class NicheTaggingMCP:
    """Registration proxy that tags niche tools as they are registered.

    Wraps only .tool() because that is the entire surface every registrar
    in app/mcp_server/tooling uses; anything else is delegated untouched.
    It is applied once, at the top of register_all_tools, so it covers the
    allowlist profiles that return before the shared block as well.
    """

    def __init__(self, mcp: Any) -> None:
        self._mcp = mcp

    @property
    def wrapped(self) -> Any:
        return self._mcp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mcp, name)

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        register = self._mcp.tool(*args, **kwargs)

        explicit_name = kwargs.get("name")
        if explicit_name is None and args and isinstance(args[0], str):
            explicit_name = args[0]

        def decorate(function: Callable[..., Any]) -> Any:
            name = explicit_name or getattr(function, "__name__", None)
            if not isinstance(name, str) or not name:
                return register(function)
            return register(wrap_niche_handler(name, function))

        if args and callable(args[0]):
            # mcp.tool(fn) -- fastmcp already consumed the function.
            return register
        return decorate


__all__ = [
    "CLASS_C_TOOL_NAMES",
    "NICHE_MARKER_ATTRIBUTE",
    "NICHE_CALL_EVENT",
    "NICHE_SENTRY_TAG",
    "NICHE_TOOL_NAMES",
    "NicheTaggingMCP",
    "RETAINED_CLASS_D_TOOL_NAMES",
    "is_niche_tagged",
    "wrap_niche_handler",
]
