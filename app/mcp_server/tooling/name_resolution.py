import logging
from typing import Any

from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.upbit_symbol_universe_service import get_upbit_market_display_names
from app.services.us_symbol_universe_service import get_us_names_by_symbols

logger = logging.getLogger(__name__)


async def resolve_names(
    symbols: list[str], market_type: str
) -> dict[str, dict[str, Any]]:
    results = {}
    if not symbols:
        return results
    if market_type == "equity_kr":
        try:
            names = await get_kr_names_by_symbols(symbols)
        except Exception as exc:
            logger.warning("KR name resolution failed, using fallback: %s", exc)
            names = {}
        for sym in symbols:
            name = names.get(sym)
            results[sym] = {"name": name or sym, "name_resolved": name is not None}
    elif market_type == "equity_us":
        try:
            names = await get_us_names_by_symbols(symbols)
        except Exception as exc:
            logger.warning("US name resolution failed, using fallback: %s", exc)
            names = {}
        for sym in symbols:
            name = names.get(sym)
            results[sym] = {"name": name or sym, "name_resolved": name is not None}
    elif market_type == "crypto":
        # upbit_service returns {market: {"korean_name": ..., "english_name": ...}}
        try:
            display_info = await get_upbit_market_display_names(symbols)
        except Exception as exc:
            logger.warning("Crypto name resolution failed, using fallback: %s", exc)
            display_info = {}
        for sym in symbols:
            info = display_info.get(sym)
            name = (
                (info.get("korean_name") or info.get("english_name")) if info else None
            )
            results[sym] = {"name": name or sym, "name_resolved": name is not None}
    else:
        for sym in symbols:
            results[sym] = {"name": sym, "name_resolved": False}
    return results
