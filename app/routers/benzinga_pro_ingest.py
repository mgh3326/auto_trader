"""Token-authenticated server-side capture ingest (hk 6579)."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.schemas.benzinga_pro_ingest import (
    MAX_BODY_BYTES,
    BenzingaProCapture,
    IngestCounts,
)
from app.services.invest_momentum_events.benzinga_pro import store_capture

router = APIRouter(prefix="/trading", tags=["benzinga-pro-ingest"])


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


async def _read_capture(request: Request) -> BenzingaProCapture:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Capture too large")
        body.extend(chunk)
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        return BenzingaProCapture.model_validate(payload)
    except (ValueError, RecursionError):
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
