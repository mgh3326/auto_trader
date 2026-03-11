"""Naver/YFinance provider helpers for fundamentals and analysis tools."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from dataclasses import dataclass
from typing import Any

try:
    import finnhub
except ImportError:
    finnhub = None

import httpx
import pandas as pd
import yfinance as yf

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.mcp_server.tooling.shared import normalize_value as _normalize_value
from app.monitoring import build_yfinance_tracing_session
from app.services import naver_finance
from app.services.analyst_normalizer import (
    normalize_rating_label,
    rating_to_bucket,
)

# ---------------------------------------------------------------------------
# YFinance Snapshot (internal dedupe helper)
# ---------------------------------------------------------------------------


@dataclass
class _YFinanceSnapshot:
    """Internal container for yfinance data to avoid duplicate ticker.info calls."""

    info: dict[str, Any] | None = None
    analyst_price_targets: dict[str, Any] | None = None
    recommendations: Any = None  # DataFrame or None
    upgrades_downgrades: Any = None  # DataFrame or None


_SCREEN_ENRICHMENT_DEFAULTS: dict[str, Any] = {
    "sector": None,
    "analyst_buy": 0,
    "analyst_hold": 0,
    "analyst_sell": 0,
    "avg_target": None,
    "upside_pct": None,
}

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local Parse/Normalize Helpers (kept here to avoid circular imports)
# ---------------------------------------------------------------------------


def _parse_naver_num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_naver_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _coerce_optional_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if value != value:
            return None
        return value
    return None


def _build_screen_enrichment_payload(
    *,
    sector: Any,
    consensus: Any,
) -> dict[str, Any]:
    normalized_sector = str(sector).strip() if sector is not None else ""
    consensus_map = consensus if isinstance(consensus, dict) else {}
    payload = dict(_SCREEN_ENRICHMENT_DEFAULTS)
    payload["sector"] = normalized_sector or None
    payload["analyst_buy"] = _parse_naver_int(consensus_map.get("buy_count")) or 0
    payload["analyst_hold"] = _parse_naver_int(consensus_map.get("hold_count")) or 0
    payload["analyst_sell"] = _parse_naver_int(consensus_map.get("sell_count")) or 0
    payload["avg_target"] = _coerce_optional_number(
        consensus_map.get("avg_target_price")
    )
    payload["upside_pct"] = _coerce_optional_number(consensus_map.get("upside_pct"))
    return payload


async def _fetch_screen_enrichment_payload(
    *,
    symbol: str,
    profile_request: Any,
    opinions_request: Any,
    profile_provider: str,
    opinions_provider: str,
) -> dict[str, Any]:
    profile_result, opinions_result = await asyncio.gather(
        profile_request,
        opinions_request,
        return_exceptions=True,
    )

    profile_error = profile_result if isinstance(profile_result, Exception) else None
    opinions_error = opinions_result if isinstance(opinions_result, Exception) else None

    if profile_error is not None and opinions_error is not None:
        raise profile_error from opinions_error

    if profile_error is not None:
        logger.warning(
            "Screen enrichment profile provider failed for %s (%s): %s: %s",
            symbol,
            profile_provider,
            type(profile_error).__name__,
            profile_error,
        )

    if opinions_error is not None:
        logger.warning(
            "Screen enrichment opinions provider failed for %s (%s): %s: %s",
            symbol,
            opinions_provider,
            type(opinions_error).__name__,
            opinions_error,
        )

    profile = profile_result if isinstance(profile_result, dict) else None
    opinions = opinions_result if isinstance(opinions_result, dict) else None
    return _build_screen_enrichment_payload(
        sector=(profile or {}).get("sector"),
        consensus=(opinions or {}).get("consensus"),
    )


def _normalize_yahoo_numeric(
    value: Any,
    *,
    zero_as_missing: bool = True,
) -> float | None:
    if isinstance(value, dict):
        value = value.get("raw", value.get("fmt"))
    value = _normalize_value(value)
    if value in (None, ""):
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if zero_as_missing and number <= 0:
        return None
    return number


def _normalize_yahoo_count(value: Any) -> int | None:
    number = _normalize_yahoo_numeric(value, zero_as_missing=False)
    if number is None or number < 0:
        return None
    return int(number)


def _select_current_recommendation_row(recommendations: Any) -> dict[str, Any] | None:
    if not isinstance(recommendations, pd.DataFrame) or recommendations.empty:
        return None
    current_rows = recommendations
    if "period" in recommendations.columns:
        period_rows = recommendations[recommendations["period"] == "0m"]
        if not period_rows.empty:
            current_rows = period_rows
    row = current_rows.iloc[0]
    if hasattr(row, "to_dict"):
        row_dict: dict[str, Any] = {}
        for key, value in row.to_dict().items():
            row_dict[str(key)] = value
        return row_dict
    return None


def _build_yahoo_count_consensus(recommendations: Any) -> dict[str, Any] | None:
    current_row = _select_current_recommendation_row(recommendations)
    if current_row is None:
        return None

    strong_buy = _normalize_yahoo_count(current_row.get("strongBuy"))
    buy = _normalize_yahoo_count(current_row.get("buy"))
    hold = _normalize_yahoo_count(current_row.get("hold"))
    sell = _normalize_yahoo_count(current_row.get("sell"))
    strong_sell = _normalize_yahoo_count(current_row.get("strongSell"))
    if (
        strong_buy is None
        or buy is None
        or hold is None
        or sell is None
        or strong_sell is None
    ):
        return None

    strong_buy_count = strong_buy
    buy_count = buy
    hold_count = hold
    sell_count = sell
    strong_sell_count = strong_sell

    total_count = (
        strong_buy_count + buy_count + hold_count + sell_count + strong_sell_count
    )
    if total_count <= 0:
        return None

    return {
        "buy_count": strong_buy_count + buy_count,
        "hold_count": hold_count,
        "sell_count": sell_count + strong_sell_count,
        "strong_buy_count": strong_buy_count,
        "total_count": total_count,
    }


def _build_yahoo_target_consensus(
    targets: dict[str, Any] | None,
    *,
    fallback_current_price: float | None,
) -> dict[str, Any] | None:
    if not isinstance(targets, dict):
        targets = {}

    avg_target_price = _normalize_yahoo_numeric(targets.get("mean"))
    median_target_price = _normalize_yahoo_numeric(targets.get("median"))
    min_target_price = _normalize_yahoo_numeric(targets.get("low"))
    max_target_price = _normalize_yahoo_numeric(targets.get("high"))
    current_price = _normalize_yahoo_numeric(targets.get("current"))
    if current_price is None:
        current_price = fallback_current_price

    if all(
        value is None
        for value in (
            avg_target_price,
            median_target_price,
            min_target_price,
            max_target_price,
            current_price,
        )
    ):
        return None

    upside_pct = None
    if avg_target_price is not None and current_price is not None and current_price > 0:
        upside_pct = round((avg_target_price - current_price) / current_price * 100, 2)

    return {
        "avg_target_price": avg_target_price,
        "median_target_price": median_target_price,
        "min_target_price": min_target_price,
        "max_target_price": max_target_price,
        "current_price": current_price,
        "upside_pct": upside_pct,
    }


def _empty_analyst_consensus(current_price: float | None) -> dict[str, Any]:
    return {
        "buy_count": None,
        "hold_count": None,
        "sell_count": None,
        "strong_buy_count": None,
        "total_count": None,
        "avg_target_price": None,
        "median_target_price": None,
        "min_target_price": None,
        "max_target_price": None,
        "upside_pct": None,
        "current_price": current_price,
    }


# ---------------------------------------------------------------------------
# Finnhub Helpers
# ---------------------------------------------------------------------------


def _get_finnhub_client() -> Any:
    if finnhub is None:
        raise ImportError("finnhub-python is required to use Finnhub providers")
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


async def _fetch_financials_yfinance(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    session = build_yfinance_tracing_session()
    ticker = yf.Ticker(symbol, session=session)

    def fetch_sync() -> dict[str, Any]:
        statement_map = {
            "income": "income_stmt",
            "balance": "balance_sheet",
            "cashflow": "cashflow",
        }
        yf_stmt_name = statement_map.get(statement)
        if not yf_stmt_name:
            raise ValueError(
                f"Invalid statement type '{statement}'. Use: income, balance, cashflow"
            )

        freq_attr = f"quarterly_{yf_stmt_name}" if freq == "quarterly" else yf_stmt_name

        if not hasattr(ticker, freq_attr):
            try:
                df = getattr(ticker, yf_stmt_name)
                if df is None or df.empty:
                    raise ValueError(f"No {statement} data available for '{symbol}'")
            except Exception as e:
                raise ValueError(f"Failed to fetch {statement} data: {e}")

        df = getattr(ticker, freq_attr)
        if df is None or df.empty:
            raise ValueError(f"No {statement} data available for '{symbol}'")

        financials = {}
        for col in df.columns:
            col_key = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)
            period_data = {}
            for row_label, val in df[col].items():
                if pd.notna(val):
                    period_data[str(row_label)] = _normalize_value(val)
            if period_data:
                financials[col_key] = period_data

        return financials

    financials = await loop.run_in_executor(None, fetch_sync)

    return {
        "symbol": symbol.upper(),
        "instrument_type": "equity_us",
        "source": "yfinance",
        "statement": statement,
        "freq": freq,
        "data": financials,
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


# ---------------------------------------------------------------------------
# Naver / YFinance Helpers
# ---------------------------------------------------------------------------


async def _fetch_news_naver(symbol: str, limit: int) -> dict[str, Any]:
    news_items = await naver_finance.fetch_news(symbol, limit=limit)
    return {
        "symbol": symbol,
        "market": "kr",
        "source": "naver",
        "count": len(news_items),
        "news": news_items,
    }


async def _fetch_analysis_snapshot_naver(
    symbol: str,
    news_limit: int,
    opinions_limit: int,
) -> dict[str, Any]:
    snapshot = await naver_finance._fetch_kr_snapshot(
        symbol,
        news_limit=news_limit,
        opinion_limit=opinions_limit,
    )
    result: dict[str, Any] = {}
    valuation = snapshot.get("valuation")
    if isinstance(valuation, dict):
        result["valuation"] = {
            "instrument_type": "equity_kr",
            "source": "naver",
            **valuation,
        }

    news_items = snapshot.get("news")
    if isinstance(news_items, list):
        result["news"] = {
            "symbol": symbol,
            "market": "kr",
            "source": "naver",
            "count": len(news_items),
            "news": news_items,
        }

    opinions = snapshot.get("opinions")
    if isinstance(opinions, dict):
        result["opinions"] = {
            "instrument_type": "equity_kr",
            "source": "naver",
            **opinions,
        }

    return result


async def _fetch_company_profile_naver(symbol: str) -> dict[str, Any]:
    profile = await naver_finance.fetch_company_profile(symbol)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **profile,
    }


async def _fetch_financials_naver(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    financials = await naver_finance.fetch_financials(symbol, statement, freq)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **financials,
    }


async def _fetch_investor_trends_naver(symbol: str, days: int) -> dict[str, Any]:
    trends = await naver_finance.fetch_investor_trends(symbol, days=days)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **trends,
    }


async def _fetch_investment_opinions_naver(symbol: str, limit: int) -> dict[str, Any]:
    opinions = await naver_finance.fetch_investment_opinions(symbol, limit=limit)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **opinions,
    }


async def _fetch_investment_opinions_yfinance(
    symbol: str,
    limit: int,
    snapshot: _YFinanceSnapshot | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    if session is None:
        session = build_yfinance_tracing_session()
    ticker = yf.Ticker(symbol, session=session)

    def _collect() -> tuple[dict[str, Any] | None, Any, Any, dict[str, Any] | None]:
        targets = None
        try:
            targets = ticker.analyst_price_targets
        except Exception:
            pass

        recommendations = None
        try:
            recommendations = ticker.recommendations
        except Exception:
            pass

        ud = None
        try:
            ud = ticker.upgrades_downgrades
        except Exception:
            pass

        info = None
        try:
            info = ticker.info
        except Exception:
            pass
        return targets, recommendations, ud, info

    # Use pre-fetched snapshot if available
    if snapshot is not None:
        targets = snapshot.analyst_price_targets
        trend = snapshot.recommendations
        ud = snapshot.upgrades_downgrades
        info = snapshot.info
    else:
        targets, trend, ud, info = await loop.run_in_executor(None, _collect)

    current_price = _normalize_yahoo_numeric((info or {}).get("currentPrice"))
    opinions: list[dict[str, Any]] = []
    if ud is not None and not ud.empty:
        df = ud.head(limit).reset_index()
        for _, row in df.iterrows():
            raw_rating = row.get("ToGrade")
            rating_label = normalize_rating_label(raw_rating)
            rec: dict[str, Any] = {
                "firm": row.get("Firm"),
                "rating": rating_label,
                "rating_bucket": rating_to_bucket(rating_label),
                "date": (
                    row["GradeDate"].strftime("%Y-%m-%d")
                    if hasattr(row.get("GradeDate", None), "strftime")
                    else str(row.get("GradeDate", ""))[:10]
                ),
            }
            target_price = _normalize_yahoo_numeric(row.get("currentPriceTarget"))
            if target_price is not None:
                rec["target_price"] = target_price
            opinions.append(rec)

    target_consensus = _build_yahoo_target_consensus(
        targets,
        fallback_current_price=current_price,
    )
    usable_target_available = False
    if isinstance(targets, dict):
        usable_target_available = any(
            _normalize_yahoo_numeric(targets.get(key)) is not None
            for key in ("mean", "median", "low", "high", "current")
        )
    consensus = _empty_analyst_consensus(
        current_price=(target_consensus or {}).get("current_price", current_price)
    )

    count_consensus = _build_yahoo_count_consensus(trend)
    if count_consensus is not None:
        consensus.update(count_consensus)
    if target_consensus is not None:
        consensus.update(target_consensus)

    result = {
        "instrument_type": "equity_us",
        "source": "yfinance",
        "symbol": symbol.upper(),
        "count": len(opinions),
        "opinions": opinions,
        "consensus": consensus,
    }
    if count_consensus is None and not usable_target_available:
        result["warning"] = (
            f"Yahoo analyst consensus data unavailable for {symbol.upper()}."
        )
    return result


async def _fetch_screen_enrichment_kr(symbol: str) -> dict[str, Any]:
    return await _fetch_screen_enrichment_payload(
        symbol=symbol,
        profile_request=_fetch_company_profile_finnhub(symbol),
        opinions_request=_fetch_investment_opinions_naver(symbol, 10),
        profile_provider="finnhub",
        opinions_provider="naver",
    )


async def _fetch_screen_enrichment_us(symbol: str) -> dict[str, Any]:
    return await _fetch_screen_enrichment_payload(
        symbol=symbol,
        profile_request=_fetch_company_profile_finnhub(symbol),
        opinions_request=_fetch_investment_opinions_yfinance(symbol, 10),
        profile_provider="finnhub",
        opinions_provider="yfinance",
    )


async def _fetch_valuation_naver(symbol: str) -> dict[str, Any]:
    valuation = await naver_finance.fetch_valuation(symbol)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **valuation,
    }


async def _fetch_valuation_yfinance(
    symbol: str,
    snapshot: _YFinanceSnapshot | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    if session is None:
        session = build_yfinance_tracing_session()
    ticker = yf.Ticker(symbol, session=session)
    if snapshot is not None and snapshot.info is not None:
        info = snapshot.info
    else:
        info: dict[str, Any] = await loop.run_in_executor(None, lambda: ticker.info)

    current_price = info.get("currentPrice")
    high_52w = info.get("fiftyTwoWeekHigh")
    low_52w = info.get("fiftyTwoWeekLow")

    current_position_52w = None
    if current_price is not None and high_52w is not None and low_52w is not None:
        if high_52w > low_52w:
            current_position_52w = round(
                (current_price - low_52w) / (high_52w - low_52w), 2
            )

    roe_raw = info.get("returnOnEquity")
    roe = round(roe_raw * 100, 2) if roe_raw is not None else None

    return {
        "instrument_type": "equity_us",
        "source": "yfinance",
        "symbol": symbol.upper(),
        "name": info.get("shortName") or info.get("longName"),
        "current_price": current_price,
        "per": info.get("trailingPE"),
        "pbr": info.get("priceToBook"),
        "roe": roe,
        "dividend_yield": info.get("dividendYield"),
        "high_52w": high_52w,
        "low_52w": low_52w,
        "current_position_52w": current_position_52w,
    }


async def _fetch_sector_peers_naver(
    symbol: str, limit: int, manual_peers: list[str] | None = None
) -> dict[str, Any]:
    data = await naver_finance.fetch_sector_peers(symbol, limit=limit)
    peers = data["peers"]

    target_per = data.get("per")
    target_pbr = data.get("pbr")

    all_pers = [
        v
        for v in [target_per] + [p.get("per") for p in peers]
        if v is not None and v > 0
    ]
    all_pbrs = [
        v
        for v in [target_pbr] + [p.get("pbr") for p in peers]
        if v is not None and v > 0
    ]

    avg_per = round(sum(all_pers) / len(all_pers), 2) if all_pers else None
    avg_pbr = round(sum(all_pbrs) / len(all_pbrs), 2) if all_pbrs else None

    target_per_rank = None
    if target_per is not None and target_per > 0 and all_pers:
        sorted_pers = sorted(all_pers)
        target_per_rank = f"{sorted_pers.index(target_per) + 1}/{len(sorted_pers)}"

    target_pbr_rank = None
    if target_pbr is not None and target_pbr > 0 and all_pbrs:
        sorted_pbrs = sorted(all_pbrs)
        target_pbr_rank = f"{sorted_pbrs.index(target_pbr) + 1}/{len(sorted_pbrs)}"

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        "symbol": symbol,
        "name": data.get("name"),
        "sector": data.get("sector"),
        "current_price": data.get("current_price"),
        "change_pct": data.get("change_pct"),
        "per": target_per,
        "pbr": target_pbr,
        "market_cap": data.get("market_cap"),
        "peers": peers,
        "comparison": {
            "avg_per": avg_per,
            "avg_pbr": avg_pbr,
            "target_per_rank": target_per_rank,
            "target_pbr_rank": target_pbr_rank,
        },
    }


async def _fetch_sector_peers_us(
    symbol: str, limit: int, manual_peers: list[str] | None = None
) -> dict[str, Any]:
    client = _get_finnhub_client()
    upper_symbol = symbol.upper()

    def get_base_ticker(ticker: str) -> str:
        if "." in ticker:
            return ticker.split(".")[0]
        return ticker

    if manual_peers:
        peer_tickers = [t.upper() for t in manual_peers if t.upper() != upper_symbol]
        # Dedupe by base ticker BEFORE network call
        target_base = get_base_ticker(upper_symbol)
        seen_bases = {target_base}
        deduped_peer_tickers = []
        for ticker in peer_tickers:
            peer_base = get_base_ticker(ticker)
            if peer_base not in seen_bases:
                seen_bases.add(peer_base)
                deduped_peer_tickers.append(ticker)
        peer_tickers = deduped_peer_tickers[:limit]
    else:
        peer_tickers: list[str] = await asyncio.to_thread(
            client.company_peers, upper_symbol
        )
        peer_tickers = [t for t in peer_tickers if t.upper() != upper_symbol]
        # Dedupe by base ticker BEFORE network call
        target_base = get_base_ticker(upper_symbol)
        seen_bases = {target_base}
        deduped_peer_tickers = []
        for ticker in peer_tickers:
            peer_base = get_base_ticker(ticker)
            if peer_base not in seen_bases:
                seen_bases.add(peer_base)
                deduped_peer_tickers.append(ticker)
        peer_tickers = deduped_peer_tickers[: limit + 5]

    all_tickers = [upper_symbol] + peer_tickers

    async def _fetch_yf_info(ticker: str) -> tuple[str, dict[str, Any] | None]:
        try:
            session = build_yfinance_tracing_session()

            def _fetch_info(symbol: str = ticker, yf_session=session) -> dict[str, Any]:
                return yf.Ticker(symbol, session=yf_session).info

            info: dict[str, Any] = await asyncio.to_thread(_fetch_info)
            return (ticker, info)
        except Exception:
            return (ticker, None)

    results = await asyncio.gather(*[_fetch_yf_info(t) for t in all_tickers])
    info_map = {t: info for t, info in results if info}

    target_info = info_map.get(upper_symbol, {})
    target_name = target_info.get("shortName") or target_info.get("longName")
    target_sector = target_info.get("sector")
    target_industry = target_info.get("industry")
    target_price = target_info.get("currentPrice")
    target_prev = target_info.get("previousClose") or target_info.get(
        "regularMarketPreviousClose"
    )
    target_change_pct = (
        round((target_price - target_prev) / target_prev * 100, 2)
        if target_price and target_prev and target_prev > 0
        else None
    )
    target_per = target_info.get("trailingPE")
    target_pbr = target_info.get("priceToBook")
    target_mcap = target_info.get("marketCap")

    target_base = get_base_ticker(upper_symbol)
    # Dedupe already applied before network call, use peer_tickers directly
    peers: list[dict[str, Any]] = []
    for ticker in peer_tickers:
        info = info_map.get(ticker)
        if info is None:
            continue
        price = info.get("currentPrice")
        prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
        change_pct = (
            round((price - prev) / prev * 100, 2)
            if price and prev and prev > 0
            else None
        )
        peers.append(
            {
                "symbol": ticker,
                "name": info.get("shortName") or info.get("longName"),
                "current_price": price,
                "change_pct": change_pct,
                "per": info.get("trailingPE"),
                "pbr": info.get("priceToBook"),
                "market_cap": info.get("marketCap"),
                "same_industry": (
                    info.get("industry") == target_industry
                    if target_industry and info.get("industry")
                    else None
                ),
            }
        )

    peers.sort(
        key=lambda x: (x.get("same_industry") is True, x.get("market_cap") or 0),
        reverse=True,
    )
    peers = peers[:limit]

    all_pers = [
        v
        for v in [target_per] + [p.get("per") for p in peers]
        if v is not None and v > 0
    ]
    all_pbrs = [
        v
        for v in [target_pbr] + [p.get("pbr") for p in peers]
        if v is not None and v > 0
    ]

    avg_per = round(sum(all_pers) / len(all_pers), 2) if all_pers else None
    avg_pbr = round(sum(all_pbrs) / len(all_pbrs), 2) if all_pbrs else None

    target_per_rank = None
    if target_per is not None and target_per > 0 and all_pers:
        sorted_pers = sorted(all_pers)
        target_per_rank = f"{sorted_pers.index(target_per) + 1}/{len(sorted_pers)}"

    target_pbr_rank = None
    if target_pbr is not None and target_pbr > 0 and all_pbrs:
        sorted_pbrs = sorted(all_pbrs)
        target_pbr_rank = f"{sorted_pbrs.index(target_pbr) + 1}/{len(sorted_pbrs)}"

    same_industry_count = sum(1 for p in peers if p.get("same_industry"))

    return {
        "instrument_type": "equity_us",
        "source": "finnhub+yfinance",
        "symbol": upper_symbol,
        "name": target_name,
        "sector": target_sector,
        "industry": target_industry,
        "current_price": target_price,
        "change_pct": target_change_pct,
        "per": target_per,
        "pbr": target_pbr,
        "market_cap": target_mcap,
        "peers": peers,
        "same_industry_count": same_industry_count,
        "comparison": {
            "avg_per": avg_per,
            "avg_pbr": avg_pbr,
            "target_per_rank": target_per_rank,
            "target_pbr_rank": target_pbr_rank,
        },
    }


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Kimchi Premium Helpers
# ---------------------------------------------------------------------------

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"


async def _fetch_exchange_rate_usd_krw() -> float:
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(EXCHANGE_RATE_URL)
        r.raise_for_status()
        data = r.json()
        return float(data["rates"]["KRW"])


async def _fetch_binance_prices(symbols: list[str]) -> dict[str, float]:
    pairs = [f"{s}USDT" for s in symbols]
    async with httpx.AsyncClient(timeout=10) as cli:
        symbols_json = json.dumps(pairs, separators=(",", ":"))
        r = await cli.get(BINANCE_TICKER_URL, params={"symbols": symbols_json})
        r.raise_for_status()
        data = r.json()

    result: dict[str, float] = {}
    for item in data:
        pair: str = item["symbol"]
        if pair.endswith("USDT"):
            sym = pair[: -len("USDT")]
            result[sym] = float(item["price"])
    return result


async def _fetch_kimchi_premium(symbols: list[str]) -> dict[str, Any]:
    upbit_markets = [f"KRW-{s}" for s in symbols]

    upbit_prices, binance_prices, exchange_rate = await asyncio.gather(
        upbit_service.fetch_multiple_current_prices(upbit_markets),
        _fetch_binance_prices(symbols),
        _fetch_exchange_rate_usd_krw(),
    )

    data: list[dict[str, Any]] = []
    for sym in symbols:
        upbit_key = f"KRW-{sym}"
        upbit_krw = upbit_prices.get(upbit_key)
        binance_usdt = binance_prices.get(sym)

        if upbit_krw is None or binance_usdt is None:
            continue

        binance_krw = binance_usdt * exchange_rate
        premium_pct = round((upbit_krw - binance_krw) / binance_krw * 100, 2)

        data.append(
            {
                "symbol": sym,
                "upbit_krw": upbit_krw,
                "binance_usdt": binance_usdt,
                "binance_krw": round(binance_krw, 0),
                "premium_pct": premium_pct,
            }
        )

    now = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")

    return {
        "instrument_type": "crypto",
        "source": "upbit+binance",
        "timestamp": now,
        "exchange_rate": exchange_rate,
        "count": len(data),
        "data": data,
    }


__all__ = [
    "_fetch_company_profile_naver",
    "_fetch_financials_naver",
    "_fetch_financials_yfinance",
    "_fetch_investment_opinions_naver",
    "_fetch_investment_opinions_yfinance",
    "_fetch_screen_enrichment_kr",
    "_fetch_screen_enrichment_us",
    "_fetch_investor_trends_naver",
    "_fetch_kimchi_premium",
    "_fetch_news_naver",
    "_fetch_sector_peers_naver",
    "_fetch_sector_peers_us",
    "_fetch_valuation_naver",
    "_fetch_valuation_yfinance",
    "_parse_naver_int",
    "_parse_naver_num",
    "_YFinanceSnapshot",
]
