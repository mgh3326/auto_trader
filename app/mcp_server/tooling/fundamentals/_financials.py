"""Handlers for get_financials, get_insider_transactions, get_earnings_calendar tools."""

from __future__ import annotations

import datetime
from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_equity_market
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_earnings_calendar_finnhub,
    _fetch_financials_finnhub,
    _fetch_insider_transactions_finnhub,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_financials_naver,
    _fetch_financials_yfinance,
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


async def handle_get_financials(
    symbol: str,
    statement: str = "income",
    freq: str = "annual",
    market: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    statement = (statement or "income").strip().lower()
    if statement not in ("income", "balance", "cashflow"):
        raise ValueError("statement must be 'income', 'balance', or 'cashflow'")

    freq = (freq or "annual").strip().lower()
    if freq not in ("annual", "quarterly"):
        raise ValueError("freq must be 'annual' or 'quarterly'")

    if _is_crypto_market(symbol):
        raise ValueError(
            "Financial statements are not available for cryptocurrencies"
        )

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    normalized_market = normalize_equity_market(market)

    try:
        if normalized_market == "kr":
            return await _fetch_financials_naver(symbol, statement, freq)
        try:
            return await _fetch_financials_finnhub(symbol, statement, freq)
        except (ValueError, Exception):
            return await _fetch_financials_yfinance(symbol, statement, freq)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "yfinance"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def handle_get_insider_transactions(
    symbol: str,
    limit: int = 20,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    capped_limit = min(max(limit, 1), 100)

    if _is_crypto_market(symbol):
        raise ValueError("Insider transactions are only available for US stocks")
    if _is_korean_equity_code(symbol):
        raise ValueError("Insider transactions are only available for US stocks")

    try:
        return await _fetch_insider_transactions_finnhub(symbol, capped_limit)
    except Exception as exc:
        return _error_payload(
            source="finnhub",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_us",
        )


async def handle_get_earnings_calendar(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip() if symbol else None

    if symbol:
        if _is_crypto_market(symbol):
            raise ValueError("Earnings calendar is only available for US stocks")
        if _is_korean_equity_code(symbol):
            raise ValueError("Earnings calendar is only available for US stocks")

    if from_date:
        try:
            datetime.date.fromisoformat(from_date)
        except ValueError:
            raise ValueError("from_date must be ISO format (e.g., '2024-01-15')")

    if to_date:
        try:
            datetime.date.fromisoformat(to_date)
        except ValueError:
            raise ValueError("to_date must be ISO format (e.g., '2024-01-15')")

    try:
        return await _fetch_earnings_calendar_finnhub(symbol, from_date, to_date)
    except Exception as exc:
        return _error_payload(
            source="finnhub",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_us",
        )
