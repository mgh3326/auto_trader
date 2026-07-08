"""TradingCodex execution MCP profile registration.

This profile is narrower than the default MCP surface and more privileged than
account_read. It is only for the TradingCodex BrokerAdapter live gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from app.mcp_server.tooling.account_read_registration import ACCOUNT_READ_TOOL_NAMES
from app.mcp_server.tooling.account_routing_registration import (
    register_account_routing_tools,
)
from app.mcp_server.tooling.analysis_artifact_registration import (
    ANALYSIS_ARTIFACT_TOOL_NAMES,
)
from app.mcp_server.tooling.analysis_readonly_registration import _AllowlistedMCP
from app.mcp_server.tooling.forecast_registration import (
    FORECAST_TOOL_NAMES,
    register_forecast_tools,
)
from app.mcp_server.tooling.forecast_tools import forecast_save as _forecast_save
from app.mcp_server.tooling.fundamentals_registration import register_fundamentals_tools
from app.mcp_server.tooling.investment_reports_handlers import (
    INVESTMENT_REPORT_TOOL_NAMES,
    register_investment_report_tools,
)
from app.mcp_server.tooling.investment_reports_handlers import (
    investment_watch_create_impl as _investment_watch_create,
)
from app.mcp_server.tooling.operating_briefing_registration import (
    OPERATING_BRIEFING_TOOL_NAMES,
    register_operating_briefing_tools,
)
from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
    LIVE_RECONCILE_TOOL_NAMES,
    register_kis_live_order_tools,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import (
    ORDER_TOOL_NAMES,
    register_order_tools,
)
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
from app.mcp_server.tooling.session_context_registration import (
    SESSION_CONTEXT_TOOL_NAMES,
)
from app.mcp_server.tooling.trade_retrospective_registration import (
    TRADE_RETROSPECTIVE_TOOL_NAMES,
    register_trade_retrospective_tools,
)
from app.mcp_server.tooling.trade_retrospective_tools import (
    save_trade_retrospective as _save_trade_retrospective,
)
from app.mcp_server.tooling.trading_policy_registration import (
    register_trading_policy_tools,
)
from app.mcp_server.tooling.user_settings_registration import USER_SETTINGS_TOOL_NAMES

if TYPE_CHECKING:
    from fastmcp import FastMCP


_TRADINGCODEX_EXECUTION_ORDER_TOOL_NAMES: set[str] = {
    "place_order",
    "cancel_order",
    "kis_live_place_order",
    "kis_live_cancel_order",
    "toss_preview_order",
    "toss_place_order",
    "toss_cancel_order",
    "sell_ladder_fill_preview",
    "buy_ladder_fill_preview",
}

_TRADINGCODEX_EXECUTION_ADVISORY_TOOL_NAMES: set[str] = {
    "suggest_order_account",
    "get_fx_rate",
    "route_request",
    "get_trading_policy",
}

_TRADINGCODEX_EXECUTION_WATCH_READ_TOOL_NAMES: set[str] = {
    "list_active_watches",
    "investment_watch_events_list_recent",
}

_TRADINGCODEX_EXECUTION_WATCH_WRITE_TOOL_NAMES: set[str] = {
    "investment_watch_create",
}

_TRADINGCODEX_EXECUTION_LEARNING_READ_TOOL_NAMES: set[str] = {
    "get_forecasts",
    "get_trade_retrospectives",
    "trade_retrospective_pending",
}

_TRADINGCODEX_EXECUTION_LEARNING_WRITE_TOOL_NAMES: set[str] = {
    "forecast_save",
    "save_trade_retrospective",
}

TRADINGCODEX_EXECUTION_TOOL_NAMES: set[str] = (
    ACCOUNT_READ_TOOL_NAMES
    | _TRADINGCODEX_EXECUTION_ORDER_TOOL_NAMES
    | _TRADINGCODEX_EXECUTION_ADVISORY_TOOL_NAMES
    | _TRADINGCODEX_EXECUTION_WATCH_READ_TOOL_NAMES
    | _TRADINGCODEX_EXECUTION_WATCH_WRITE_TOOL_NAMES
    | _TRADINGCODEX_EXECUTION_LEARNING_READ_TOOL_NAMES
    | _TRADINGCODEX_EXECUTION_LEARNING_WRITE_TOOL_NAMES
)

_INVESTMENT_REPORT_REGISTERED_TOOL_NAMES: set[str] = INVESTMENT_REPORT_TOOL_NAMES | {
    "investment_watch_events_list_recent",
}

# Narrow allowlist passed to register_investment_report_tools so the raw
# investment_watch_create (no created_by guard) is NEVER registered through
# the filtered pathway. Only watch read tools survive the filter — the
# guarded investment_watch_create wrapper is registered directly on the
# unfiltered mcp via _register_watch_write_tools below.
_TRADINGCODEX_EXECUTION_INVESTMENT_REPORT_FILTER_TOOL_NAMES: set[str] = (
    _TRADINGCODEX_EXECUTION_WATCH_READ_TOOL_NAMES
)

TRADINGCODEX_EXECUTION_FORBIDDEN_TOOL_NAMES: set[str] = (
    (ORDER_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | (KIS_LIVE_ORDER_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | KIS_MOCK_ORDER_TOOL_NAMES
    | LIVE_RECONCILE_TOOL_NAMES
    | KIWOOM_MOCK_TOOL_NAMES
    | PAPER_LIMIT_ORDER_TOOL_NAMES
    | (TOSS_LIVE_ORDER_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | ANALYSIS_ARTIFACT_TOOL_NAMES
    | (FORECAST_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | (TRADE_RETROSPECTIVE_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | SESSION_CONTEXT_TOOL_NAMES
    | USER_SETTINGS_TOOL_NAMES
    | (OPERATING_BRIEFING_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | (_INVESTMENT_REPORT_REGISTERED_TOOL_NAMES - TRADINGCODEX_EXECUTION_TOOL_NAMES)
    | {
        "get_available_capital",
        "get_position",
        "update_manual_holdings",
    }
)


def _clean_label(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _created_by_required(tool_name: str, *, parameter: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": "created_by_required",
        "tool": tool_name,
        "detail": (
            "tradingcodex_execution write calls must pass explicit "
            f"{parameter} such as 'tradingcodex'."
        ),
    }


def _register_learning_read_tools(mcp: FastMCP) -> None:
    filtered = cast(
        "FastMCP",
        _AllowlistedMCP(mcp, _TRADINGCODEX_EXECUTION_LEARNING_READ_TOOL_NAMES),
    )
    register_forecast_tools(filtered)
    register_trade_retrospective_tools(filtered)


def _register_learning_write_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="forecast_save",
        description=(
            "tradingcodex_execution: record a resolvable forecast. Requires "
            "explicit created_by such as 'tradingcodex'; no implicit caller label."
        ),
    )
    async def forecast_save(
        created_by: str | None = None,
        symbol: str = "",
        instrument_type: str = "",
        forecast_target: dict | None = None,
        probability: float = 0.0,
        review_date: str = "",
        forecast_id: str | None = None,
        horizon: str | None = None,
        probability_range_low: float | None = None,
        probability_range_high: float | None = None,
        evidence_ids: list | None = None,
        contrary_evidence: str | None = None,
        forecast_start_date: str | None = None,
        resolution_source: str | None = None,
        session_label: str | None = None,
        model_label: str | None = None,
        policy_version: str | None = None,
        artifact_uuid: str | None = None,
        journal_id: int | None = None,
        report_uuid: str | None = None,
        report_item_uuid: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        label = _clean_label(created_by)
        if label is None:
            return _created_by_required("forecast_save", parameter="created_by")
        return await _forecast_save(
            created_by=label,
            symbol=symbol,
            instrument_type=instrument_type,
            forecast_target=forecast_target or {},
            probability=probability,
            review_date=review_date,
            forecast_id=forecast_id,
            horizon=horizon,
            probability_range_low=probability_range_low,
            probability_range_high=probability_range_high,
            evidence_ids=evidence_ids,
            contrary_evidence=contrary_evidence,
            forecast_start_date=forecast_start_date,
            resolution_source=resolution_source,
            session_label=session_label,
            model_label=model_label,
            policy_version=policy_version,
            artifact_uuid=artifact_uuid,
            journal_id=journal_id,
            report_uuid=report_uuid,
            report_item_uuid=report_item_uuid,
            correlation_id=correlation_id,
        )

    @mcp.tool(
        name="save_trade_retrospective",
        description=(
            "tradingcodex_execution: store a structured trade retrospective. "
            "Requires explicit created_by_profile such as 'tradingcodex'."
        ),
    )
    async def save_trade_retrospective(
        symbol: str = "",
        instrument_type: str = "",
        account_mode: str = "",
        outcome: str = "",
        side: str | None = None,
        market: str | None = None,
        strategy_key: str | None = None,
        correlation_id: str | None = None,
        journal_id: int | None = None,
        report_uuid: str | None = None,
        report_item_uuid: str | None = None,
        plan_price: float | None = None,
        fill_price: float | None = None,
        realized_pnl: float | None = None,
        realized_pnl_currency: str | None = None,
        pnl_pct: float | None = None,
        rationale: str | None = None,
        result_summary: str | None = None,
        lesson: str | None = None,
        next_strategy: str | None = None,
        evidence_snapshot: dict | None = None,
        created_by_profile: str | None = None,
        buy_fx_rate: float | None = None,
        sell_fx_rate: float | None = None,
        fx_pnl_krw: float | None = None,
        security_pnl_usd: float | None = None,
        security_pnl_krw: float | None = None,
        total_pnl_krw: float | None = None,
        fx_rate_source: str | None = None,
        fx_pnl_accuracy: str | None = None,
        trigger_type: str | None = None,
        root_cause_class: str | None = None,
        intended_vs_happened: dict | None = None,
        next_actions: list | None = None,
        guardrail_fired: str | None = None,
        policy_version: str | None = None,
    ) -> dict[str, Any]:
        label = _clean_label(created_by_profile)
        if label is None:
            return _created_by_required(
                "save_trade_retrospective",
                parameter="created_by_profile",
            )
        return await _save_trade_retrospective(
            symbol=symbol,
            instrument_type=instrument_type,
            account_mode=account_mode,
            outcome=outcome,
            side=side,
            market=market,
            strategy_key=strategy_key,
            correlation_id=correlation_id,
            journal_id=journal_id,
            report_uuid=report_uuid,
            report_item_uuid=report_item_uuid,
            plan_price=plan_price,
            fill_price=fill_price,
            realized_pnl=realized_pnl,
            realized_pnl_currency=realized_pnl_currency,
            pnl_pct=pnl_pct,
            rationale=rationale,
            result_summary=result_summary,
            lesson=lesson,
            next_strategy=next_strategy,
            evidence_snapshot=evidence_snapshot,
            created_by_profile=label,
            buy_fx_rate=buy_fx_rate,
            sell_fx_rate=sell_fx_rate,
            fx_pnl_krw=fx_pnl_krw,
            security_pnl_usd=security_pnl_usd,
            security_pnl_krw=security_pnl_krw,
            total_pnl_krw=total_pnl_krw,
            fx_rate_source=fx_rate_source,
            fx_pnl_accuracy=fx_pnl_accuracy,
            trigger_type=trigger_type,
            root_cause_class=root_cause_class,
            intended_vs_happened=intended_vs_happened,
            next_actions=next_actions,
            guardrail_fired=guardrail_fired,
            policy_version=policy_version,
        )


def _register_watch_write_tools(mcp: FastMCP) -> None:
    """Direct watch-create wrapper: ``created_by`` guard BEFORE DB write.

    The underlying ``investment_watch_create_impl`` is a base MCP surface tool
    (no guard). Registering it through the filtered pathway would leak the raw
    impl onto ``tradingcodex_execution`` and defeat the write provenance gate.
    This wrapper rejects missing/blank ``created_by`` and only then forwards to
    the base impl.
    """

    @mcp.tool(
        name="investment_watch_create",
        description=(
            "tradingcodex_execution: create an active support/resistance watch "
            "without report-flow coupling. Requires explicit created_by such "
            "as 'tradingcodex'."
        ),
    )
    async def investment_watch_create(
        created_by: str | None = None,
        market: str = "",
        symbol: str = "",
        intent: str = "",
        rationale: str = "",
        watch_condition: dict | None = None,
        valid_until: str = "",
        trigger_checklist: list[str] | None = None,
        max_action: dict | None = None,
        metadata: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        label = _clean_label(created_by)
        if label is None:
            return _created_by_required(
                "investment_watch_create", parameter="created_by"
            )
        return await _investment_watch_create(
            created_by=label,
            market=market,
            symbol=symbol,
            intent=intent,
            rationale=rationale,
            watch_condition=watch_condition or {},
            valid_until=valid_until,
            trigger_checklist=trigger_checklist,
            max_action=max_action,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )


def register_tradingcodex_execution_tools(mcp: FastMCP) -> None:
    """Register only the TradingCodex broker execution allowlist."""
    filtered = cast("FastMCP", _AllowlistedMCP(mcp, TRADINGCODEX_EXECUTION_TOOL_NAMES))
    register_portfolio_tools(filtered)
    register_order_tools(filtered)
    register_kis_live_order_tools(filtered)
    register_toss_live_order_tools(filtered)
    register_account_routing_tools(filtered)
    register_fundamentals_tools(filtered)
    register_trading_policy_tools(filtered)
    register_route_request_tools(filtered)
    register_operating_briefing_tools(filtered)
    # Narrow allowlist so the raw (unguarded) investment_watch_create impl is
    # NOT registered through the filtered pathway. The guarded wrapper is
    # registered directly on ``mcp`` via _register_watch_write_tools below.
    investment_report_filtered = cast(
        "FastMCP",
        _AllowlistedMCP(
            mcp, _TRADINGCODEX_EXECUTION_INVESTMENT_REPORT_FILTER_TOOL_NAMES
        ),
    )
    register_investment_report_tools(
        investment_report_filtered, include_snapshot_generator=False
    )
    _register_learning_read_tools(mcp)
    _register_learning_write_tools(mcp)
    _register_watch_write_tools(mcp)


__all__ = [
    "TRADINGCODEX_EXECUTION_FORBIDDEN_TOOL_NAMES",
    "TRADINGCODEX_EXECUTION_TOOL_NAMES",
    "register_tradingcodex_execution_tools",
]
