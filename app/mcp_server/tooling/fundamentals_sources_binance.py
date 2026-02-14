"""Binance/crypto derivatives provider helpers."""

from __future__ import annotations

from typing import Any

import app.mcp_server.tooling.fundamentals_sources_naver as _naver_sources


async def _fetch_funding_rate_batch(symbols: list[str]) -> list[dict[str, Any]]:
    return await _naver_sources._fetch_funding_rate_batch(symbols)


async def _fetch_funding_rate(symbol: str, limit: int) -> dict[str, Any]:
    return await _naver_sources._fetch_funding_rate(symbol, limit)


__all__ = ["_fetch_funding_rate", "_fetch_funding_rate_batch"]
