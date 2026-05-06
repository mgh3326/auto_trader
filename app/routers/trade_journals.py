# app/routers/trade_journals.py
"""ROB-120 — Trade journal operator endpoints."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
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

api_router = APIRouter(prefix="/api/trade-journals", tags=["trade-journals"])
router = APIRouter()


@api_router.get("/coverage", response_model=JournalCoverageResponse)
async def get_journal_coverage(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[
        Literal["KR", "US", "CRYPTO"] | None,
        Query(description="Optional market filter"),
    ] = None,
) -> JournalCoverageResponse:
    svc = TradeJournalCoverageService(db)
    return await svc.build_coverage(user_id=user.id, market_filter=market)


@api_router.get("/retrospective", response_model=list[JournalReadResponse])
async def get_journal_retrospective(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[JournalReadResponse]:
    """List journals in terminal status (closed/stopped/expired)."""
    svc = TradeJournalReadService(db)
    return await svc.list_retrospective()


@api_router.post("", response_model=JournalReadResponse)
async def create_journal(
    req: JournalCreateRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JournalReadResponse:
    svc = TradeJournalWriteService(db)
    try:
        return await svc.create(req)
    except JournalWriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@api_router.patch("/{journal_id}", response_model=JournalReadResponse)
async def update_journal(
    journal_id: Annotated[int, Path(ge=1)],
    req: JournalUpdateRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JournalReadResponse:
    svc = TradeJournalWriteService(db)
    try:
        return await svc.update(journal_id, req)
    except JournalWriteError as exc:
        if "not found" in str(exc):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc


router.include_router(api_router)
router.include_router(api_router, prefix="/trading")
