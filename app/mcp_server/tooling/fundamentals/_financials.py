"""Handlers for get_financials, get_insider_transactions, get_earnings_calendar tools."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.fundamentals._helpers import (
    normalize_equity_market,
    normalize_market_with_crypto,
)
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_earnings_calendar_finnhub,
    _fetch_financials_finnhub,
    _fetch_insider_transactions_finnhub,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_financials_naver,
)
from app.mcp_server.tooling.fundamentals_sources_yfinance import (
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
from app.services.market_events.query_service import MarketEventsQueryService


def _has_financial_values(payload: dict[str, Any]) -> bool:
    """Return true only when a provider supplied at least one real metric value."""

    def _has_value(value: Any) -> bool:
        if isinstance(value, dict):
            return any(_has_value(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(_has_value(item) for item in value)
        return value is not None

    metrics = payload.get("metrics")
    if isinstance(metrics, dict) and _has_value(metrics):
        return True

    reports = payload.get("reports")
    if isinstance(reports, (list, tuple)):
        return any(
            isinstance(report, dict) and _has_value(report.get("data"))
            for report in reports
        )

    data = payload.get("data")
    return isinstance(data, dict) and _has_value(data)


def _financial_period_count(payload: dict[str, Any]) -> int:
    for key in ("periods", "reports", "data"):
        value = payload.get(key)
        if isinstance(value, (dict, list, tuple)):
            return len(value)
    return 0


def _annotate_financial_availability(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    if _has_financial_values(result):
        result["status"] = "available"
        result["scoreable"] = True
        result.pop("reason", None)
        result.pop("evidence", None)
        return result

    result["status"] = "unavailable"
    result["scoreable"] = False
    result["reason"] = "financial_metrics_unavailable"
    result["evidence"] = {
        "source": result.get("source"),
        "statement": result.get("statement"),
        "freq": result.get("freq"),
        "period_count": _financial_period_count(result),
    }
    return result


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
        raise ValueError("Financial statements are not available for cryptocurrencies")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    normalized_market = normalize_equity_market(market)

    try:
        if normalized_market == "kr":
            payload = await _fetch_financials_naver(symbol, statement, freq)
            return _annotate_financial_availability(payload)
        try:
            payload = await _fetch_financials_finnhub(symbol, statement, freq)
        except (ValueError, Exception):
            payload = await _fetch_financials_yfinance(symbol, statement, freq)
        return _annotate_financial_availability(payload)
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


def _parse_iso_date(value: str | None, *, field_name: str) -> datetime.date | None:
    if value is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be ISO format (e.g., '2024-01-15')"
        ) from exc


def _normalize_kr_calendar_symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    normalized = symbol.strip().upper()
    if len(normalized) == 7 and normalized.startswith("A") and normalized[1:].isdigit():
        normalized = normalized[1:]
    if normalized.isdigit() and len(normalized) < 6:
        normalized = normalized.zfill(6)
    return normalized


def _is_kr_earnings_calendar_symbol(symbol: str) -> bool:
    normalized = symbol.strip().upper()
    return (len(normalized) == 6 and normalized.isdigit()) or (
        len(normalized) == 7 and normalized.startswith("A") and normalized[1:].isdigit()
    )


def _normalize_calendar_date_window(
    from_date: str | None,
    to_date: str | None,
) -> tuple[datetime.date, datetime.date]:
    start = _parse_iso_date(from_date, field_name="from_date") or datetime.date.today()
    end = _parse_iso_date(to_date, field_name="to_date") or (
        start + datetime.timedelta(days=30)
    )
    if start > end:
        raise ValueError("from_date must be <= to_date")
    return start, end


def _resolve_earnings_calendar_market(
    symbol: str | None,
    market: str | None,
) -> str:
    if symbol and _is_crypto_market(symbol):
        raise ValueError("Earnings calendar is not available for cryptocurrencies")
    if market is not None:
        normalized = normalize_market_with_crypto(market)
        if normalized == "crypto":
            raise ValueError("Earnings calendar is not available for cryptocurrencies")
        return normalized
    if symbol and _is_kr_earnings_calendar_symbol(symbol):
        return "kr"
    return "us"


def _number_or_none(value: Decimal | int | float | None) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    return value


def _metric_value(event: Any, metric_name: str, field_name: str) -> int | float | None:
    for value in event.values:
        if value.metric_name == metric_name:
            return _number_or_none(getattr(value, field_name))
    return None


async def _fetch_earnings_calendar_market_events_kr(
    symbol: str | None,
    from_date: str | None,
    to_date: str | None,
) -> dict[str, Any]:
    normalized_symbol = _normalize_kr_calendar_symbol(symbol)
    if normalized_symbol and not (
        len(normalized_symbol) == 6 and normalized_symbol.isdigit()
    ):
        raise ValueError("KR earnings calendar requires a Korean equity code")

    start, end = _normalize_calendar_date_window(from_date, to_date)

    async with AsyncSessionLocal() as db:
        svc = MarketEventsQueryService(db)
        response = await svc.list_for_range(
            start,
            end,
            category="earnings",
            market="kr",
            symbol=normalized_symbol,
        )

    earnings: list[dict[str, Any]] = []
    for event in response.events:
        earnings.append(
            {
                "symbol": event.symbol,
                "company_name": event.company_name,
                "date": event.event_date.isoformat(),
                "hour": event.time_hint or "unknown",
                "time_hint": event.time_hint or "unknown",
                "eps_estimate": _metric_value(event, "eps", "forecast"),
                "eps_actual": _metric_value(event, "eps", "actual"),
                "revenue_estimate": _metric_value(event, "revenue", "forecast"),
                "revenue_actual": _metric_value(event, "revenue", "actual"),
                "quarter": event.fiscal_quarter,
                "year": event.fiscal_year,
                "status": event.status,
                "source": event.source,
                "source_event_id": event.source_event_id,
                "source_url": event.source_url,
                "title": event.title,
            }
        )

    return {
        "symbol": normalized_symbol,
        "instrument_type": "equity_kr",
        "market": "kr",
        "source": "market_events",
        "sources": sorted({item["source"] for item in earnings}),
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "count": len(earnings),
        "earnings": earnings,
        "warning": (
            "KR earnings calendar is backed by market_events rows only "
            "(WiseFn scheduled earnings and DART filings classified as earnings). "
            "Shareholder meetings, ex-dividend dates, IR, and conferences are not "
            "covered by this tool yet."
        ),
    }


async def handle_get_earnings_calendar(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip() if symbol else None
    normalized_market = _resolve_earnings_calendar_market(symbol, market)

    if normalized_market == "kr":
        return await _fetch_earnings_calendar_market_events_kr(
            symbol,
            from_date,
            to_date,
        )

    if symbol and _is_kr_earnings_calendar_symbol(symbol):
        raise ValueError("Use market='kr' for Korean equities")

    start, end = _normalize_calendar_date_window(from_date, to_date)

    try:
        return await _fetch_earnings_calendar_finnhub(
            symbol,
            start.isoformat(),
            end.isoformat(),
        )
    except Exception as exc:
        return _error_payload(
            source="finnhub",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_us",
        )
