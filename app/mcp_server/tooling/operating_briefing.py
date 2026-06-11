"""Read-only operating briefing MCP tools for ROB-517."""

from __future__ import annotations

from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.pending_orders_snapshot import (
    DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE,
    collect_pending_orders_snapshot,
)
from app.mcp_server.tooling.portfolio_holdings import _get_holdings_impl
from app.schemas.investment_reports import (
    ActiveWatchesListResponse,
    InvestmentWatchAlertResponse,
)
from app.schemas.session_context import SessionContextResponse
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
    _advisory_draft_profiles,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.session_context import SessionContextService


def _normalize_watch_symbol(symbol: str | None, market: str | None) -> str | None:
    if symbol is None:
        return None
    stripped = str(symbol).strip()
    if not stripped:
        return None
    if market in {"us", "crypto"}:
        return stripped.upper()
    return stripped


async def list_active_watches_impl(
    market: str | None = None,
    symbol: str | None = None,
    include_expired_status_rows: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    as_of = now_kst()
    capped_limit = max(1, min(int(limit), 250))
    normalized_symbol = _normalize_watch_symbol(symbol, market)
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        rows = await repo.list_active_alerts(
            market=market,
            symbol=normalized_symbol,
            valid_at=as_of,
            include_expired_status_rows=include_expired_status_rows,
            limit=capped_limit,
        )
        response = ActiveWatchesListResponse(
            count=len(rows),
            as_of=as_of,
            filters={
                "market": market,
                "symbol": normalized_symbol,
                "include_expired_status_rows": include_expired_status_rows,
                "limit": capped_limit,
            },
            active_watches=[
                InvestmentWatchAlertResponse.model_validate(row) for row in rows
            ],
        )
    return response.model_dump(mode="json", by_alias=True)


def _default_account_scope(market: str, account_scope: str | None) -> str:
    if account_scope:
        return account_scope
    default = DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE.get(market)
    if default is None:
        raise ValueError(f"unsupported market for operating briefing: {market}")
    return default


def _holdings_kwargs(
    market: str, account_scope: str, include_current_price: bool
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "market": market,
        "include_current_price": include_current_price,
        "routing_account_mode": account_scope,
    }
    if account_scope == "kis_mock":
        kwargs["is_mock"] = True
    if account_scope == "upbit_live":
        kwargs["account"] = "upbit"
    if account_scope == "alpaca_paper":
        kwargs["account"] = "paper"
    return kwargs


def _flatten_positions(holdings: dict[str, Any]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for account in holdings.get("accounts") or []:
        account_name = account.get("account")
        for position in account.get("positions") or []:
            row = dict(position)
            row.setdefault("account", account_name)
            positions.append(row)
    return positions


def _top_movers(holdings: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    candidates = []
    for position in _flatten_positions(holdings):
        profit_rate = position.get("profit_rate")
        if profit_rate is None:
            continue
        try:
            abs_rate = abs(float(profit_rate))
        except (TypeError, ValueError):
            continue
        candidates.append((abs_rate, position))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "symbol": row.get("symbol"),
            "account": row.get("account"),
            "profit_rate": row.get("profit_rate"),
            "profit_loss": row.get("profit_loss"),
            "evaluation_amount": row.get("evaluation_amount"),
        }
        for _, row in candidates[:limit]
    ]


async def _latest_report_summary(
    db: Any,
    *,
    market: str,
    account_scope: str,
) -> dict[str, Any] | None:
    service = InvestmentReportQueryService(db)
    reports = await service.list_reports(
        market=market,
        account_scope=account_scope,
        limit=20,
    )
    advisory_profiles = _advisory_draft_profiles()
    report = next(
        (
            row
            for row in reports
            if getattr(row, "created_by_profile", None) in advisory_profiles
        ),
        None,
    )
    if report is None:
        return None
    bundle = await service.get_bundle(report.report_uuid)
    items = bundle["items"] if bundle is not None else []
    by_status: dict[str, int] = {}
    top_items: list[dict[str, Any]] = []
    for item in items:
        status = str(getattr(item, "status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1
        if len(top_items) < 5:
            top_items.append(
                {
                    "item_uuid": str(item.item_uuid),
                    "symbol": item.symbol,
                    "item_kind": item.item_kind,
                    "intent": item.intent,
                    "status": item.status,
                    "rationale": item.rationale,
                }
            )
    return {
        "report_uuid": str(report.report_uuid),
        "title": report.title,
        "status": report.status,
        "created_by_profile": report.created_by_profile,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "items": {
            "total": len(items),
            "by_status": by_status,
            "top": top_items,
        },
    }


async def _recent_session_context(
    db: Any,
    *,
    market: str,
    account_scope: str,
    limit: int,
) -> dict[str, Any]:
    service = SessionContextService(db)
    rows = await service.get_recent(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        limit=max(1, min(int(limit), 100)),
    )
    return {
        "count": len(rows),
        "entries": [
            SessionContextResponse.model_validate(row).model_dump(mode="json")
            for row in rows
        ],
    }


async def get_operating_briefing_impl(
    market: str,
    account_scope: str | None = None,
    session_context_limit: int = 10,
    include_current_price: bool = True,
) -> dict[str, Any]:
    as_of = now_kst()
    effective_scope = _default_account_scope(market, account_scope)
    holdings = await _get_holdings_impl(
        **_holdings_kwargs(market, effective_scope, include_current_price)
    )
    async with AsyncSessionLocal() as db:
        pending = await collect_pending_orders_snapshot(
            db,
            market=market,
            account_scope=effective_scope,
        )
        latest_report = await _latest_report_summary(
            db,
            market=market,
            account_scope=effective_scope,
        )
        session_context = await _recent_session_context(
            db,
            market=market,
            account_scope=effective_scope,
            limit=session_context_limit,
        )
    active_watches = await list_active_watches_impl(market=market)
    response = {
        "success": True,
        "market": market,
        "account_scope": effective_scope,
        "as_of": as_of.isoformat(),
        "staleness": {
            "holdings": {
                "as_of": as_of.isoformat(),
                "freshness_status": "live_or_best_effort",
                "errors": holdings.get("errors") or [],
            },
            "pending_orders": {
                "as_of": pending.as_of,
                "freshness_status": pending.freshness_status,
                "unavailable_reason": pending.unavailable_reason,
            },
            "active_watches": {
                "as_of": active_watches.get("as_of"),
                "freshness_status": "db_read",
            },
            "latest_report": {
                "freshness_status": "db_read" if latest_report else "not_found",
            },
            "session_context": {
                "freshness_status": "db_read",
            },
        },
        "holdings": {
            "filters": holdings.get("filters"),
            "total_accounts": holdings.get("total_accounts"),
            "total_positions": holdings.get("total_positions"),
            "summary": holdings.get("summary"),
            "top_movers": _top_movers(holdings),
            "errors": holdings.get("errors") or [],
        },
        "pending_orders": {
            "count": len(pending.orders or []),
            "orders": pending.orders,
            "unavailable_reason": pending.unavailable_reason,
        },
        "active_watches": {
            "count": active_watches.get("count", 0),
            "watches": active_watches.get("active_watches", []),
        },
        "latest_report": latest_report,
        "session_context": session_context,
    }
    return response


__all__ = [
    "get_operating_briefing_impl",
    "list_active_watches_impl",
]
