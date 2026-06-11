"""Read-only operating briefing MCP tools for ROB-517."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.schemas.investment_reports import (
    ActiveWatchesListResponse,
    InvestmentWatchAlertResponse,
)
from app.services.investment_reports.repository import InvestmentReportsRepository


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


async def get_operating_briefing_impl(
    market: str,
    account_scope: str | None = None,
    session_context_limit: int = 10,
    include_current_price: bool = True,
) -> dict[str, Any]:
    raise NotImplementedError("Task 5 implements get_operating_briefing_impl")


__all__ = [
    "get_operating_briefing_impl",
    "list_active_watches_impl",
]
