"""Handlers for valuation and equity-analysis tools.

Includes: get_valuation, get_investment_opinions, get_investor_trends, get_short_interest.
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_equity_market
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_investment_opinions_naver,
    _fetch_valuation_naver,
)
from app.mcp_server.tooling.fundamentals_sources_yfinance import (
    _fetch_investment_opinions_yfinance,
    _fetch_valuation_yfinance,
)
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


async def handle_get_valuation(
    symbol: str | int,
    market: str | None = None,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError("Valuation metrics are not available for cryptocurrencies")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    normalized_market = normalize_equity_market(market)

    try:
        if normalized_market == "kr":
            return await _fetch_valuation_naver(symbol)
        return await _fetch_valuation_yfinance(symbol)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "yfinance"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def handle_get_investment_opinions(
    symbol: str | int,
    limit: int = 10,
    market: str | None = None,
    opinion_window_months: int = 12,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError("Investment opinions are not available for cryptocurrencies")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    if not market:
        raise ValueError("market is required")

    normalized_market = normalize_equity_market(str(market))
    capped_limit = min(max(limit, 1), 30)
    # ROB-486: KR 컨센서스 recency 윈도우 (1~60개월 클램프). US(yfinance)는
    # 벤더 컨센서스를 그대로 쓰므로 적용되지 않는다.
    capped_window = min(max(opinion_window_months, 1), 60)

    try:
        if normalized_market == "kr":
            return await _fetch_investment_opinions_naver(
                symbol, capped_limit, window_months=capped_window
            )
        return await _fetch_investment_opinions_yfinance(symbol, capped_limit)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "yfinance"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )
