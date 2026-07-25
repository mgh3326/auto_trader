"""FastAPI router for /invest operator session-context read surface (ROB-664).

Read-only exposure of ``review.operator_session_context`` (ROB-516): the
append-only operator handoff log (entry_type: plan/decision/handoff_note/...),
newest first, with market/account_scope/entry_type/kst_date_from filters.
``append_entries`` stays MCP-only; no mutation is reachable from here.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, get_args

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.investment_reports import AccountScopeLiteral, MarketLiteral
from app.schemas.session_context import (
    SessionContextEntryTypeLiteral,
    SessionContextRecentRequest,
    SessionContextRecentResponse,
    SessionContextResponse,
)
from app.services.session_context import SessionContextService

router = APIRouter(
    prefix="/trading/api/invest/session-context",
    tags=["invest-session-context"],
)

_VALID_MARKETS = frozenset(get_args(MarketLiteral))
_VALID_ACCOUNT_SCOPES = frozenset(get_args(AccountScopeLiteral))
_VALID_ENTRY_TYPES = frozenset(get_args(SessionContextEntryTypeLiteral))


def _validate(name: str, value: str | None, allowed: frozenset[str]) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(status_code=422, detail=f"invalid {name}: {value}")


@router.get("/recent")
async def list_recent_session_context(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[str | None, Query()] = None,
    account_scope: Annotated[str | None, Query()] = None,
    entry_type: Annotated[str | None, Query()] = None,
    kst_date_from: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SessionContextRecentResponse:
    _validate("market", market, _VALID_MARKETS)
    _validate("account_scope", account_scope, _VALID_ACCOUNT_SCOPES)
    _validate("entry_type", entry_type, _VALID_ENTRY_TYPES)
    svc = SessionContextService(db)
    rows = await svc.get_recent(
        market=market,
        account_scope=account_scope,
        entry_type=entry_type,
        kst_date_from=kst_date_from,
        limit=limit,
    )
    filters = SessionContextRecentRequest(
        market=market,
        account_scope=account_scope,
        entry_type=entry_type,
        kst_date_from=kst_date_from,
        limit=limit,
    )
    entries = [SessionContextResponse.model_validate(r) for r in rows]
    return SessionContextRecentResponse(
        count=len(entries), filters=filters, entries=entries
    )
