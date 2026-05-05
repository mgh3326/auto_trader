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

logger = logging.getLogger(__name__)

try:
    import finnhub
except ImportError:  # pragma: no cover - dependency presence varies by env
    finnhub = None


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


async def fetch_news_finnhub(symbol: str, market: str, limit: int) -> dict[str, Any]:
    """Fetch and normalize Finnhub news using the existing MCP response shape."""
    client = _get_finnhub_client()
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

    news_items = await asyncio.to_thread(fetch_sync)

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
