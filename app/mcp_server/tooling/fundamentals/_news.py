# app/mcp_server/tooling/fundamentals/_news.py
"""Handler for get_news tool (routes through symbol_news_service, ROB-423)."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_market_with_crypto
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    is_crypto_market as _is_crypto_market,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input,
)
from app.services import symbol_news_service

_INSTRUMENT_BY_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


async def handle_get_news(
    symbol: str | int,
    market: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        elif _is_crypto_market(symbol):
            market = "crypto"
        else:
            market = "us"

    normalized_market = normalize_market_with_crypto(market)
    capped_limit = min(max(limit, 1), 50)
    instrument_type = _INSTRUMENT_BY_MARKET.get(normalized_market, "equity_us")

    result = await symbol_news_service.fetch_symbol_news(
        symbol, normalized_market, instrument_type, limit=capped_limit
    )

    if result.status in ("error", "unavailable"):
        return _error_payload(
            source=result.provider,
            message=result.error_code or "news_unavailable",
            symbol=symbol,
            instrument_type=instrument_type,
        )

    news = []
    for article in result.articles:
        source_item = article.provider_metadata.get("source_item", {})
        item = dict(source_item) if isinstance(source_item, dict) else {}
        if relevance := article.provider_metadata.get("relevance"):
            item["relevance"] = relevance
        news.append(item)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "market": normalized_market,
        "source": result.provider,
        "count": len(news),
        "excluded_count": result.excluded_count,
        "news": news,
    }
    if result.degraded:
        payload["degraded"] = True
        payload["fetch_error"] = result.fetch_error
    return payload
