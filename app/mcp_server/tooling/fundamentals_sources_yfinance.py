"""YFinance provider helpers for fundamentals and analysis tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pandas as pd
import yfinance as yf

from app.mcp_server.tooling.fundamentals_sources_common import (
    _fetch_screen_enrichment_payload,
)
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_company_profile_finnhub,
    _get_finnhub_client,
)
from app.mcp_server.tooling.shared import normalize_value as _normalize_value
from app.monitoring import (
    build_yfinance_tracing_session,
    close_yfinance_session,
    yfinance_tracing_session,
)
from app.services.analyst_normalizer import (
    normalize_rating_label,
    rating_to_bucket,
)


@dataclass
class _YFinanceSnapshot:
    """Internal container for yfinance data to avoid duplicate ticker.info calls."""

    info: dict[str, Any] | None = None
    analyst_price_targets: dict[str, Any] | None = None
    recommendations: Any = None  # DataFrame or None
    upgrades_downgrades: Any = None  # DataFrame or None


# ---------------------------------------------------------------------------
# YFinance normalize helpers
# ---------------------------------------------------------------------------


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
# YFinance fetch functions
# ---------------------------------------------------------------------------


async def _fetch_financials_yfinance(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    def fetch_sync(ticker: yf.Ticker) -> dict[str, Any]:
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

    with yfinance_tracing_session() as session:
        ticker = yf.Ticker(symbol, session=session)
        financials = await asyncio.to_thread(fetch_sync, ticker)

    return {
        "symbol": symbol.upper(),
        "instrument_type": "equity_us",
        "source": "yfinance",
        "statement": statement,
        "freq": freq,
        "data": financials,
    }


async def _fetch_investment_opinions_yfinance(
    symbol: str,
    limit: int,
    snapshot: _YFinanceSnapshot | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    owns_session = session is None and snapshot is None
    if snapshot is None:
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
        try:
            targets, trend, ud, info = await asyncio.to_thread(_collect)
        finally:
            if owns_session and session is not None:
                close_yfinance_session(session)

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


async def _fetch_investment_opinions_yfinance_screen(
    symbol: str,
    *,
    current_price: float | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    owns_session = session is None
    if session is None:
        session = build_yfinance_tracing_session()
    ticker = yf.Ticker(symbol, session=session)

    def _collect() -> tuple[dict[str, Any] | None, Any]:
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

        return targets, recommendations

    try:
        targets, trend = await asyncio.to_thread(_collect)
    finally:
        if owns_session:
            close_yfinance_session(session)

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
        "count": 0,
        "opinions": [],
        "consensus": consensus,
    }
    if count_consensus is None and not usable_target_available:
        result["warning"] = (
            f"Yahoo analyst consensus data unavailable for {symbol.upper()}."
        )
    return result


async def _fetch_valuation_yfinance(
    symbol: str,
    snapshot: _YFinanceSnapshot | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    owns_session = session is None and not (
        snapshot is not None and snapshot.info is not None
    )
    if snapshot is not None and snapshot.info is not None:
        info = snapshot.info
    else:
        if session is None:
            session = build_yfinance_tracing_session()
        ticker = yf.Ticker(symbol, session=session)
        try:
            info: dict[str, Any] = await asyncio.to_thread(lambda: ticker.info)
        finally:
            if owns_session:
                close_yfinance_session(session)

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


# ---------------------------------------------------------------------------
# US market cross-source functions
# ---------------------------------------------------------------------------


async def _fetch_screen_enrichment_us(
    symbol: str,
    *,
    current_price: float | None = None,
    session: Any | None = None,
    include_opinion_history: bool = True,
) -> dict[str, Any]:
    opinions_request: Any
    opinions_provider: str
    if include_opinion_history:
        if session is None:
            opinions_request = _fetch_investment_opinions_yfinance(symbol, 10)
        else:
            opinions_request = _fetch_investment_opinions_yfinance(
                symbol,
                10,
                session=session,
            )
        opinions_provider = "yfinance"
    else:
        if current_price is None and session is None:
            opinions_request = _fetch_investment_opinions_yfinance_screen(symbol)
        elif session is None:
            opinions_request = _fetch_investment_opinions_yfinance_screen(
                symbol,
                current_price=current_price,
            )
        else:
            opinions_request = _fetch_investment_opinions_yfinance_screen(
                symbol,
                current_price=current_price,
                session=session,
            )
        opinions_provider = "yfinance_screen"

    return await _fetch_screen_enrichment_payload(
        symbol=symbol,
        profile_request=_fetch_company_profile_finnhub(symbol),
        opinions_request=opinions_request,
        profile_provider="finnhub",
        opinions_provider=opinions_provider,
    )


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
            with yfinance_tracing_session() as session:

                def _fetch_info(
                    symbol: str = ticker, yf_session=session
                ) -> dict[str, Any]:
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


__all__ = [
    "_build_yahoo_count_consensus",
    "_build_yahoo_target_consensus",
    "_empty_analyst_consensus",
    "_fetch_financials_yfinance",
    "_fetch_investment_opinions_yfinance",
    "_fetch_investment_opinions_yfinance_screen",
    "_fetch_screen_enrichment_us",
    "_fetch_sector_peers_us",
    "_fetch_valuation_yfinance",
    "_normalize_yahoo_count",
    "_normalize_yahoo_numeric",
    "_select_current_recommendation_row",
    "_YFinanceSnapshot",
]
