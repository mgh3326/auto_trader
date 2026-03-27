"""Handler for get_news tool."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_market_with_crypto
from app.mcp_server.tooling.fundamentals_sources_finnhub import _fetch_news_finnhub
from app.mcp_server.tooling.fundamentals_sources_naver import _fetch_news_naver
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

    try:
        if normalized_market == "kr":
            return await _fetch_news_naver(symbol, capped_limit)
        return await _fetch_news_finnhub(symbol, normalized_market, capped_limit)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "finnhub"
        instrument_type = {
            "kr": "equity_kr",
            "us": "equity_us",
            "crypto": "crypto",
        }.get(normalized_market, "equity_us")
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )
