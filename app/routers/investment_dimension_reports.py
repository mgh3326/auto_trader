"""GET read surface for dimension reports (ROB-306, read-only)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.services.invest_view_model.dimension_report_view import (
    build_dimension_reports_view,
)

router = APIRouter(
    prefix="/trading/api/investment-reports", tags=["investment-reports"]
)


@router.get("/runs/{run_uuid}/dimension-reports")
async def get_dimension_reports(
    run_uuid: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    dimension: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    return await build_dimension_reports_view(
        db, run_uuid=run_uuid, dimension=dimension
    )
