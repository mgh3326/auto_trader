"""Upbit daily candle backfill script."""

import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

# Upbit API endpoint
UPBIT_API_URL = "https://api.upbit.com/v1"

# Data directory
DATA_DIR = Path(__file__).resolve().parent / "data"


def _parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Fetch Upbit candle data")
    parser.add_argument(
        "--symbols", nargs="*", help="Specific symbols to fetch (e.g., BTC ETH)"
    )
    parser.add_argument(
        "--days", type=int, default=730, help="Number of days to fetch (default: 730)"
    )
    parser.add_argument(
        "--top-n", type=int, default=100, help="Top N markets by volume (default: 100)"
    )
    parser.add_argument(
        "--interval",
        choices=["1d", "1h", "4h"],
        default="1d",
        help="Candle interval (default: 1d)",
    )
    return parser.parse_args()


def _data_dir_for_interval(interval: str) -> Path:
    if interval == "1d":
        return DATA_DIR
    return DATA_DIR / interval


def _filter_krw_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter markets to only KRW pairs."""
    return [m for m in markets if m.get("market", "").startswith("KRW-")]


def _select_top_n(markets: list[dict[str, Any]], top_n: int) -> list[str]:
    """Select top N markets by 24h traded value (acc_trade_price_24h)."""
    # Sort by acc_trade_price_24h (24h accumulated trade price) descending
    sorted_markets = sorted(
        markets, key=lambda x: x.get("acc_trade_price_24h", 0), reverse=True
    )
    return [m["market"] for m in sorted_markets[:top_n]]


def _normalize_symbols(symbols: list[str]) -> list[str]:
    """Normalize symbols to KRW-XXX format."""
    return [f"KRW-{s.upper()}" for s in symbols]


def _determine_refresh_days(
    existing_df: pd.DataFrame | None,
    requested_days: int,
    overlap_days: int = 7,
    today: datetime | None = None,
) -> int:
    """Return the number of days to fetch for an incremental refresh.

    If we already have local data, we only need a recent overlap window so the
    rerun can refresh the tail without re-downloading the entire range.
    """
    if existing_df is None or existing_df.empty:
        return max(1, int(requested_days))
    if "date" not in existing_df.columns:
        return max(1, min(int(requested_days), int(overlap_days)))

    reference = today or datetime.now()
    last_stored = pd.to_datetime(existing_df["date"], errors="coerce").max()
    if pd.isna(last_stored):
        return max(1, min(int(requested_days), int(overlap_days)))

    stale_days = max(0, (reference.date() - last_stored.date()).days)
    refresh_days = stale_days + int(overlap_days)
    return max(1, min(int(requested_days), refresh_days))


def _determine_refresh_hours(
    existing_df: pd.DataFrame | None,
    requested_hours: int,
    overlap_hours: int = 48,
    now: datetime | None = None,
) -> int:
    if existing_df is None or existing_df.empty:
        return max(1, int(requested_hours))
    if "date" not in existing_df.columns:
        return max(1, min(int(requested_hours), int(overlap_hours)))

    reference = now or datetime.now()
    last_stored = pd.to_datetime(existing_df["date"], errors="coerce").max()
    if pd.isna(last_stored):
        return max(1, min(int(requested_hours), int(overlap_hours)))

    stale_hours = max(0, int((reference - last_stored).total_seconds() / 3600))
    refresh_hours = stale_hours + int(overlap_hours)
    return max(1, min(int(requested_hours), refresh_hours))


def fetch_markets() -> list[dict[str, Any]]:
    """Fetch all available markets from Upbit."""
    url = f"{UPBIT_API_URL}/market/all"
    with httpx.Client() as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def fetch_candles(market: str, days: int) -> list[dict[str, Any]]:
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
            oldest_date = datetime.fromisoformat(
                candles[-1]["candle_date_time_utc"].replace("Z", "+00:00")
            )
            to_date = oldest_date - timedelta(days=1)
            remaining_days -= count

            # Rate limiting
            time.sleep(0.1)

    return all_candles


def fetch_candles_minutes(
    market: str, unit: int = 60, hours: int = 24
) -> list[dict[str, Any]]:
    url = f"{UPBIT_API_URL}/candles/minutes/{unit}"
    all_candles: list[dict[str, Any]] = []
    remaining = hours
    to_date = datetime.now()

    with httpx.Client() as client:
        while remaining > 0:
            count = min(200, remaining)
            params = {
                "market": market,
                "count": count,
                "to": to_date.strftime("%Y-%m-%dT%H:%M:%S"),
            }

            response = client.get(url, params=params)
            response.raise_for_status()
            candles = response.json()

            if not candles:
                break

            all_candles.extend(candles)

            oldest = datetime.fromisoformat(
                candles[-1]["candle_date_time_utc"].replace("Z", "+00:00")
            )
            to_date = oldest - timedelta(hours=1)
            remaining -= count

            time.sleep(0.11)

    return all_candles


def _normalize_candles(
    candles: list[dict[str, Any]], interval: str = "1d"
) -> pd.DataFrame:
    """Normalize Upbit API candle data to target schema."""
    if not candles:
        return pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "volume", "value"]
        )

    df = pd.DataFrame(candles)

    # Map Upbit columns to our schema
    df = df.rename(
        columns={
            "candle_date_time_utc": "date",
            "opening_price": "open",
            "high_price": "high",
            "low_price": "low",
            "trade_price": "close",
            "candle_acc_trade_volume": "volume",
            "candle_acc_trade_price": "value",
        }
    )

    # Keep only required columns
    df = df[["date", "open", "high", "low", "close", "volume", "value"]]

    if interval == "1d":
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    else:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%dT%H:%M:%S")

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


def save_candles(market: str, df: pd.DataFrame, data_dir: Path | None = None) -> None:
    """Save candles to parquet file.

    Args:
        market: Market code (e.g., "KRW-BTC")
        df: DataFrame with candle data
    """
    target_dir = data_dir or DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = target_dir / f"{market}.parquet"

    merged_df = _merge_with_existing(df, parquet_path)
    merged_df.to_parquet(parquet_path, index=False)


def _validate_data_quality(df: pd.DataFrame, interval: str) -> dict[str, float | int]:
    total_bars = len(df)
    if total_bars <= 1:
        return {
            "missing_pct": 0.0,
            "max_gap_hours": 0.0,
            "total_bars": total_bars,
        }

    dates = pd.to_datetime(df["date"]).sort_values()
    diffs = dates.diff().dropna()

    freq_map = {
        "1d": pd.Timedelta(days=1),
        "1h": pd.Timedelta(hours=1),
        "4h": pd.Timedelta(hours=4),
    }
    expected_freq = freq_map.get(interval, pd.Timedelta(days=1))

    missing_bars = 0
    max_gap = pd.Timedelta(0)
    for diff in diffs:
        if diff > expected_freq:
            gap_bars = int(diff / expected_freq) - 1
            missing_bars += gap_bars
            if diff > max_gap:
                max_gap = diff

    expected_total = total_bars + missing_bars
    missing_pct = (missing_bars / expected_total * 100) if expected_total > 0 else 0.0
    max_gap_hours = max_gap.total_seconds() / 3600

    return {
        "missing_pct": round(missing_pct, 2),
        "max_gap_hours": round(max_gap_hours, 1),
        "total_bars": total_bars,
    }


def main() -> None:
    """Main entry point."""
    args = _parse_args()
    interval = args.interval
    target_dir = _data_dir_for_interval(interval)
    unit_map = {"1h": 60, "4h": 240}

    # Determine which markets to fetch
    if args.symbols:
        markets = _normalize_symbols(args.symbols)
    else:
        print(f"Fetching top {args.top_n} KRW markets...")
        all_markets = fetch_markets()
        krw_markets = _filter_krw_markets(all_markets)

        # Fetch trade prices to sort by volume
        markets_with_price = []
        for market in krw_markets:
            try:
                with httpx.Client() as client:
                    response = client.get(
                        f"{UPBIT_API_URL}/ticker", params={"markets": market["market"]}
                    )
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

    print(f"Fetching {args.days} days ({interval}) for {len(markets)} markets...")

    for market in markets:
        try:
            parquet_path = target_dir / f"{market}.parquet"
            existing_df = (
                pd.read_parquet(parquet_path) if parquet_path.exists() else None
            )

            print(f"  Fetching {market}...", end=" ")

            if interval == "1d":
                refresh_days = _determine_refresh_days(existing_df, args.days)
                candles = fetch_candles(market, refresh_days)
            else:
                total_hours = args.days * 24
                refresh_hours = _determine_refresh_hours(existing_df, total_hours)
                candles = fetch_candles_minutes(
                    market, unit=unit_map[interval], hours=refresh_hours
                )

            if candles:
                df = _normalize_candles(candles, interval=interval)
                save_candles(market, df, data_dir=target_dir)
                quality = _validate_data_quality(df, interval)
                status = f"OK ({int(quality['total_bars'])} bars"
                if quality["missing_pct"] > 0:
                    status += f", {quality['missing_pct']:.1f}% missing"
                if quality["max_gap_hours"] > 6:
                    status += f", WARNING: {quality['max_gap_hours']:.0f}h gap"
                status += ")"
                print(status)
            else:
                print("✗ (no data)")
        except Exception as e:
            print(f"✗ ({e})")

    print("Done!")


if __name__ == "__main__":
    main()
