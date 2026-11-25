"""Token-authenticated server-side capture ingest (hk 6579)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.schemas.benzinga_pro_ingest import (
    MAX_BODY_BYTES,
    BenzingaProCapture,
    IngestCounts,
)
from app.services.invest_momentum_events.benzinga_pro import store_capture

router = APIRouter(prefix="/trading", tags=["benzinga-pro-ingest"])


async def _read_capture(request: Request) -> BenzingaProCapture:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Capture too large")
        body.extend(chunk)
    try:
        return BenzingaProCapture.model_validate_json(body)
    except ValidationError:
        # A rejected payload may carry credentials in unexpected fields.
        raise HTTPException(status_code=422, detail="Invalid capture payload") from None


@router.post("/api/research/benzinga-pro/ingest")
async def ingest_benzinga_pro(
    capture: Annotated[BenzingaProCapture, Depends(_read_capture)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IngestCounts:
    counts = await store_capture(db, capture)
    await db.commit()
    return counts
