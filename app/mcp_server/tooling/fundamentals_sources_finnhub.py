"""Finnhub provider helpers for fundamentals domain."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

try:
    import finnhub
except ImportError:
    finnhub = None

from app.core.config import settings


def _get_finnhub_client() -> finnhub.Client:
    api_key = settings.finnhub_api_key
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set")
    return finnhub.Client(api_key=api_key)


async def _fetch_news_finnhub(symbol: str, market: str, limit: int) -> dict[str, Any]:
    client = _get_finnhub_client()
    to_date = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=7)

    def fetch_sync() -> list[dict[str, Any]]:
        if market == "crypto":
            news = client.general_news("crypto", min_id=0)
        else:
            news = client.company_news(
                symbol.upper(),
                _from=from_date.strftime("%Y-%m-%d"),
                to=to_date.strftime("%Y-%m-%d"),
            )
        return news[:limit] if news else []

    news_items = await asyncio.to_thread(fetch_sync)

    result_items = []
    for item in news_items:
        result_items.append(
            {
                "title": item.get("headline", ""),
                "source": item.get("source", ""),
                "datetime": datetime.datetime.fromtimestamp(
                    item.get("datetime", 0)
                ).isoformat()
                if item.get("datetime")
                else None,
                "url": item.get("url", ""),
                "summary": item.get("summary", ""),
                "sentiment": item.get("sentiment"),
                "related": item.get("related", ""),
            }
        )

    return {
        "symbol": symbol,
        "market": market,
        "source": "finnhub",
        "count": len(result_items),
        "news": result_items,
    }


async def _fetch_company_profile_finnhub(symbol: str) -> dict[str, Any]:
    client = _get_finnhub_client()

    def fetch_sync() -> dict[str, Any]:
        return client.company_profile2(symbol=symbol.upper())

    profile = await asyncio.to_thread(fetch_sync)
    if not profile:
        raise ValueError(f"Company profile not found for symbol '{symbol}'")

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "name": profile.get("name", ""),
        "ticker": profile.get("ticker", ""),
        "country": profile.get("country", ""),
        "currency": profile.get("currency", ""),
        "exchange": profile.get("exchange", ""),
        "ipo_date": profile.get("ipo", ""),
        "market_cap": profile.get("marketCapitalization"),
        "shares_outstanding": profile.get("shareOutstanding"),
        "sector": profile.get("finnhubIndustry", ""),
        "website": profile.get("weburl", ""),
        "logo": profile.get("logo", ""),
        "phone": profile.get("phone", ""),
    }


async def _fetch_financials_finnhub(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    client = _get_finnhub_client()

    statement_map = {
        "income": "ic",
        "balance": "bs",
        "cashflow": "cf",
    }
    finnhub_statement = statement_map.get(statement)
    if not finnhub_statement:
        raise ValueError(
            f"Invalid statement type '{statement}'. Use: income, balance, cashflow"
        )

    def fetch_sync() -> dict[str, Any]:
        return client.financials_reported(symbol=symbol.upper(), freq=freq)

    result = await asyncio.to_thread(fetch_sync)

    if not result or not result.get("data"):
        raise ValueError(f"Financial data not found for symbol '{symbol}'")

    reports = []
    for report in result.get("data", [])[:4]:
        report_data = report.get("report", {})
        statement_data = report_data.get(finnhub_statement, [])

        financials = {}
        for item in statement_data:
            label = item.get("label", item.get("concept", ""))
            value = item.get("value")
            if label and value is not None:
                financials[label] = value

        reports.append(
            {
                "year": report.get("year"),
                "quarter": report.get("quarter"),
                "filed_date": report.get("filedDate"),
                "period_start": report.get("startDate"),
                "period_end": report.get("endDate"),
                "data": financials,
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "statement": statement,
        "freq": freq,
        "reports": reports,
    }


async def _fetch_insider_transactions_finnhub(
    symbol: str, limit: int
) -> dict[str, Any]:
    client = _get_finnhub_client()

    def fetch_sync() -> dict[str, Any]:
        return client.stock_insider_transactions(symbol=symbol.upper())

    result = await asyncio.to_thread(fetch_sync)

    if not result or not result.get("data"):
        return {
            "symbol": symbol,
            "instrument_type": "equity_us",
            "source": "finnhub",
            "count": 0,
            "transactions": [],
        }

    transactions = []
    for txn in result.get("data", [])[:limit]:
        txn_code = txn.get("transactionCode", "")
        txn_type_map = {
            "P": "Purchase",
            "S": "Sale",
            "A": "Grant/Award",
            "D": "Sale to Issuer",
            "F": "Tax Payment",
            "M": "Option Exercise",
            "G": "Gift",
            "C": "Conversion",
            "J": "Other",
        }
        transactions.append(
            {
                "name": txn.get("name", ""),
                "transaction_type": txn_type_map.get(txn_code, txn_code),
                "transaction_code": txn_code,
                "shares": txn.get("share"),
                "change": txn.get("change"),
                "price": txn.get("transactionPrice"),
                "date": txn.get("transactionDate"),
                "filing_date": txn.get("filingDate"),
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "count": len(transactions),
        "transactions": transactions,
    }


async def _fetch_earnings_calendar_finnhub(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    client = _get_finnhub_client()

    if not from_date:
        from_date = datetime.date.today().isoformat()
    if not to_date:
        to_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

    def fetch_sync() -> dict[str, Any]:
        return client.earnings_calendar(
            symbol=symbol.upper() if symbol else "",
            _from=from_date,
            to=to_date,
        )

    result = await asyncio.to_thread(fetch_sync)

    if not result or not result.get("earningsCalendar"):
        return {
            "symbol": symbol,
            "instrument_type": "equity_us",
            "source": "finnhub",
            "from_date": from_date,
            "to_date": to_date,
            "count": 0,
            "earnings": [],
        }

    earnings = []
    for item in result.get("earningsCalendar", []):
        earnings.append(
            {
                "symbol": item.get("symbol", ""),
                "date": item.get("date"),
                "hour": item.get("hour", ""),
                "eps_estimate": item.get("epsEstimate"),
                "eps_actual": item.get("epsActual"),
                "revenue_estimate": item.get("revenueEstimate"),
                "revenue_actual": item.get("revenueActual"),
                "quarter": item.get("quarter"),
                "year": item.get("year"),
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "from_date": from_date,
        "to_date": to_date,
        "count": len(earnings),
        "earnings": earnings,
    }


__all__ = [
    "_fetch_company_profile_finnhub",
    "_fetch_earnings_calendar_finnhub",
    "_fetch_financials_finnhub",
    "_fetch_insider_transactions_finnhub",
    "_fetch_news_finnhub",
    "_get_finnhub_client",
]

