# app/services/yahoo.py
import asyncio
import logging
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import yfinance as yf

from app.core.config import settings
from app.core.symbol import to_yahoo_symbol
from app.monitoring import build_yfinance_tracing_session, close_yfinance_session

logger = logging.getLogger(__name__)


def _flatten_cols(df: pd.DataFrame) -> pd.DataFrame:
    """('open','NVDA') → open  처럼 1단 컬럼으로 변환"""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]  # level-0 만 취함
    else:
        df.columns = [c.lower() for c in df.columns]
    return df


def _bucket_key(period: str, bucket_date) -> tuple[int, int, int]:
    normalized_period = str(period or "").strip().lower()
    if normalized_period == "day":
        return (bucket_date.year, bucket_date.month, bucket_date.day)
    if normalized_period == "week":
        iso = bucket_date.isocalendar()
        return (iso.year, iso.week, 0)
    if normalized_period == "month":
        return (bucket_date.year, bucket_date.month, 0)
    raise ValueError("period must be one of ['day', 'week', 'month']")


def _fast_info_get(info: Any, *keys: str) -> Any:
    for key in keys:
        value = getattr(info, key, None)
        if value is None and hasattr(info, "get"):
            try:
                value = info.get(key)
            except Exception:
                value = None
        if value is not None:
            return value
    return None


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


_CRUMB_RETRY_MAX = 1


@contextmanager
def yfinance_tracing_session() -> Iterator[Any]:
    """Create a Yahoo/yfinance session and close it after each attempt.

    Keep this wrapper local so tests and retry paths that monkeypatch
    build_yfinance_tracing_session still exercise fresh-session behavior.
    """
    session = build_yfinance_tracing_session()
    try:
        yield session
    finally:
        close_yfinance_session(session)


def _is_crumb_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
        return True
    msg = str(exc).lower()
    return "invalid crumb" in msg or "invalid cookie" in msg


async def fetch_ohlcv(
    ticker: str,
    days: int = 100,
    period: str = "day",
    end_date: datetime | None = None,
) -> pd.DataFrame:
    normalized_period = str(period or "").strip().lower()

    if (
        normalized_period in {"day", "week", "month"}
        and settings.yahoo_ohlcv_cache_enabled
    ):
        from app.services import yahoo_ohlcv_cache as yahoo_ohlcv_cache_service

        cached = await yahoo_ohlcv_cache_service.get_closed_candles(
            ticker,
            count=days,
            period=normalized_period,
            raw_fetcher=_fetch_ohlcv_raw,
        )
        if cached is not None:
            return cached

    raw = await _fetch_ohlcv_raw(
        ticker=ticker,
        days=days,
        period=normalized_period,
        end_date=end_date,
    )
    if normalized_period in {"day", "week", "month"}:
        return _filter_closed_buckets_nyse(raw, normalized_period)
    return raw


async def _fetch_ohlcv_raw(
    ticker: str,
    days: int = 100,
    period: str = "day",
    end_date: datetime | None = None,
) -> pd.DataFrame:
    period_map = {"day": "1d", "week": "1wk", "month": "1mo", "1h": "60m"}
    if period not in period_map:
        raise ValueError(f"period must be one of {list(period_map.keys())}")

    yahoo_ticker = to_yahoo_symbol(ticker)
    end = (end_date.date() if end_date else datetime.now(UTC).date()) + timedelta(
        days=1
    )
    multiplier = {"day": 2, "week": 10, "month": 40, "1h": 2}.get(period, 2)
    start = end - timedelta(days=days * multiplier)

    last_exc: BaseException | None = None
    for attempt in range(_CRUMB_RETRY_MAX + 1):
        try:
            with yfinance_tracing_session() as session:
                df = yf.download(
                    yahoo_ticker,
                    start=start,
                    end=end,
                    interval=period_map[period],
                    progress=False,
                    auto_adjust=False,
                    session=session,
                )
            df = _flatten_cols(df).reset_index(names="date")
            df = (
                df.assign(date=lambda d: pd.to_datetime(d["date"]).dt.date)
                .loc[:, ["date", "open", "high", "low", "close", "volume"]]
                .tail(days)
                .reset_index(drop=True)
            )
            if df.empty:
                raise ValueError(f"{ticker} OHLCV not found")
            return df
        except Exception as exc:
            last_exc = exc
            if _is_crumb_auth_error(exc) and attempt < _CRUMB_RETRY_MAX:
                logger.warning(
                    "Yahoo crumb/auth error for %s OHLCV (attempt %d/%d), retrying: %s",
                    yahoo_ticker,
                    attempt + 1,
                    _CRUMB_RETRY_MAX + 1,
                    exc,
                )
                continue
            raise

    raise last_exc  # type: ignore[misc]


def _filter_closed_buckets_nyse(
    df: pd.DataFrame,
    period: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df

    normalized_period = str(period or "").strip().lower()
    if normalized_period not in {"day", "week", "month"}:
        return df

    from app.services import yahoo_ohlcv_cache as yahoo_ohlcv_cache_service

    last_closed_bucket = yahoo_ohlcv_cache_service.get_last_closed_bucket_nyse(
        normalized_period,
        now,
    )
    parsed_dates = pd.to_datetime(df["date"], errors="coerce")
    valid_mask = parsed_dates.notna()
    bucket_keys = parsed_dates.loc[valid_mask].dt.date.map(
        lambda bucket_date: _bucket_key(normalized_period, bucket_date)
    )
    target_key = _bucket_key(normalized_period, last_closed_bucket)
    keep_mask = pd.Series(False, index=df.index)
    keep_mask.loc[valid_mask] = bucket_keys <= target_key
    return df.loc[keep_mask].sort_values("date").reset_index(drop=True)


def _fetch_fast_info_sync(ticker: str) -> dict[str, Any]:
    yahoo_ticker = to_yahoo_symbol(ticker)
    last_exc: BaseException | None = None

    for attempt in range(_CRUMB_RETRY_MAX + 1):
        try:
            with yfinance_tracing_session() as session:
                info = yf.Ticker(yahoo_ticker, session=session).fast_info
            return {
                "symbol": ticker,
                "previous_close": _to_float_or_none(
                    _fast_info_get(
                        info,
                        "regular_market_previous_close",
                        "regularMarketPreviousClose",
                        "previous_close",
                        "previousClose",
                    )
                ),
                "open": _to_float_or_none(_fast_info_get(info, "open")),
                "high": _to_float_or_none(_fast_info_get(info, "day_high", "dayHigh")),
                "low": _to_float_or_none(_fast_info_get(info, "day_low", "dayLow")),
                "close": _to_float_or_none(
                    _fast_info_get(
                        info,
                        "last_price",
                        "lastPrice",
                        "regular_market_price",
                        "regularMarketPrice",
                    )
                ),
                "volume": _to_int_or_none(
                    _fast_info_get(info, "last_volume", "lastVolume", "volume")
                ),
            }
        except Exception as exc:
            last_exc = exc
            if _is_crumb_auth_error(exc) and attempt < _CRUMB_RETRY_MAX:
                logger.warning(
                    "Yahoo crumb/auth error for %s (attempt %d/%d), retrying with fresh session: %s",
                    yahoo_ticker,
                    attempt + 1,
                    _CRUMB_RETRY_MAX + 1,
                    exc,
                )
                continue
            raise

    raise last_exc  # type: ignore[misc]


def _fetch_price_sync(ticker: str) -> pd.DataFrame:
    """Blocking Yahoo fast_info call for single ticker."""
    fast_info = _fetch_fast_info_sync(ticker)
    row = {
        "code": ticker,  # DB 형식 유지
        "date": datetime.now(UTC).date(),
        "time": datetime.now(UTC).time(),
        "open": fast_info.get("open") or 0.0,
        "high": fast_info.get("high") or 0.0,
        "low": fast_info.get("low") or 0.0,
        "close": fast_info.get("close") or 0.0,
        "volume": fast_info.get("volume") or 0,
        "value": 0,
    }
    return pd.DataFrame([row]).set_index("code")


async def fetch_price(ticker: str) -> pd.DataFrame:
    """미국 장중 현재가(15분 지연) 1행 DF – yfinance fast_info"""
    return await asyncio.to_thread(_fetch_price_sync, ticker)


async def fetch_fast_info(ticker: str) -> dict[str, Any]:
    return await asyncio.to_thread(_fetch_fast_info_sync, ticker)


async def fetch_fundamental_info(ticker: str) -> dict:
    yahoo_ticker = to_yahoo_symbol(ticker)

    def _fetch_sync() -> dict:
        last_exc: BaseException | None = None
        for attempt in range(_CRUMB_RETRY_MAX + 1):
            try:
                with yfinance_tracing_session() as session:
                    info = yf.Ticker(yahoo_ticker, session=session).info
                return {
                    "PER": info.get("trailingPE"),
                    "PBR": info.get("priceToBook"),
                    "EPS": info.get("trailingEps"),
                    "BPS": info.get("bookValue"),
                    "Dividend Yield": info.get("trailingAnnualDividendYield"),
                }
            except Exception as exc:
                last_exc = exc
                if _is_crumb_auth_error(exc) and attempt < _CRUMB_RETRY_MAX:
                    logger.warning(
                        "Yahoo crumb/auth error for %s fundamentals (attempt %d/%d), retrying: %s",
                        yahoo_ticker,
                        attempt + 1,
                        _CRUMB_RETRY_MAX + 1,
                        exc,
                    )
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    return await asyncio.to_thread(_fetch_sync)
