"""Invocation-only observation of the audit's C tools within each MCP profile.

FastMCP tags provide the ``niche`` group without changing public names or
schemas. The profile/name pairs below are transcribed from the 2026-09-03
Complete classification table; usage is never reclassified at runtime.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any

import sentry_sdk

# (audited profiles, C tool names). The removed paper_execution profile's
# paper_cohort_kill_switch handler is preserved but has no active registration.
NICHE_GROUPS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (
        frozenset({"account_read", "default", "crypto", "tradingcodex_execution"}),
        frozenset(
            {
                "get_order_history",
            }
        ),
    ),
    (
        frozenset({"default", "us-paper", "alpaca-paper-clean"}),
        frozenset(
            {
                "alpaca_paper_get_order",
                "alpaca_paper_ledger_get",
                "alpaca_paper_list_assets",
                "alpaca_paper_preview_order",
                "alpaca_paper_roundtrip_report",
            }
        ),
    ),
    (
        frozenset(
            {
                "analysis_readonly",
                "kiwoom",
                "db-paper",
                "default",
                "kiwoom_kr",
                "us-paper",
                "hermes-paper-kis",
                "crypto",
            }
        ),
        frozenset(
            {
                "discover_buy_candidates_fanout",
            }
        ),
    ),
    (
        frozenset(
            {
                "kiwoom",
                "db-paper",
                "default",
                "shadow-replay",
                "kiwoom_kr",
                "us-paper",
                "hermes-paper-kis",
                "crypto",
            }
        ),
        frozenset(
            {
                "investment_report_get_hermes_context",
            }
        ),
    ),
    (
        frozenset(
            {
                "kiwoom",
                "db-paper",
                "default",
                "kiwoom_kr",
                "us-paper",
                "tradingcodex_execution",
                "hermes-paper-kis",
                "crypto",
            }
        ),
        frozenset(
            {
                "order_proposal_list_expired_defensive",
                "support_reserve_net_consume",
            }
        ),
    ),
    (
        frozenset(
            {
                "kiwoom",
                "db-paper",
                "default",
                "kiwoom_kr",
                "us-paper",
                "hermes-paper-kis",
                "crypto",
            }
        ),
        frozenset(
            {
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
                "get_retail_sentiment",
                "get_theme_events",
                "get_upbit_index",
                "get_valuation",
                "investment_report_create",
                "investment_report_generate_from_bundle",
                "investment_report_prepare_bundle",
                "investment_snapshot_bundle_get",
                "investment_snapshot_bundle_list",
                "investment_snapshot_list",
                "investment_stage_artifacts_ingest_from_hermes",
                "modify_journal_entry",
                "research_session_get",
                "research_session_list_recent",
                "watch_downside_register_sweep",
            }
        ),
    ),
    (
        frozenset(
            {"kiwoom", "db-paper", "default", "us-paper", "hermes-paper-kis", "crypto"}
        ),
        frozenset(
            {
                "kis_mock_mirror_execute_report",
            }
        ),
    ),
    (
        frozenset({"default", "crypto"}),
        frozenset(
            {
                "kis_mock_reconciliation_run",
                "live_reconcile_orders",
                "modify_order",
            }
        ),
    ),
    (
        frozenset({"default", "crypto", "tradingcodex_execution"}),
        frozenset(
            {
                "buy_ladder_fill_preview",
                "cancel_order",
                "place_order",
            }
        ),
    ),
    (
        frozenset({"db-paper"}),
        frozenset(
            {
                "list_paper_accounts",
            }
        ),
    ),
    (
        frozenset({"default"}),
        frozenset(
            {
                "kis_live_modify_order",
                "kis_live_reconcile_orders",
                "paper_list_pending_orders",
                "paper_place_limit_order",
                "paper_reconcile_orders",
                "toss_detect_manual_activity",
                "toss_modify_order",
            }
        ),
    ),
    (
        frozenset({"default", "hermes-paper-kis"}),
        frozenset(
            {
                "kis_mock_cancel_order",
                "kis_mock_modify_order",
                "kis_mock_place_order",
            }
        ),
    ),
    (
        frozenset({"default", "kiwoom"}),
        frozenset(
            {
                "kiwoom_mock_us_cancel_order",
                "kiwoom_mock_us_modify_order",
                "kiwoom_mock_us_place_order",
                "kiwoom_mock_us_preview_order",
            }
        ),
    ),
    (
        frozenset({"default", "kiwoom", "kiwoom_kr"}),
        frozenset(
            {
                "kiwoom_mock_get_order_detail",
            }
        ),
    ),
    (
        frozenset({"default", "kiwoom", "kiwoom_kr", "tradingcodex_execution"}),
        frozenset(
            {
                "kiwoom_mock_cancel_order",
                "kiwoom_mock_modify_order",
                "kiwoom_mock_place_order",
                "kiwoom_mock_preview_order",
            }
        ),
    ),
    (
        frozenset({"default", "tradingcodex_execution"}),
        frozenset(
            {
                "kis_live_cancel_order",
                "kis_live_place_order",
                "toss_cancel_order",
                "toss_place_order",
            }
        ),
    ),
    (
        frozenset({"default", "us-paper"}),
        frozenset(
            {
                "alpaca_paper_cancel_order",
                "alpaca_paper_submit_order",
                "market_quote_snapshot_ensure",
                "market_quote_snapshot_latest",
                "us_dual_paper_account_states",
                "us_dual_paper_capability_matrix",
                "us_dual_paper_preview",
            }
        ),
    ),
    (
        frozenset({"us-paper"}),
        frozenset(
            {
                "alpaca_paper_automated_submit_order",
            }
        ),
    ),
)


class _NicheLogger(logging.LoggerAdapter):
    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        extra = dict(kwargs.get("extra") or {})
        extra["tool"] = kwargs.pop("tool")
        kwargs["extra"] = extra
        return msg, kwargs


logger = _NicheLogger(logging.getLogger(__name__), {})


@contextmanager
def _niche_call(tool_name: str, *, warn: bool = True) -> Iterator[None]:
    # Context-local scope restores tags on success, error and cancellation.
    # Setup failures are observation-only; the handler never enters this catch.
    with sentry_sdk.new_scope() as scope:
        try:
            scope.set_tag("mcp.niche", "true")
            span = sentry_sdk.get_current_span()
            if span is not None:
                span.set_tag("mcp.niche", "true")
        except Exception:
            pass
        if warn:
            try:
                logger.warning("mcp.niche_tool_called", tool=tool_name)
            except Exception:
                pass
        yield


def _observe(function: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def observed(*args: Any, **kwargs: Any) -> Any:
            with _niche_call(tool_name):
                return await function(*args, **kwargs)

        return observed

    @wraps(function)
    def observed_sync(*args: Any, **kwargs: Any) -> Any:
        with _niche_call(tool_name):
            result = function(*args, **kwargs)
        if inspect.isawaitable(result):
            # FastMCP also supports sync callables that return awaitables.
            # Re-enter the call scope while awaiting, without a second warning.
            async def finish() -> Any:
                with _niche_call(tool_name, warn=False):
                    return await result

            return finish()
        return result

    return observed_sync


class NicheMCP:
    """Registration proxy composed inside existing gates/account pins."""

    def __init__(self, inner: Any, *, profile: str) -> None:
        self._inner = inner
        self._names = frozenset(
            name
            for profiles, names in NICHE_GROUPS
            if profile in profiles
            for name in names
        )

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        direct = args[0] if args and callable(args[0]) else None
        name = kwargs.get("name")
        if name is None and args:
            name = direct.__name__ if direct is not None else args[0]
        if name not in self._names:
            return self._inner.tool(*args, **kwargs)
        options = dict(kwargs)
        options["tags"] = set(options.get("tags") or ()) | {"niche"}
        if direct is not None:
            return self._inner.tool(_observe(direct, name), *args[1:], **options)
        register = self._inner.tool(*args, **options)
        return lambda function: register(_observe(function, name))

    def list_tools(self) -> Any:
        lister = getattr(self._inner, "list_tools", None)
        return [] if lister is None else lister()
