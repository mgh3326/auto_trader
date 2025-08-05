# app/services/yahoo.py
import pandas as pd, yfinance as yf
from datetime import datetime, timedelta, timezone


def _flatten_cols(df: pd.DataFrame) -> pd.DataFrame:
    """('open','NVDA') → open  처럼 1단 컬럼으로 변환"""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]  # level-0 만 취함
    else:
        df.columns = [c.lower() for c in df.columns]
    return df


async def fetch_ohlcv(ticker: str, days: int = 100) -> pd.DataFrame:
    """최근 days개(최대 100) 일봉 OHLCV DataFrame 반환"""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days * 2)  # 휴일 감안 넉넉히
    df = yf.download(ticker,
                     start=start, end=end, interval="1d",
                     progress=False, auto_adjust=False)
    df = _flatten_cols(df).reset_index(names="date")  # ← 여기만 변경
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
    info = yf.Ticker(ticker).fast_info
    row = {
        "code": ticker,
        "date": datetime.now(timezone.utc).date(),
        "time": datetime.now(timezone.utc).time(),
        "open": getattr(info, "open", 0.0),
        "high": getattr(info, "day_high", 0.0),
        "low": getattr(info, "day_low", 0.0),
        "close": getattr(info, "last_price", 0.0),  # ← 이렇게!
        "volume": getattr(info, "last_volume", 0),
        "value": 0,
    }
    return pd.DataFrame([row]).set_index("code")
