# app/routers/health.py
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["Health"])


class HealthOut(BaseModel):
    status: str = "ok"
    timestamp: datetime
    version: str = "0.1.0"


@router.get("/healthz", response_model=HealthOut, include_in_schema=False)
async def healthz() -> HealthOut:
    """
    Kubernetes / AWS ALB 헬스체크를 위한 엔드포인트.
    Docs 에 노출할 필요 없어서 include_in_schema=False.
    """
    return HealthOut(timestamp=datetime.now(timezone.utc))