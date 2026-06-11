"""Finnhub news provider helpers for service-layer consumers.

This module intentionally avoids importing ``app.mcp_server`` so API/research
services can fetch Finnhub headlines without triggering MCP tool registration or
broker/order settings at import time.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

try:
    import finnhub
except ImportError:  # pragma: no cover - dependency presence varies by env
    finnhub = None


# ROB-510: 테스트에서 monkeypatch로 대기 제거 가능하도록 모듈 레벨 상수
FINNHUB_NEWS_RETRY_WAIT = wait_exponential_jitter(initial=0.5, max=2.0)


def _news_setting(name: str, default: Any) -> Any:
    """Lazy settings read — app config을 import 전제조건으로 만들지 않는다."""
    env_value = os.getenv(name)
    if env_value is not None:
        try:
            return type(default)(env_value)
        except (TypeError, ValueError):
            return default
    try:
        from app.core.config import settings
    except Exception:  # noqa: BLE001 — config 부재 환경에서도 동작
        return default
    return getattr(settings, name, default)


def _is_retryable_news_error(exc: BaseException) -> bool:
    """타임아웃/네트워크/5xx/429만 재시도. 4xx·설정오류는 즉시 실패."""
    if isinstance(exc, TimeoutError):
        return True
    if finnhub is not None and isinstance(exc, finnhub.FinnhubAPIException):
        status = getattr(exc, "status_code", None)
        return status == 429 or (isinstance(status, int) and status >= 500)
    try:
        import requests
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, requests.RequestException)


def _get_finnhub_api_key() -> str | None:
    """Return Finnhub API key without making app config an import prerequisite."""
    api_key = os.getenv("FINNHUB_API_KEY")
    if api_key:
        return api_key

    try:
        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unable to load app settings for Finnhub key: %s", exc)
        return None

    return getattr(settings, "finnhub_api_key", None)


def _get_finnhub_client() -> Any:
    if finnhub is None:
        raise ImportError("finnhub-python is required to use Finnhub providers")
    api_key = _get_finnhub_api_key()
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set")
    return finnhub.Client(api_key=api_key)


async def fetch_news_finnhub(
    symbol: str,
    market: str,
    limit: int,
    *,
    timeout_s: float | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Fetch and normalize Finnhub news using the existing MCP response shape.

    ROB-510: per-attempt timeout + bounded exponential-backoff retry.
    """
    client = _get_finnhub_client()
    per_attempt_timeout = (
        timeout_s
        if timeout_s is not None
        else float(_news_setting("FINNHUB_NEWS_TIMEOUT_S", 8.0))
    )
    attempts = (
        max_attempts
        if max_attempts is not None
        else int(_news_setting("FINNHUB_NEWS_MAX_ATTEMPTS", 3))
    )
    to_date = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=7)

    def fetch_sync() -> list[dict[str, Any]]:
        if market == "crypto":
            news = client.general_news("crypto", min_id=0)
        else:
            news = client.company_news(
                symbol.upper(),
                _from=from_date.strftime("%Y-%m-%d"),
                to=to_date.strftime("%Y-%m-%d"),
            )
        return news[:limit] if news else []

    news_items: list[dict[str, Any]] = []
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=FINNHUB_NEWS_RETRY_WAIT,
        retry=retry_if_exception(_is_retryable_news_error),
        reraise=True,
    ):
        with attempt:
            news_items = await asyncio.wait_for(
                asyncio.to_thread(fetch_sync), timeout=per_attempt_timeout
            )

    result_items = []
    for item in news_items:
        result_items.append(
            {
                "title": item.get("headline", ""),
                "source": item.get("source", ""),
                "datetime": datetime.datetime.fromtimestamp(
                    item.get("datetime", 0)
                ).isoformat()
                if item.get("datetime")
                else None,
                "url": item.get("url", ""),
                "summary": item.get("summary", ""),
                "sentiment": item.get("sentiment"),
                "related": item.get("related", ""),
            }
        )

    return {
        "symbol": symbol,
        "market": market,
        "source": "finnhub",
        "count": len(result_items),
        "news": result_items,
    }


__all__ = ["fetch_news_finnhub"]
