"""GET-only sources for the manual paper-runner bar producer."""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd

from app.core.symbol import to_db_symbol
from research.crypto_universe.exclusions import (
    STABLE_BASES,
    WRAPPED_DUPLICATE_BASES,
)
from research.crypto_universe.universe import is_leveraged_token
from research.stock_selection.dataset import decision_at

COLUMNS = [
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "traded_value",
    "available_at",
    "action",
]
NON_COMMON = re.compile(
    r"\b(etf|etn|fund|warrants?|units?|preferred|depositary|depositary shares)\b",
    re.IGNORECASE,
)


def canonical(rows: list[dict], market: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=COLUMNS)
    if not frame.empty:
        frame["available_at"] = [
            decision_at(market, date.fromisoformat(d)).isoformat() for d in frame.date
        ]
    return frame


class JsonGet:
    """Fixed endpoint calls only; no broker execution clients or raw errors."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def get(self, url: str, *, params=None, headers=None):
        # Bound request rate to 120/minute, including pagination.
        time.sleep(0.5)
        result = None
        failed = False
        try:
            response = self.client.get(url, params=params, headers=headers)
            if response.status_code != 200:
                failed = True
            else:
                result = response.json()
        except (httpx.HTTPError, ValueError):
            failed = True
        if failed:
            raise ValueError("market data request failed")
        return result


def us_assets(assets: list[dict]) -> list[str]:
    return sorted(
        {
            a["symbol"]
            for a in assets
            if a.get("status") == "active"
            and a.get("tradable") is True
            and a.get("class") == "us_equity"
            and a.get("exchange") in {"NYSE", "NASDAQ", "AMEX"}
            and re.fullmatch(r"[A-Z]+(?:\.[A-Z])?", a["symbol"])
            and not NON_COMMON.search(a.get("name", ""))
            and not any(
                a.get(k) is True
                for k in ("is_etf", "is_warrant", "is_unit", "is_preferred")
            )
        }
    )


def crypto_assets(info: dict) -> list[str]:
    return sorted(
        {
            a["symbol"]
            for a in info["symbols"]
            if a.get("status") == "TRADING"
            and a.get("quoteAsset") == "USDT"
            and a.get("isSpotTradingAllowed") is True
            and (a["baseAsset"] == "PAXG" or a["baseAsset"] not in STABLE_BASES)
            and a["baseAsset"] not in WRAPPED_DUPLICATE_BASES
            and not is_leveraged_token(a["baseAsset"])
            and re.fullmatch(r"[A-Z0-9]+USDT", a["symbol"])
        }
    )


def crypto_static_exclusions(info: dict) -> dict[str, str]:
    """Known static exclusions from the same exchangeInfo snapshot."""
    excluded = {}
    for asset in info["symbols"]:
        if (
            asset.get("status") != "TRADING"
            or asset.get("quoteAsset") != "USDT"
            or asset.get("isSpotTradingAllowed") is not True
            or not re.fullmatch(r"[A-Z0-9]+USDT", asset.get("symbol", ""))
        ):
            continue
        base = asset["baseAsset"]
        if base == "PAXG":
            continue
        if base in STABLE_BASES:
            excluded[asset["symbol"]] = "stable_list"
        elif base in WRAPPED_DUPLICATE_BASES:
            excluded[asset["symbol"]] = "wrapped_duplicate"
        elif is_leveraged_token(base):
            excluded[asset["symbol"]] = "leveraged_token"
    return excluded


class AlpacaSource:
    market = "us"
    benchmark = "SPY"

    def __init__(self, get: JsonGet, feed: str = "iex"):
        if feed not in {"iex", "sip"}:
            raise ValueError("feed must be iex or sip")
        self.get, self.feed = get, feed
        self.headers = {}
        for env, header in (
            ("ALPACA_DATA_API_KEY_ID", "APCA-API-KEY-ID"),
            ("ALPACA_DATA_API_SECRET_KEY", "APCA-API-SECRET-KEY"),
        ):
            value = os.environ.get(env, "").strip()
            if not value or not value.isascii() or any(ord(c) < 33 for c in value):
                raise ValueError(f"missing or invalid {env}")
            self.headers[header] = value

    def assets(self) -> list[str]:
        return us_assets(
            self.get.get(
                "https://paper-api.alpaca.markets/v2/assets",
                params={"status": "active", "asset_class": "us_equity"},
                headers=self.headers,
            )
        )

    def bars(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        rows = []
        for offset in range(0, len(symbols), 100):
            batch = symbols[offset : offset + 100]
            params = {
                "symbols": ",".join(batch),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": (end + timedelta(days=1)).isoformat(),
                "adjustment": "all",
                "feed": self.feed,
                "sort": "asc",
                "limit": 10000,
            }
            seen = set()
            for _ in range(10000):
                page = self.get.get(
                    "https://data.alpaca.markets/v2/stocks/bars",
                    params=params,
                    headers=self.headers,
                )
                for symbol, bars in page["bars"].items():
                    if symbol not in batch:
                        raise ValueError("unexpected Alpaca symbol")
                    for bar in bars:
                        day = (
                            pd.Timestamp(bar["t"]).tz_convert("America/New_York").date()
                        )
                        if start <= day <= end:
                            rows.append(
                                {
                                    "date": day.isoformat(),
                                    "symbol": to_db_symbol(symbol),
                                    **{
                                        k: bar[v]
                                        for k, v in (
                                            ("open", "o"),
                                            ("high", "h"),
                                            ("low", "l"),
                                            ("close", "c"),
                                            ("volume", "v"),
                                        )
                                    },
                                    "traded_value": float(bar["c"]) * float(bar["v"]),
                                    "action": False,
                                }
                            )
                token = page.get("next_page_token")
                if not token:
                    break
                if token in seen:
                    raise ValueError("nonadvancing Alpaca page")
                seen.add(token)
                params["page_token"] = token
            else:
                raise ValueError("Alpaca page cap")
        return canonical(rows, self.market)


class BinanceSource:
    market = "crypto"
    benchmark = "BTCUSDT"
    feed = "binance_spot_utc"

    def __init__(self, get: JsonGet):
        self.get = get
        self.excluded_assets: dict[str, str] = {}

    def assets(self) -> list[str]:
        info = self.get.get("https://data-api.binance.vision/api/v3/exchangeInfo")
        self.excluded_assets = crypto_static_exclusions(info)
        return crypto_assets(info)

    def bars(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        rows = []
        stop = int(
            datetime.combine(
                end + timedelta(days=1), datetime.min.time(), UTC
            ).timestamp()
            * 1000
        )
        for symbol in symbols:
            cursor = int(
                datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1000
            )
            for _ in range(10000):
                page = self.get.get(
                    "https://data-api.binance.vision/api/v3/klines",
                    params={
                        "symbol": symbol,
                        "interval": "1d",
                        "timeZone": "0",
                        "startTime": cursor,
                        "endTime": stop - 1,
                        "limit": 1000,
                    },
                )
                if not isinstance(page, list):
                    raise ValueError("invalid Binance klines")
                for bar in page:
                    stamp = int(bar[0])
                    if stamp < cursor or stamp >= stop or int(bar[6]) >= stop:
                        raise ValueError("out-of-range Binance candle")
                    rows.append(
                        {
                            "date": datetime.fromtimestamp(stamp / 1000, UTC)
                            .date()
                            .isoformat(),
                            "symbol": symbol,
                            **{
                                k: float(bar[i])
                                for i, k in enumerate(
                                    ("open", "high", "low", "close", "volume"), 1
                                )
                            },
                            "traded_value": float(bar[7]),
                            "action": False,
                        }
                    )
                if len(page) < 1000:
                    break
                next_cursor = int(page[-1][0]) + 86400000
                if next_cursor <= cursor:
                    raise ValueError("nonadvancing Binance page")
                cursor = next_cursor
                if cursor >= stop:
                    break
            else:
                raise ValueError("Binance page cap")
        return canonical(rows, self.market)
