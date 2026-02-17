# app/services/yahoo.py
from datetime import UTC, datetime, timedelta

import pandas as pd
import yfinance as yf

from app.core.config import settings
from app.core.symbol import to_yahoo_symbol
from app.monitoring import build_yfinance_tracing_session


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
    period_map = {
        "day": "1d",
        "week": "1wk",
        "month": "1mo",
    }
    if period not in period_map:
        raise ValueError(f"period must be one of {list(period_map.keys())}")

    yahoo_ticker = to_yahoo_symbol(ticker)  # DB형식 . -> Yahoo형식 -
    end = (end_date.date() if end_date else datetime.now(UTC).date()) + timedelta(
        days=1
    )

    # 주봉/월봉은 더 넓은 기간 필요
    multiplier = {"day": 2, "week": 10, "month": 40}.get(period, 2)
    start = end - timedelta(days=days * multiplier)
    session = build_yfinance_tracing_session()

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


async def fetch_price(ticker: str) -> pd.DataFrame:
    """미국 장중 현재가(15분 지연) 1행 DF – yfinance fast_info"""
    yahoo_ticker = to_yahoo_symbol(ticker)  # DB형식 . -> Yahoo형식 -
    session = build_yfinance_tracing_session()
    info = yf.Ticker(yahoo_ticker, session=session).fast_info
    row = {
        "code": ticker,  # DB 형식 유지
        "date": datetime.now(UTC).date(),
        "time": datetime.now(UTC).time(),
        "open": getattr(info, "open", 0.0),
        "high": getattr(info, "day_high", 0.0),
        "low": getattr(info, "day_low", 0.0),
        "close": getattr(info, "last_price", 0.0),  # ← 이렇게!
        "volume": getattr(info, "last_volume", 0),
        "value": 0,
    }
    return pd.DataFrame([row]).set_index("code")


async def fetch_fundamental_info(ticker: str) -> dict:
    """
    yf.Ticker(ticker).info에서 PER, PBR, EPS, BPS, 배당수익률 등
    주요 펀더멘털 지표를 가져와 딕셔너리로 반환합니다.
    """
    yahoo_ticker = to_yahoo_symbol(ticker)  # DB형식 . -> Yahoo형식 -
    session = build_yfinance_tracing_session()
    info = yf.Ticker(yahoo_ticker, session=session).info

    fundamental_data = {
        "PER": info.get("trailingPE"),
        "PBR": info.get("priceToBook"),
        "EPS": info.get("trailingEps"),
        "BPS": info.get("bookValue"),
        "Dividend Yield": info.get("trailingAnnualDividendYield"),
    }
    return fundamental_data
