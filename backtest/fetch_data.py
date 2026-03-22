"""Upbit daily candle backfill script."""

import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

# Upbit API endpoint
UPBIT_API_URL = "https://api.upbit.com/v1"

# Data directory
DATA_DIR = Path(__file__).resolve().parent / "data"


def _parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Fetch Upbit daily candle data")
    parser.add_argument("--symbols", nargs="*", help="Specific symbols to fetch (e.g., BTC ETH)")
    parser.add_argument("--days", type=int, default=730, help="Number of days to fetch (default: 730)")
    parser.add_argument("--top-n", type=int, default=100, help="Top N markets by volume (default: 100)")
    return parser.parse_args()


def _filter_krw_markets(markets: list[dict]) -> list[dict]:
    """Filter markets to only KRW pairs."""
    return [m for m in markets if m.get("market", "").startswith("KRW-")]


def _select_top_n(markets: list[dict], top_n: int) -> list[str]:
    """Select top N markets by 24h traded value (acc_trade_price_24h)."""
    # Sort by acc_trade_price_24h (24h accumulated trade price) descending
    sorted_markets = sorted(
        markets, key=lambda x: x.get("acc_trade_price_24h", 0), reverse=True
    )
    return [m["market"] for m in sorted_markets[:top_n]]


def _normalize_symbols(symbols: list[str]) -> list[str]:
    """Normalize symbols to KRW-XXX format."""
    return [f"KRW-{s.upper()}" for s in symbols]


def fetch_markets() -> list[dict]:
    """Fetch all available markets from Upbit."""
    url = f"{UPBIT_API_URL}/market/all"
    with httpx.Client() as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def fetch_candles(market: str, days: int) -> list[dict]:
    """Fetch daily candles for a market.

    Args:
        market: Market code (e.g., "KRW-BTC")
        days: Number of days to fetch

    Returns:
        List of candle data
    """
    url = f"{UPBIT_API_URL}/candles/days"
    all_candles = []

    # Calculate number of requests needed (max 200 per request)
    to_date = datetime.now()
    remaining_days = days

    with httpx.Client() as client:
        while remaining_days > 0:
            count = min(200, remaining_days)
            params = {
                "market": market,
                "count": count,
                "to": to_date.strftime("%Y-%m-%d %H:%M:%S"),
            }

            response = client.get(url, params=params)
            response.raise_for_status()
            candles = response.json()

            if not candles:
                break

            all_candles.extend(candles)

            # Update to_date for next pagination
            oldest_date = datetime.fromisoformat(candles[-1]["candle_date_time_utc"].replace("Z", "+00:00"))
            to_date = oldest_date - timedelta(days=1)
            remaining_days -= count

            # Rate limiting
            time.sleep(0.1)

    return all_candles


def _normalize_candles(candles: list[dict]) -> pd.DataFrame:
    """Normalize Upbit API candle data to target schema."""
    if not candles:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "value"])

    df = pd.DataFrame(candles)

    # Map Upbit columns to our schema
    df = df.rename(columns={
        "candle_date_time_utc": "date",
        "opening_price": "open",
        "high_price": "high",
        "low_price": "low",
        "trade_price": "close",
        "candle_acc_trade_volume": "volume",
        "candle_acc_trade_price": "value",
    })

    # Keep only required columns
    df = df[["date", "open", "high", "low", "close", "volume", "value"]]

    # Convert date to string format (YYYY-MM-DD)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    # Sort by date ascending
    df = df.sort_values("date").reset_index(drop=True)

    return df


def _merge_with_existing(new_df: pd.DataFrame, parquet_path: Path) -> pd.DataFrame:
    """Merge new data with existing parquet data.

    Args:
        new_df: New data to merge
        parquet_path: Path to existing parquet file

    Returns:
        Merged DataFrame with deduplicated data
    """
    if not parquet_path.exists():
        return new_df

    existing_df = pd.read_parquet(parquet_path)

    # Combine existing and new data
    combined = pd.concat([existing_df, new_df], ignore_index=True)

    # Drop duplicates, keeping the last occurrence (newer data)
    combined = combined.drop_duplicates(subset=["date"], keep="last")

    # Sort by date ascending
    combined = combined.sort_values("date").reset_index(drop=True)

    return combined


def save_candles(market: str, df: pd.DataFrame) -> None:
    """Save candles to parquet file.

    Args:
        market: Market code (e.g., "KRW-BTC")
        df: DataFrame with candle data
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    parquet_path = DATA_DIR / f"{market}.parquet"

    merged_df = _merge_with_existing(df, parquet_path)
    merged_df.to_parquet(parquet_path, index=False)


def main() -> None:
    """Main entry point."""
    args = _parse_args()

    # Determine which markets to fetch
    if args.symbols:
        markets = _normalize_symbols(args.symbols)
    else:
        print(f"Fetching top {args.top_n} KRW markets...")
        all_markets = fetch_markets()
        krw_markets = _filter_krw_markets(all_markets)

        # Fetch trade prices to sort by volume
        markets_with_price = []
        for market in krw_markets[:args.top_n * 2]:  # Fetch extra for safety
            try:
                with httpx.Client() as client:
                    response = client.get(f"{UPBIT_API_URL}/ticker", params={"markets": market["market"]})
                    if response.status_code == 200:
                        ticker = response.json()[0]
                        market["acc_trade_price_24h"] = ticker.get(
                            "acc_trade_price_24h", 0
                        )
                        markets_with_price.append(market)
                time.sleep(0.05)
            except Exception:
                pass

        markets = _select_top_n(markets_with_price, args.top_n)

    print(f"Fetching {args.days} days of data for {len(markets)} markets...")

    for market in markets:
        try:
            print(f"  Fetching {market}...", end=" ")
            candles = fetch_candles(market, args.days)
            if candles:
                df = _normalize_candles(candles)
                save_candles(market, df)
                print(f"✓ ({len(df)} candles)")
            else:
                print("✗ (no data)")
        except Exception as e:
            print(f"✗ ({e})")

    print("Done!")


if __name__ == "__main__":
    main()
