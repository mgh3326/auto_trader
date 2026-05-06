# app/routers/trade_journals.py
"""ROB-120 — Trade journal operator endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.db import get_db
from app.models.trading import User
from app.schemas.trade_journal import (
    JournalCoverageResponse,
    JournalCreateRequest,
    JournalReadResponse,
    JournalUpdateRequest,
)
from app.services.trade_journal_coverage_service import TradeJournalCoverageService
from app.services.trade_journal_read_service import TradeJournalReadService
from app.services.trade_journal_write_service import (
    JournalWriteError,
    TradeJournalWriteService,
)

router = APIRouter(prefix="/trade-journals", tags=["Trade Journals"])


@router.get("/coverage", response_model=JournalCoverageResponse)
async def get_journal_coverage(
    market: str | None = Query(None, regex="^(KR|US|CRYPTO)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JournalCoverageResponse:
    svc = TradeJournalCoverageService(db)
    return await svc.build_coverage(user_id=user.id, market_filter=market)


@router.get("/retrospective", response_model=list[JournalReadResponse])
async def get_retrospective(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[JournalReadResponse]:
    svc = TradeJournalReadService(db)
    return await svc.list_retrospective()


@router.post("", response_model=JournalReadResponse)
async def create_journal(
    req: JournalCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JournalReadResponse:
    svc = TradeJournalWriteService(db)
    try:
        # Service ensures live account and draft/active status.
        return await svc.create(req)
    except JournalWriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{journal_id}", response_model=JournalReadResponse)
async def update_journal(
    journal_id: int,
    req: JournalUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JournalReadResponse:
    svc = TradeJournalWriteService(db)
    try:
        return await svc.update(journal_id, req)
    except JournalWriteError as exc:
        if "not found" in str(exc):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
