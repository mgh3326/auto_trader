"""Service boundary for market index quotes used by runtime jobs."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals_sources_indices import _fetch_index_kr_current

_KR_INDEX_NAMES = {
    "KOSPI": "KOSPI",
    "KOSDAQ": "KOSDAQ",
}


async def get_kr_index_quote(symbol: str) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol not in _KR_INDEX_NAMES:
        raise ValueError("KR index symbol must be one of: KOSPI, KOSDAQ")

    return await _fetch_index_kr_current(
        normalized_symbol,
        _KR_INDEX_NAMES[normalized_symbol],
    )
