"""Handler for get_sector_peers tool."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_sector_peers_naver,
)
from app.mcp_server.tooling.fundamentals_sources_yfinance import (
    _fetch_sector_peers_us,
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
    is_us_equity_symbol as _is_us_equity_symbol,
)

# NOTE: These alias sets intentionally differ from _helpers.py — sector_peers
# does NOT accept "equity_kr"/"equity_us" to preserve the original tool contract.
_KR_ALIASES = frozenset({"kr", "krx", "korea", "kospi", "kosdaq", "kis", "naver"})
_US_ALIASES = frozenset({"us", "usa", "nyse", "nasdaq", "yahoo"})


async def handle_get_sector_peers(
    symbol: str,
    market: str = "",
    limit: int = 5,
    manual_peers: list[str] | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError("Sector peers are not available for cryptocurrencies")

    capped_limit = min(max(limit, 1), 20)

    market_str = (market or "").strip().lower()
    if market_str in _KR_ALIASES:
        resolved_market = "kr"
    elif market_str in _US_ALIASES:
        resolved_market = "us"
    elif market_str == "":
        if _is_korean_equity_code(symbol):
            resolved_market = "kr"
        elif _is_us_equity_symbol(symbol):
            resolved_market = "us"
        else:
            raise ValueError(
                f"Cannot auto-detect market for symbol '{symbol}'. "
                "Please specify market='kr' or market='us'."
            )
    else:
        raise ValueError("market must be 'kr' or 'us'")

    try:
        if resolved_market == "kr":
            return await _fetch_sector_peers_naver(symbol, capped_limit, manual_peers)
        return await _fetch_sector_peers_us(symbol, capped_limit, manual_peers)
    except Exception as exc:
        source = "naver" if resolved_market == "kr" else "finnhub+yfinance"
        instrument_type = "equity_kr" if resolved_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )
