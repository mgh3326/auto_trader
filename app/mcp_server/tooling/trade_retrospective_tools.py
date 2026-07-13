# app/mcp_server/tooling/trade_retrospective_tools.py
"""ROB-474 — MCP tools for structured trade retrospectives."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.trade_journal.trade_retrospective_service import (
    RetrospectiveValidationError,
    build_retrospective_aggregate,
    build_retrospective_pending,
    get_retrospectives,
    save_retrospective,
    serialize_retrospective,
)

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


async def save_trade_retrospective(
    symbol: str,
    instrument_type: str,
    account_mode: str,
    outcome: str,
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
    """Store a structured trade retrospective.

    Args:
        outcome: One of filled, partially_filled, unfilled, rejected, cancelled.
    """
    symbol = (symbol or "").strip()
    if not symbol:
        return {"success": False, "error": "symbol is required"}
    # ROB-647 — forward postmortem fields only when the caller supplied them, so
    # an idempotent re-save that omits them preserves prior values (the service
    # distinguishes omitted from explicit-None via a sentinel).
    postmortem: dict[str, Any] = {}
    if trigger_type is not None:
        postmortem["trigger_type"] = trigger_type
    if root_cause_class is not None:
        postmortem["root_cause_class"] = root_cause_class
    if intended_vs_happened is not None:
        postmortem["intended_vs_happened"] = intended_vs_happened
    if next_actions is not None:
        postmortem["next_actions"] = next_actions
    if guardrail_fired is not None:
        postmortem["guardrail_fired"] = guardrail_fired
    if policy_version is not None:
        postmortem["policy_version"] = policy_version
    try:
        async with _session_factory()() as db:
            action, row = await save_retrospective(
                db,
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
                created_by_profile=created_by_profile,
                buy_fx_rate=buy_fx_rate,
                sell_fx_rate=sell_fx_rate,
                fx_pnl_krw=fx_pnl_krw,
                security_pnl_usd=security_pnl_usd,
                security_pnl_krw=security_pnl_krw,
                total_pnl_krw=total_pnl_krw,
                fx_rate_source=fx_rate_source,
                fx_pnl_accuracy=fx_pnl_accuracy,
                **postmortem,
            )
            await db.commit()
            await db.refresh(row)
            return {
                "success": True,
                "action": action,
                "data": serialize_retrospective(row),
            }
    except RetrospectiveValidationError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("save_trade_retrospective failed")
        return {"success": False, "error": f"save_trade_retrospective failed: {exc}"}


async def get_trade_retrospectives(
    symbol: str | None = None,
    account_mode: str | None = None,
    strategy_key: str | None = None,
    market: str | None = None,
    correlation_id: str | None = None,
    days: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    try:
        async with _session_factory()() as db:
            result = await get_retrospectives(
                db,
                symbol=symbol,
                account_mode=account_mode,
                strategy_key=strategy_key,
                market=market,
                correlation_id=correlation_id,
                days=days,
                limit=limit,
            )
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_trade_retrospectives failed")
        return {"success": False, "error": f"get_trade_retrospectives failed: {exc}"}


async def get_retrospective_aggregate(
    kst_date_from: str | None = None,
    kst_date_to: str | None = None,
    account_mode: str | None = None,
    market: str | None = None,
    strategy_key: str | None = None,
    group_by: str = "strategy",
) -> dict[str, Any]:
    today = now_kst().date().isoformat()
    date_from = kst_date_from or today
    date_to = kst_date_to or date_from
    try:
        async with _session_factory()() as db:
            result = await build_retrospective_aggregate(
                db,
                kst_date_from=date_from,
                kst_date_to=date_to,
                account_mode=account_mode,
                market=market,
                strategy_key=strategy_key,
                group_by=group_by,
            )
        return {
            "success": True,
            "kst_date_from": date_from,
            "kst_date_to": date_to,
            **result,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_retrospective_aggregate failed")
        return {"success": False, "error": f"get_retrospective_aggregate failed: {exc}"}


async def trade_retrospective_pending(
    kst_date_from: str | None = None,
    kst_date_to: str | None = None,
    account_mode: str | None = None,
    limit: int = 100,
    include_cancelled: bool = False,
) -> dict[str, Any]:
    today = now_kst().date().isoformat()
    date_to = kst_date_to or today
    # Default lookback window: 14 KST days ending today.
    date_from = kst_date_from or (now_kst().date() - timedelta(days=14)).isoformat()
    try:
        async with _session_factory()() as db:
            result = await build_retrospective_pending(
                db,
                kst_date_from=date_from,
                kst_date_to=date_to,
                account_mode=account_mode,
                limit=limit,
                include_cancelled=include_cancelled,
            )
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("trade_retrospective_pending failed")
        return {"success": False, "error": f"trade_retrospective_pending failed: {exc}"}
