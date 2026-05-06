"""ROB-123 — read-only `/invest/api`.

이 라우터는 `InvestHomeService` 만 의존하고 broker / KIS / Upbit 클라이언트를 직접
import 하지 않는다. order / watch / scheduler / mutation 경로 import 금지.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_home import InvestHomeResponse
from app.services.invest_home_readers import (
    KISHomeReader,
    ManualHomeReader,
    UpbitHomeReader,
)
from app.services.invest_home_service import InvestHomeService

router = APIRouter(prefix="/invest/api", tags=["invest"])


def get_invest_home_service(db: AsyncSession = Depends(get_db)) -> InvestHomeService:
    return InvestHomeService(
        kis_reader=KISHomeReader(db),
        upbit_reader=UpbitHomeReader(db),
        manual_reader=ManualHomeReader(db),
    )


@router.get("/home", response_model=InvestHomeResponse)
async def get_home(
    user=Depends(get_authenticated_user),
    service: InvestHomeService = Depends(get_invest_home_service),
) -> InvestHomeResponse:
    return await service.get_home(user_id=user.id)
