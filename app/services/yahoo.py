# app/services/yahoo.py
from datetime import UTC, datetime, timedelta

import pandas as pd
import yfinance as yf

from app.core.symbol import to_yahoo_symbol


def _flatten_cols(df: pd.DataFrame) -> pd.DataFrame:
    """('open','NVDA') → open  처럼 1단 컬럼으로 변환"""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]  # level-0 만 취함
    else:
        df.columns = [c.lower() for c in df.columns]
    return df


async def fetch_ohlcv(
    ticker: str,
    days: int = 100,
    period: str = "day",
    end_date: datetime | None = None,
) -> pd.DataFrame:
    """최근 days개 OHLCV DataFrame 반환 (Yahoo Finance)

    Parameters
    ----------
    ticker : str
        종목 심볼 (DB 형식)
    days : int, default 100
        가져올 캔들 수
    period : str, default "day"
        캔들 주기 ("day", "week", "month")
    end_date : datetime | None, default None
        조회 기준 시간 (None이면 현재 시간)
    """
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

    df = yf.download(
        yahoo_ticker,
        start=start,
        end=end,
        interval=period_map[period],
        progress=False,
        auto_adjust=False,
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


async def fetch_price(ticker: str) -> pd.DataFrame:
    """미국 장중 현재가(15분 지연) 1행 DF – yfinance fast_info"""
    yahoo_ticker = to_yahoo_symbol(ticker)  # DB형식 . -> Yahoo형식 -
    info = yf.Ticker(yahoo_ticker).fast_info
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
    info = yf.Ticker(yahoo_ticker).info

    fundamental_data = {
        "PER": info.get("trailingPE"),
        "PBR": info.get("priceToBook"),
        "EPS": info.get("trailingEps"),
        "BPS": info.get("bookValue"),
        "Dividend Yield": info.get("trailingAnnualDividendYield"),
    }
    return fundamental_data
