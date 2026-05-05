"""ROB-117 — Candidate Discovery router (read-only)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.candidate_discovery import (
    CandidateScreenRequest,
    CandidateScreenResponse,
)
from app.services.candidate_screening_service import CandidateScreeningService

api_router = APIRouter(prefix="/api/candidates", tags=["candidate-discovery"])
router = APIRouter()


def get_candidate_screening_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CandidateScreeningService:
    return CandidateScreeningService(db)


@api_router.post("/screen", response_model=CandidateScreenResponse)
async def screen_candidates(
    payload: CandidateScreenRequest,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        CandidateScreeningService, Depends(get_candidate_screening_service)
    ],
) -> CandidateScreenResponse:
    return await service.screen(
        user_id=current_user.id,
        market=payload.market,
        asset_type=payload.asset_type,
        strategy=payload.strategy,
        sort_by=payload.sort_by,
        sort_order=payload.sort_order,
        min_market_cap=payload.min_market_cap,
        max_per=payload.max_per,
        max_pbr=payload.max_pbr,
        min_dividend_yield=payload.min_dividend_yield,
        max_rsi=payload.max_rsi,
        adv_krw_min=payload.adv_krw_min,
        market_cap_min_krw=payload.market_cap_min_krw,
        market_cap_max_krw=payload.market_cap_max_krw,
        exclude_sectors=payload.exclude_sectors,
        instrument_types=payload.instrument_types,
        krw_only=payload.krw_only,
        exclude_warnings=payload.exclude_warnings,
        limit=payload.limit,
    )


router.include_router(api_router)
router.include_router(api_router, prefix="/trading")
