"""Market index provider helpers for fundamentals domain."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

import httpx
import pandas as pd
import yfinance as yf

from app.monitoring import yfinance_tracing_session
from app.services.external.btc_dominance import fetch_btc_dominance

_INDEX_META: dict[str, dict[str, str]] = {
    "KOSPI": {"name": "코스피", "source": "naver", "naver_code": "KOSPI"},
    "KOSDAQ": {"name": "코스닥", "source": "naver", "naver_code": "KOSDAQ"},
    "SPX": {"name": "S&P 500", "source": "yfinance", "yf_ticker": "^GSPC"},
    "SP500": {"name": "S&P 500", "source": "yfinance", "yf_ticker": "^GSPC"},
    "NASDAQ": {"name": "NASDAQ Composite", "source": "yfinance", "yf_ticker": "^IXIC"},
    "DJI": {"name": "다우존스", "source": "yfinance", "yf_ticker": "^DJI"},
    "DOW": {"name": "다우존스", "source": "yfinance", "yf_ticker": "^DJI"},
    "VIX": {"name": "CBOE 변동성지수(VIX)", "source": "yfinance", "yf_ticker": "^VIX"},
    "CRYPTO": {
        "name": "암호화폐 총 시가총액",
        "source": "coingecko",
        "cg_metric": "total_market_cap",
    },
    "BTC.D": {
        "name": "BTC 도미넌스",
        "source": "coingecko",
        "cg_metric": "btc_dominance",
    },
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


def _safe_fast_info_attr(info: Any, name: str) -> Any:
    """Read a yfinance ``fast_info`` attribute without propagating its internals.

    ROB-365 hotfix: ``getattr(info, name, default)`` only shields against
    ``AttributeError``. yfinance's ``FastInfo`` computes values lazily on access
    and can raise ``TypeError: 'NoneType' object is not subscriptable`` (and
    similar) when the underlying price frame is missing — that propagates out of
    ``getattr`` and crashes the caller. Treat any such failure as "unavailable".
    """
    if info is None:
        return None
    try:
        return getattr(info, name, None)
    except Exception:  # noqa: BLE001 — FastInfo internals can raise non-AttributeError
        return None


async def _index_current_from_history(yf_ticker: str) -> dict[str, Any] | None:
    """Latest-history-row fallback for a US index current quote (ROB-365 hotfix).

    Returns ``{current, previous_close, open, high, low, volume}`` from the most
    recent daily history row (``previous_close`` from the prior row when present),
    or ``None`` when history is empty/unavailable. Never raises.
    """
    try:
        rows = await _fetch_index_us_history(yf_ticker, 2, "day")
    except Exception:  # noqa: BLE001 — fallback must never raise
        return None
    if not rows:
        return None
    latest = rows[-1]
    current = latest.get("close")
    if current is None:
        return None
    return {
        "current": current,
        "previous_close": rows[-2].get("close") if len(rows) >= 2 else None,
        "open": latest.get("open"),
        "high": latest.get("high"),
        "low": latest.get("low"),
        "volume": latest.get("volume"),
    }


async def _fetch_index_us_current(
    yf_ticker: str, name: str, symbol: str
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    # fast_info acquisition itself can fail (network / yfinance internals); never
    # let it crash the current-quote path.
    info: Any = None
    try:
        with yfinance_tracing_session() as session:
            ticker_obj = yf.Ticker(yf_ticker, session=session)
            info = await loop.run_in_executor(None, lambda: ticker_obj.fast_info)
    except Exception:  # noqa: BLE001 — degrade to history fallback below
        info = None

    current = _safe_fast_info_attr(info, "last_price")
    previous_close = _safe_fast_info_attr(info, "regular_market_previous_close")
    open_ = _safe_fast_info_attr(info, "open")
    high = _safe_fast_info_attr(info, "day_high")
    low = _safe_fast_info_attr(info, "day_low")
    volume = _safe_fast_info_attr(info, "last_volume")
    source = "yfinance"

    # ROB-365 hotfix: when fast_info yields no current price (failed internally or
    # returned None), fall back to the latest daily history row.
    if current is None:
        fallback = await _index_current_from_history(yf_ticker)
        if fallback is not None:
            current = fallback["current"]
            if previous_close is None:
                previous_close = fallback["previous_close"]
            open_ = open_ if open_ is not None else fallback["open"]
            high = high if high is not None else fallback["high"]
            low = low if low is not None else fallback["low"]
            volume = volume if volume is not None else fallback["volume"]
            source = "yfinance_history_fallback"

    change: float | None = None
    change_pct: float | None = None
    if current is not None and previous_close is not None and previous_close != 0:
        change = round(current - previous_close, 2)
        change_pct = round((current - previous_close) / previous_close * 100, 2)

    result: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "current": current,
        "change": change,
        "change_pct": change_pct,
        "open": open_,
        "high": high,
        "low": low,
        "volume": volume,
        "source": source,
    }
    # Fail-closed: neither fast_info nor history yielded a price -> explicit
    # degraded result instead of raising.
    if current is None:
        result["unavailable"] = True
        result["degraded_reason"] = (
            "current quote unavailable: fast_info failed and no history fallback"
        )
    return result


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
            session=session,
        )
        if raw_df is None or not isinstance(raw_df, pd.DataFrame):
            return pd.DataFrame()

        df = raw_df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        return df.reset_index(names="date")

    with yfinance_tracing_session() as session:
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


async def _fetch_index_crypto_current(
    cg_metric: str, name: str, symbol: str
) -> dict[str, Any]:
    """Crypto market-regime "index" row from CoinGecko /global (cached).

    Row shape matches the KR/US index rows so the snapshot collector and
    MarketStage consume it unchanged. ``total_market_cap`` carries a usable
    24h change_pct (the regime driver); ``btc_dominance`` reports the dominance
    level only (CoinGecko /global has no dominance 24h change) → change_pct is
    None, which the collector intentionally drops and MarketStage skips rather
    than fabricating a flat 0.0%. Raises on an unreachable /global so the
    handler maps it to an error payload (never fabricate values).
    """
    data = await fetch_btc_dominance()
    if not data:
        raise RuntimeError("CoinGecko /global unavailable")

    if cg_metric == "total_market_cap":
        current = data.get("total_market_cap_usd")
        change_pct = data.get("total_market_cap_change_24h")
    elif cg_metric == "btc_dominance":
        current = data.get("btc_dominance")
        change_pct = None
    else:
        raise ValueError(f"unknown cg_metric '{cg_metric}'")

    return {
        "symbol": symbol,
        "name": name,
        "current": current,
        "change": None,
        "change_pct": change_pct,
        "source": "coingecko",
    }


__all__ = [
    "_DEFAULT_INDICES",
    "_INDEX_META",
    "_fetch_index_kr_current",
    "_fetch_index_kr_history",
    "_fetch_index_us_current",
    "_fetch_index_us_history",
    "_fetch_index_crypto_current",
]
