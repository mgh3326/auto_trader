"""Finnhub provider helpers for fundamentals domain."""

from __future__ import annotations

from typing import Any

import app.mcp_server.tooling.fundamentals_sources_naver as _naver_sources


def _get_finnhub_client() -> Any:
    return _naver_sources._get_finnhub_client()


async def _fetch_news_finnhub(symbol: str, market: str, limit: int) -> dict[str, Any]:
    return await _naver_sources._fetch_news_finnhub(symbol, market, limit)


async def _fetch_company_profile_finnhub(symbol: str) -> dict[str, Any]:
    return await _naver_sources._fetch_company_profile_finnhub(symbol)


async def _fetch_financials_finnhub(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    return await _naver_sources._fetch_financials_finnhub(symbol, statement, freq)


async def _fetch_insider_transactions_finnhub(
    symbol: str, limit: int
) -> dict[str, Any]:
    return await _naver_sources._fetch_insider_transactions_finnhub(symbol, limit)


async def _fetch_earnings_calendar_finnhub(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    return await _naver_sources._fetch_earnings_calendar_finnhub(
        symbol=symbol,
        from_date=from_date,
        to_date=to_date,
    )


__all__ = [
    "_fetch_company_profile_finnhub",
    "_fetch_earnings_calendar_finnhub",
    "_fetch_financials_finnhub",
    "_fetch_insider_transactions_finnhub",
    "_fetch_news_finnhub",
    "_get_finnhub_client",
]
