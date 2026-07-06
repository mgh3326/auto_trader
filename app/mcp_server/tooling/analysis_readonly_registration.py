"""Read-only analysis MCP profile registration for Codex/headless consumers."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from app.mcp_server.tooling.analysis_artifact_tools import (
    analysis_artifact_get as _analysis_artifact_get,
)
from app.mcp_server.tooling.analysis_artifact_tools import (
    analysis_artifact_save as _analysis_artifact_save,
)
from app.mcp_server.tooling.analysis_registration import register_analysis_tools
from app.mcp_server.tooling.forecast_registration import register_forecast_tools
from app.mcp_server.tooling.fundamentals_registration import register_fundamentals_tools
from app.mcp_server.tooling.market_data_registration import register_market_data_tools
from app.mcp_server.tooling.operating_briefing_registration import (
    register_operating_briefing_tools,
)
from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
    LIVE_RECONCILE_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.orders_toss_variants import (
    TOSS_LIVE_ORDER_TOOL_NAMES,
    register_toss_live_order_tools,
)
from app.mcp_server.tooling.paper_limit_order_handler import (
    PAPER_LIMIT_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.portfolio_registration import register_portfolio_tools
from app.mcp_server.tooling.route_request_registration import (
    register_route_request_tools,
)
from app.mcp_server.tooling.session_context_tools import (
    session_context_append as _session_context_append,
)
from app.mcp_server.tooling.session_context_tools import (
    session_context_get_recent as _session_context_get_recent,
)
from app.mcp_server.tooling.trading_policy_registration import (
    register_trading_policy_tools,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

_F = TypeVar("_F", bound=Callable[..., Any])


ANALYSIS_READONLY_TOOL_NAMES: set[str] = {
    "get_operating_briefing",
    "route_request",
    "get_trading_policy",
    "get_market_index",
    "get_quote",
    "analyze_stock_batch",
    "get_support_resistance",
    "get_indicators",
    "screen_stocks",
    "screen_stocks_snapshot",
    "get_top_stocks",
    "get_news",
    "get_fx_rate",
    "get_holdings",
    "toss_get_positions",
    "get_intraday_investor_flow",
    "analysis_artifact_save",
    "analysis_artifact_get",
    "forecast_save",
    "session_context_append",
    "session_context_get_recent",
}

ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES: set[str] = (
    ORDER_TOOL_NAMES
    | KIS_LIVE_ORDER_TOOL_NAMES
    | KIS_MOCK_ORDER_TOOL_NAMES
    | LIVE_RECONCILE_TOOL_NAMES
    | KIWOOM_MOCK_TOOL_NAMES
    | PAPER_LIMIT_ORDER_TOOL_NAMES
    | (TOSS_LIVE_ORDER_TOOL_NAMES - {"toss_get_positions"})
    | {
        "analysis_artifact_list",
        "forecast_resolve",
        "get_forecasts",
        "get_forecast_calibration",
        "get_user_setting",
        "update_manual_holdings",
        "get_cash_balance",
        "get_available_capital",
        "get_order_history",
        "toss_get_order_history",
        "toss_get_orderable_cash",
        "list_active_watches",
    }
)


class _AllowlistedMCP:
    """Proxy that makes existing group registrars physically register only allowed names."""

    def __init__(self, inner: Any, allowed_names: set[str]) -> None:
        self._inner = inner
        self._allowed_names = allowed_names

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[_F], _F]:
        name = kwargs.get("name")
        if name is None and args:
            name = args[0]
        if str(name) in self._allowed_names:
            return cast(Callable[[_F], _F], self._inner.tool(*args, **kwargs))

        def decorator(func: _F) -> _F:
            return func

        return decorator

    def list_tools(self) -> Any:
        lister = getattr(self._inner, "list_tools", None)
        if lister is None:
            return []
        return lister()


def _created_by_required(tool_name: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": "created_by_required",
        "tool": tool_name,
        "detail": "analysis_readonly persistence calls must pass an explicit created_by label such as 'codex'.",
    }


def _clean_created_by(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _register_persistence_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="analysis_artifact_save",
        description=(
            "analysis_readonly: persist a structured analysis artifact. "
            "Requires explicit created_by such as 'codex'; no implicit caller label."
        ),
    )
    async def analysis_artifact_save(
        market: str,
        kind: str,
        title: str,
        symbols: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        as_of: str | None = None,
        valid_until: str | None = None,
        created_by: str | None = None,
        session_label: str | None = None,
        correlation_id: str | None = None,
        account_scope: str | None = None,
        readiness_label: str | None = None,
    ) -> dict[str, Any]:
        label = _clean_created_by(created_by)
        if label is None:
            return _created_by_required("analysis_artifact_save")
        return await _analysis_artifact_save(
            market=market,
            kind=kind,
            title=title,
            symbols=symbols,
            payload=payload,
            as_of=as_of,
            valid_until=valid_until,
            created_by=label,
            session_label=session_label,
            correlation_id=correlation_id,
            account_scope=account_scope,
            readiness_label=readiness_label,
        )

    mcp.tool(
        name="analysis_artifact_get",
        description="analysis_readonly: fetch one persisted analysis artifact by id or UUID.",
    )(_analysis_artifact_get)

    # forecast_save already has a required created_by parameter and the service
    # rejects blank values; register through the filter below.

    @mcp.tool(
        name="session_context_append",
        description=(
            "analysis_readonly: append session context entries. Every entry must "
            "include explicit created_by such as 'codex'."
        ),
    )
    async def session_context_append(
        entries: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        if entries:
            missing = [
                idx
                for idx, entry in enumerate(entries)
                if isinstance(entry, dict)
                and not _clean_created_by(str(entry.get("created_by", "")))
            ]
            if missing:
                return {
                    "success": False,
                    "error": "created_by_required",
                    "tool": "session_context_append",
                    "entry_indexes": missing,
                    "detail": "each analysis_readonly session context entry must pass created_by explicitly",
                }
        return await _session_context_append(entries)

    mcp.tool(
        name="session_context_get_recent",
        description="analysis_readonly: read recent operator session context entries.",
    )(_session_context_get_recent)


def register_analysis_readonly_tools(mcp: FastMCP) -> None:
    """Register the physically restricted Codex/headless analysis surface."""
    filtered = cast("FastMCP", _AllowlistedMCP(mcp, ANALYSIS_READONLY_TOOL_NAMES))
    register_operating_briefing_tools(filtered)
    register_trading_policy_tools(filtered)
    register_route_request_tools(filtered)
    register_market_data_tools(filtered)
    register_fundamentals_tools(filtered)
    register_analysis_tools(filtered)
    register_portfolio_tools(filtered)
    register_toss_live_order_tools(filtered)
    register_forecast_tools(filtered)
    _register_persistence_tools(mcp)


__all__ = [
    "ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES",
    "ANALYSIS_READONLY_TOOL_NAMES",
    "register_analysis_readonly_tools",
]
