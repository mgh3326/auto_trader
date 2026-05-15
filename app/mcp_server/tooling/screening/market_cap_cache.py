"""CoinGecko market-cap cache for MCP screening."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.mcp_server.tooling.shared import _to_optional_float, _to_optional_int

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


def _rank_priority(rank: int | None) -> int:
    if rank is None or rank <= 0:
        return 1_000_000_000
    return rank


class MarketCapCache:
    def __init__(self, ttl: int = 600) -> None:
        self.ttl = ttl
        self._lock = asyncio.Lock()
        self._symbol_map: dict[str, dict[str, Any]] = {}
        self._updated_at: float | None = None

    def _age_seconds(self, now: float) -> float | None:
        if self._updated_at is None:
            return None
        return round(max(0.0, now - self._updated_at), 3)

    def _is_fresh(self, now: float) -> bool:
        if not self._symbol_map or self._updated_at is None:
            return False
        return (now - self._updated_at) <= self.ttl

    async def _fetch_market_caps(self) -> dict[str, dict[str, Any]]:
        params = {
            "vs_currency": "krw",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
            "sparkline": "false",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(COINGECKO_MARKETS_URL, params=params)
            response.raise_for_status()
            rows = response.json()

        if not isinstance(rows, list):
            raise ValueError("Unexpected CoinGecko response format")

        symbol_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            market_cap = _to_optional_float(row.get("market_cap"))
            market_cap_rank = _to_optional_int(row.get("market_cap_rank"))

            selected = {
                "market_cap": market_cap,
                "market_cap_rank": market_cap_rank,
            }
            existing = symbol_map.get(symbol)
            if existing is None or _rank_priority(market_cap_rank) < _rank_priority(
                _to_optional_int(existing.get("market_cap_rank"))
            ):
                symbol_map[symbol] = selected

        return symbol_map

    async def get(self) -> dict[str, Any]:
        now = time.time()
        if self._is_fresh(now):
            return {
                "data": self._symbol_map,
                "cached": True,
                "age_seconds": self._age_seconds(now),
                "stale": False,
                "error": None,
            }

        async with self._lock:
            now = time.time()
            if self._is_fresh(now):
                return {
                    "data": self._symbol_map,
                    "cached": True,
                    "age_seconds": self._age_seconds(now),
                    "stale": False,
                    "error": None,
                }
            try:
                fetched = await self._fetch_market_caps()
                self._symbol_map = fetched
                self._updated_at = now
                return {
                    "data": fetched,
                    "cached": False,
                    "age_seconds": 0.0,
                    "stale": False,
                    "error": None,
                }
            except Exception as exc:
                if self._symbol_map:
                    return {
                        "data": self._symbol_map,
                        "cached": True,
                        "age_seconds": self._age_seconds(now),
                        "stale": True,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                return {
                    "data": {},
                    "cached": False,
                    "age_seconds": None,
                    "stale": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
