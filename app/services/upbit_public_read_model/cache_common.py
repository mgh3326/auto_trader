"""Internal cache helpers for Upbit public read-model modules."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def cache_enabled() -> bool:
    return bool(getattr(settings, "upbit_public_read_model_cache_enabled", True))


def classify_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return "rate_limited" if exc.response.status_code == 429 else "http_error"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    return "unknown"


async def read_json(redis_client: Any, key: str) -> dict[str, Any] | None:
    if not cache_enabled():
        return None
    try:
        raw = await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001 — cache outage should degrade to miss
        logger.warning("upbit_public_read_model cache read failed key=%s: %s", key, exc)
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return None
    for field in ("fetchedAt", "cachedAt"):
        if obj.get(field):
            obj[field] = datetime.fromisoformat(obj[field])
    return obj


async def write_json(
    redis_client: Any, key: str, payload: dict[str, Any], *, ex: int
) -> None:
    if not cache_enabled():
        return

    def default(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    try:
        await redis_client.set(key, json.dumps(payload, default=default), ex=ex)
    except Exception as exc:  # noqa: BLE001 — fresh upstream data is still usable
        logger.warning(
            "upbit_public_read_model cache write failed key=%s: %s", key, exc
        )
