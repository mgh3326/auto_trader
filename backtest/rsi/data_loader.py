"""Upbit 1h candle data loader with parquet caching."""

import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

UPBIT_API_URL = "https://api.upbit.com/v1"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "1h"
MAX_CANDLES_PER_REQUEST = 200
REQUEST_INTERVAL = 0.11  # seconds between requests (stay under 10/sec)


def fetch_krw_markets() -> list[str]:
    """Fetch all KRW market codes from Upbit.

    Returns:
        List of market codes like ["KRW-BTC", "KRW-ETH", ...].
    """
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{UPBIT_API_URL}/market/all", params={"is_details": "false"})
        resp.raise_for_status()
        markets = resp.json()
    return [m["market"] for m in markets if m["market"].startswith("KRW-")]


def fetch_hourly_candles(market: str, start: str, end: str) -> list[dict]:
    """Fetch 1h candles from Upbit API with backward pagination.

    Args:
        market: Market code (e.g., "KRW-BTC").
        start: Start date "YYYY-MM-DD" (inclusive).
        end: End date "YYYY-MM-DD" (inclusive, fetches up to end+1 day 00:00 KST).

    Returns:
        Raw candle dicts from Upbit API.
    """
    end_dt = datetime.fromisoformat(f"{end}T23:59:59")
    start_dt = datetime.fromisoformat(f"{start}T00:00:00")
    to_dt = end_dt + timedelta(hours=1)

    all_candles: list[dict] = []

    with httpx.Client(timeout=30) as client:
        while True:
            params = {
                "market": market,
                "count": MAX_CANDLES_PER_REQUEST,
                "to": to_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            resp = client.get(f"{UPBIT_API_URL}/candles/minutes/60", params=params)
            resp.raise_for_status()
            candles = resp.json()

            if not candles:
                break

            all_candles.extend(candles)

            # Oldest candle in this batch
            oldest_kst = candles[-1]["candle_date_time_kst"]
            oldest_dt = datetime.fromisoformat(oldest_kst)

            if oldest_dt <= start_dt:
                break

            # Next page: before the oldest candle
            to_dt = oldest_dt
            time.sleep(REQUEST_INTERVAL)

    return all_candles


def normalize_candles(raw: list[dict]) -> pd.DataFrame:
    """Normalize Upbit candle response to standard DataFrame.

    Columns: datetime, open, high, low, close, volume, value.
    datetime is KST ISO string "YYYY-MM-DDTHH:MM:SS".
    """
    if not raw:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "value"])

    df = pd.DataFrame(raw)
    df = df.rename(columns={
        "candle_date_time_kst": "datetime",
        "opening_price": "open",
        "high_price": "high",
        "low_price": "low",
        "trade_price": "close",
        "candle_acc_trade_volume": "volume",
        "candle_acc_trade_price": "value",
    })
    df = df[["datetime", "open", "high", "low", "close", "volume", "value"]]
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def merge_with_existing(new_df: pd.DataFrame, parquet_path: Path) -> pd.DataFrame:
    """Merge new candles with existing parquet, deduplicating by datetime."""
    if not parquet_path.exists():
        return new_df

    existing = pd.read_parquet(parquet_path)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["datetime"], keep="last")
    combined = combined.sort_values("datetime").reset_index(drop=True)
    return combined


def save_candles(market: str, df: pd.DataFrame, data_dir: Path = DATA_DIR) -> None:
    """Save candles to parquet with merge."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{market}.parquet"
    merged = merge_with_existing(df, path)
    merged.to_parquet(path, index=False)


def load_candles(
    market: str,
    start: str,
    end: str,
    data_dir: Path = DATA_DIR,
) -> pd.DataFrame | None:
    """Load cached candles for a date range.

    Args:
        market: Market code.
        start: Start date "YYYY-MM-DD".
        end: End date "YYYY-MM-DD".
        data_dir: Directory containing parquet files.

    Returns:
        Filtered DataFrame or None if no cache exists.
    """
    path = data_dir / f"{market}.parquet"
    if not path.exists():
        return None

    df = pd.read_parquet(path)
    start_dt = f"{start}T00:00:00"
    end_dt = f"{end}T23:59:59"
    mask = (df["datetime"] >= start_dt) & (df["datetime"] <= end_dt)
    filtered = df[mask].reset_index(drop=True)
    return filtered if len(filtered) > 0 else None


def fetch_and_cache(
    market: str,
    start: str,
    end: str,
    data_dir: Path = DATA_DIR,
) -> pd.DataFrame:
    """Fetch 1h candles from API, cache to parquet, and return.

    Performs incremental fetch: only downloads data not yet cached.
    """
    cached = load_candles(market, start, end, data_dir)
    if cached is not None and len(cached) > 0:
        # Check if we have enough coverage
        first_cached = cached["datetime"].iloc[0][:10]
        last_cached = cached["datetime"].iloc[-1][:10]
        if first_cached <= start and last_cached >= end:
            return cached

    raw = fetch_hourly_candles(market, start, end)
    df = normalize_candles(raw)
    if len(df) > 0:
        save_candles(market, df, data_dir)
    return load_candles(market, start, end, data_dir) or df


def fetch_all_universe(
    start: str,
    end: str,
    top_n_prefetch: int = 100,
    data_dir: Path = DATA_DIR,
) -> dict[str, pd.DataFrame]:
    """Fetch 1h candles for wider universe, cache, and return.

    Fetches current top markets by volume as a pragmatic pre-filter.
    Survivorship bias is accepted for MVP.

    Args:
        start: Backtest start date.
        end: Backtest end date.
        top_n_prefetch: How many markets to pre-fetch (wider than top_n).
        data_dir: Cache directory.

    Returns:
        Dict mapping market code to DataFrame.
    """
    # Get current top markets by 24h volume
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{UPBIT_API_URL}/ticker/all", params={"quoteCurrencies": "KRW"})
        resp.raise_for_status()
        tickers = resp.json()

    # Sort by 24h trade value descending
    tickers.sort(key=lambda t: t.get("acc_trade_price_24h", 0), reverse=True)
    markets = [t["market"] for t in tickers[:top_n_prefetch]]

    all_data: dict[str, pd.DataFrame] = {}
    total = len(markets)

    for i, market in enumerate(markets, 1):
        print(f"  [{i}/{total}] {market}...", end=" ", flush=True)
        try:
            df = fetch_and_cache(market, start, end, data_dir)
            if df is not None and len(df) > 0:
                all_data[market] = df
                print(f"{len(df)} bars")
            else:
                print("no data")
        except Exception as exc:
            print(f"error: {exc}")

    return all_data
