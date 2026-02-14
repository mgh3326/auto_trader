"""Market index provider helpers for fundamentals domain."""

from __future__ import annotations

from typing import Any

import app.mcp_server.tooling.fundamentals_sources_naver as _naver_sources

_INDEX_META: dict[str, dict[str, str]] = _naver_sources._INDEX_META
_DEFAULT_INDICES = _naver_sources._DEFAULT_INDICES


async def _fetch_index_kr_current(naver_code: str, name: str) -> dict[str, Any]:
    return await _naver_sources._fetch_index_kr_current(naver_code, name)


async def _fetch_index_kr_history(
    naver_code: str, count: int, period: str
) -> list[dict[str, Any]]:
    return await _naver_sources._fetch_index_kr_history(naver_code, count, period)


async def _fetch_index_us_current(
    yf_ticker: str, name: str, symbol: str
) -> dict[str, Any]:
    return await _naver_sources._fetch_index_us_current(yf_ticker, name, symbol)


async def _fetch_index_us_history(
    yf_ticker: str, count: int, period: str
) -> list[dict[str, Any]]:
    return await _naver_sources._fetch_index_us_history(yf_ticker, count, period)


__all__ = [
    "_DEFAULT_INDICES",
    "_INDEX_META",
    "_fetch_index_kr_current",
    "_fetch_index_kr_history",
    "_fetch_index_us_current",
    "_fetch_index_us_history",
]
