# app/routers/health.py
import logging
from datetime import UTC, datetime

import redis.asyncio as redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import settings
from app.core.db import engine

router = APIRouter(tags=["Health"])
logger = logging.getLogger(__name__)


class HealthOut(BaseModel):
    status: str = "ok"
    timestamp: datetime
    version: str = "0.1.0"


class ReadyOut(BaseModel):
    status: str
    timestamp: datetime
    checks: dict[str, str]


async def check_database_ready() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Database readiness check failed")
        return False


async def check_redis_ready() -> bool:
    client = redis.from_url(settings.get_redis_url())
    try:
        pong = await client.ping()
        return bool(pong)
    except Exception:
        logger.exception("Redis readiness check failed")
        return False
    finally:
        await client.aclose()


@router.get("/healthz", response_model=HealthOut, include_in_schema=False)
async def healthz() -> HealthOut:
    """
    Kubernetes / AWS ALB 헬스체크를 위한 엔드포인트.
    Docs 에 노출할 필요 없어서 include_in_schema=False.
    """
    return HealthOut(timestamp=datetime.now(UTC))


@router.get("/readyz", response_model=ReadyOut, include_in_schema=False)
async def readyz() -> ReadyOut | JSONResponse:
    db_ready, redis_ready = await check_database_ready(), await check_redis_ready()
    checks = {
        "database": "ok" if db_ready else "error",
        "redis": "ok" if redis_ready else "error",
    }

    if db_ready and redis_ready:
        return ReadyOut(
            status="ready",
            timestamp=datetime.now(UTC),
            checks=checks,
        )

    body = ReadyOut(
        status="not_ready",
        timestamp=datetime.now(UTC),
        checks=checks,
    )
    return JSONResponse(status_code=503, content=body.model_dump(mode="json"))
