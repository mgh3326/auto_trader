"""Market index provider helpers for fundamentals domain."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

import httpx
import pandas as pd
import yfinance as yf

_INDEX_META: dict[str, dict[str, str]] = {
    "KOSPI": {"name": "코스피", "source": "naver", "naver_code": "KOSPI"},
    "KOSDAQ": {"name": "코스닥", "source": "naver", "naver_code": "KOSDAQ"},
    "SPX": {"name": "S&P 500", "source": "yfinance", "yf_ticker": "^GSPC"},
    "SP500": {"name": "S&P 500", "source": "yfinance", "yf_ticker": "^GSPC"},
    "NASDAQ": {"name": "NASDAQ Composite", "source": "yfinance", "yf_ticker": "^IXIC"},
    "DJI": {"name": "다우존스", "source": "yfinance", "yf_ticker": "^DJI"},
    "DOW": {"name": "다우존스", "source": "yfinance", "yf_ticker": "^DJI"},
}

_DEFAULT_INDICES = ["KOSPI", "KOSDAQ", "SPX", "NASDAQ"]

NAVER_INDEX_BASIC_URL = "https://m.stock.naver.com/api/index/{code}/basic"
NAVER_INDEX_PRICE_URL = "https://m.stock.naver.com/api/index/{code}/price"


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


async def _fetch_index_kr_current(naver_code: str, name: str) -> dict[str, Any]:
    basic_url = NAVER_INDEX_BASIC_URL.format(code=naver_code)
    price_url = NAVER_INDEX_PRICE_URL.format(code=naver_code)

    async with httpx.AsyncClient(timeout=10) as cli:
        basic_resp, price_resp = await asyncio.gather(
            cli.get(basic_url, headers={"User-Agent": "Mozilla/5.0"}),
            cli.get(
                price_url,
                params={"pageSize": 1, "page": 1},
                headers={"User-Agent": "Mozilla/5.0"},
            ),
        )
        basic_resp.raise_for_status()
        price_resp.raise_for_status()

        basic = basic_resp.json()
        price_list = price_resp.json()

    latest = price_list[0] if price_list else {}

    return {
        "symbol": naver_code,
        "name": name,
        "current": _parse_naver_num(basic.get("closePrice")),
        "change": _parse_naver_num(basic.get("compareToPreviousClosePrice")),
        "change_pct": _parse_naver_num(basic.get("fluctuationsRatio")),
        "open": _parse_naver_num(latest.get("openPrice")),
        "high": _parse_naver_num(latest.get("highPrice")),
        "low": _parse_naver_num(latest.get("lowPrice")),
        "volume": _parse_naver_int(latest.get("accumulatedTradingVolume")),
        "source": "naver",
    }


async def _fetch_index_kr_history(
    naver_code: str, count: int, period: str
) -> list[dict[str, Any]]:
    url = NAVER_INDEX_PRICE_URL.format(code=naver_code)
    period_map = {"day": "day", "week": "week", "month": "month"}
    timeframe = period_map.get(period, "day")

    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(
            url,
            params={"pageSize": count, "page": 1, "timeframe": timeframe},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()

    history: list[dict[str, Any]] = []
    for item in data:
        history.append(
            {
                "date": item.get("localTradedAt", ""),
                "close": _parse_naver_num(item.get("closePrice")),
                "open": _parse_naver_num(item.get("openPrice")),
                "high": _parse_naver_num(item.get("highPrice")),
                "low": _parse_naver_num(item.get("lowPrice")),
                "volume": _parse_naver_int(item.get("accumulatedTradingVolume")),
            }
        )
    return history


async def _fetch_index_us_current(
    yf_ticker: str, name: str, symbol: str
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    ticker_obj = yf.Ticker(yf_ticker)
    info = await loop.run_in_executor(None, lambda: ticker_obj.fast_info)

    current = getattr(info, "last_price", None)
    previous_close = getattr(info, "regular_market_previous_close", None)

    change: float | None = None
    change_pct: float | None = None
    if current is not None and previous_close is not None and previous_close != 0:
        change = round(current - previous_close, 2)
        change_pct = round((current - previous_close) / previous_close * 100, 2)

    return {
        "symbol": symbol,
        "name": name,
        "current": current,
        "change": change,
        "change_pct": change_pct,
        "open": getattr(info, "open", None),
        "high": getattr(info, "day_high", None),
        "low": getattr(info, "day_low", None),
        "volume": getattr(info, "last_volume", None),
        "source": "yfinance",
    }


async def _fetch_index_us_history(
    yf_ticker: str, count: int, period: str
) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    period_map = {"day": "1d", "week": "1wk", "month": "1mo"}
    interval = period_map.get(period, "1d")

    multiplier = {"day": 2, "week": 10, "month": 40}.get(period, 2)
    end = datetime.date.today() + datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=count * multiplier)

    def download() -> pd.DataFrame:
        raw_df = yf.download(
            yf_ticker,
            start=start,
            end=end,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )
        if raw_df is None or not isinstance(raw_df, pd.DataFrame):
            return pd.DataFrame()

        df = raw_df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        return df.reset_index(names="date")

    df = await loop.run_in_executor(None, download)
    if df.empty:
        return []

    df = df.tail(count).reset_index(drop=True)

    history: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        d = row.get("date")
        if isinstance(d, (datetime.date, datetime.datetime, pd.Timestamp)):
            date_str = d.strftime("%Y-%m-%d")
        else:
            date_str = str(d)[:10]
        history.append(
            {
                "date": date_str,
                "close": float(row["close"]) if pd.notna(row.get("close")) else None,
                "open": float(row["open"]) if pd.notna(row.get("open")) else None,
                "high": float(row["high"]) if pd.notna(row.get("high")) else None,
                "low": float(row["low"]) if pd.notna(row.get("low")) else None,
                "volume": int(row["volume"]) if pd.notna(row.get("volume")) else None,
            }
        )
    return history


__all__ = [
    "_DEFAULT_INDICES",
    "_INDEX_META",
    "_fetch_index_kr_current",
    "_fetch_index_kr_history",
    "_fetch_index_us_current",
    "_fetch_index_us_history",
]
