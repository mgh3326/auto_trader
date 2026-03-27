"""Handler for get_market_index tool."""

from __future__ import annotations

import asyncio
from typing import Any

from app.mcp_server.tooling.fundamentals_sources_indices import (
    _DEFAULT_INDICES,
    _INDEX_META,
    _fetch_index_kr_current,
    _fetch_index_kr_history,
    _fetch_index_us_current,
    _fetch_index_us_history,
)
from app.mcp_server.tooling.shared import error_payload as _error_payload


async def handle_get_market_index(
    symbol: str | None = None,
    period: str = "day",
    count: int = 20,
) -> dict[str, Any]:
    period = (period or "day").strip().lower()
    if period not in ("day", "week", "month"):
        raise ValueError("period must be 'day', 'week', or 'month'")

    capped_count = min(max(count, 1), 100)

    if symbol:
        sym = symbol.strip().upper()
        meta = _INDEX_META.get(sym)
        if meta is None:
            raise ValueError(
                f"Unknown index symbol '{sym}'. Supported: {', '.join(sorted(_INDEX_META))}"
            )

        try:
            if meta["source"] == "naver":
                current_data, history = await asyncio.gather(
                    _fetch_index_kr_current(meta["naver_code"], meta["name"]),
                    _fetch_index_kr_history(meta["naver_code"], capped_count, period),
                )
            else:
                current_data, history = await asyncio.gather(
                    _fetch_index_us_current(meta["yf_ticker"], meta["name"], sym),
                    _fetch_index_us_history(meta["yf_ticker"], capped_count, period),
                )
            return {"indices": [current_data], "history": history}
        except Exception as exc:
            return _error_payload(source=meta["source"], message=str(exc), symbol=sym)

    tasks = []
    for idx_sym in _DEFAULT_INDICES:
        meta = _INDEX_META[idx_sym]
        if meta["source"] == "naver":
            tasks.append(_fetch_index_kr_current(meta["naver_code"], meta["name"]))
        else:
            tasks.append(
                _fetch_index_us_current(meta["yf_ticker"], meta["name"], idx_sym)
            )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    indices: list[dict[str, Any]] = []
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            indices.append({"symbol": _DEFAULT_INDICES[i], "error": str(r)})
        elif isinstance(r, dict):
            indices.append(r)
        else:
            indices.append({"symbol": _DEFAULT_INDICES[i], "error": str(r)})

    return {"indices": indices}
