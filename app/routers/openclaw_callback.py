from typing import Literal

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.models import PriceAnalysis
from app.core.config import settings
from app.core.db import get_db
from app.models.analysis import StockAnalysisResult
from app.services.stock_info_service import create_stock_if_not_exists

router = APIRouter(prefix="/api/v1/openclaw", tags=["OpenClaw"])


def _extract_bearer_token(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2:
        return None
    if parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


async def _require_openclaw_callback_token(request: Request) -> None:
    expected = settings.OPENCLAW_CALLBACK_TOKEN.strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OPENCLAW_CALLBACK_TOKEN is not configured",
        )

    provided = _extract_bearer_token(request.headers.get("authorization"))
    if provided is None:
        provided = request.headers.get("x-openclaw-token")
        provided = provided.strip() if provided else None

    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OpenClaw callback token",
            headers={"WWW-Authenticate": "Bearer"},
        )


class OpenClawCallbackRequest(BaseModel):
    request_id: str = Field(description="Correlation ID for this analysis request")
    symbol: str = Field(description="Instrument symbol/ticker")
    name: str = Field(description="Instrument display name")
    instrument_type: str = Field(
        description="Instrument type (equity_kr, equity_us, crypto, etc)"
    )

    decision: Literal["buy", "hold", "sell"] = Field(
        description="Investment decision (buy, hold, sell)"
    )
    confidence: int = Field(description="Confidence (0-100)", ge=0, le=100)
    reasons: list[str] | None = Field(
        default=None, description="Decision reasons (up to 3)"
    )

    price_analysis: PriceAnalysis
    detailed_text: str | None = None

    # Upstream model name (stored in detailed_text; DB model_name is fixed)
    model_name: str | None = None

    # Optional: if upstream provides original prompt, we store it.
    prompt: str | None = None


@router.post("/callback")
async def openclaw_callback(
    payload: OpenClawCallbackRequest,
    _: None = Depends(_require_openclaw_callback_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stock_info = await create_stock_if_not_exists(
        symbol=payload.symbol,
        name=payload.name,
        instrument_type=payload.instrument_type,
    )

    prompt = payload.prompt or (
        f"[openclaw request_id={payload.request_id}] {payload.symbol} ({payload.name})"
    )

    detailed_text = payload.detailed_text
    if payload.model_name:
        prefix = f"[openclaw upstream_model={payload.model_name}]"
        detailed_text = f"{prefix}\n{detailed_text or ''}".rstrip() or prefix

    record = StockAnalysisResult(
        stock_info_id=stock_info.id,
        prompt=prompt,
        model_name="openclaw-gpt",
        decision=payload.decision,
        confidence=payload.confidence,
        appropriate_buy_min=payload.price_analysis.appropriate_buy_range.min,
        appropriate_buy_max=payload.price_analysis.appropriate_buy_range.max,
        appropriate_sell_min=payload.price_analysis.appropriate_sell_range.min,
        appropriate_sell_max=payload.price_analysis.appropriate_sell_range.max,
        buy_hope_min=payload.price_analysis.buy_hope_range.min,
        buy_hope_max=payload.price_analysis.buy_hope_range.max,
        sell_target_min=payload.price_analysis.sell_target_range.min,
        sell_target_max=payload.price_analysis.sell_target_range.max,
        reasons=payload.reasons,
        detailed_text=detailed_text,
    )

    db.add(record)
    await db.commit()
    await db.refresh(record)

    return {
        "status": "ok",
        "request_id": payload.request_id,
        "analysis_result_id": record.id,
    }
