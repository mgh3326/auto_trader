"""Explicit public-only historical download, for diagnostic runs.

Uses the existing data-plane GET allowlist, not an authenticated app client.
No universe is silently chosen: pass symbols or a file enumerating the universe.
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
import pandas as pd

from app.core.symbol import to_db_symbol, to_yahoo_symbol
from research.data_plane.public_http import make_public_fetch

from .dataset import decision_at
from .io import add_input_args, digest, write_json

BENCHMARK = {"kr": "^KS11", "us": "^GSPC", "crypto": "KRW-BTC"}


def naver_daily(
    code: str, start: date, end: date, *, rejections: list[dict] | None = None
) -> list[dict]:
    """One fixed public chart endpoint, no redirects or credential clients.

    The whole selected series uses Naver; individual price rows are never patched.
    """
    if code != "KOSPI" and not re.fullmatch(r"\d{6}", code):
        raise ValueError("Naver only supports explicit KR codes or KOSPI here")
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = client.get(
            "https://fchart.stock.naver.com/sise.nhn",
            params={
                "symbol": code,
                "timeframe": "day",
                "count": "5000",
                "requestType": "0",
            },
        )
        response.raise_for_status()
    entries = [
        element.attrib["data"].split("|")
        for element in ET.fromstring(response.content.decode("euc-kr")).iter("item")
    ]
    if not entries or datetime.strptime(entries[0][0], "%Y%m%d").date() > start:
        raise ValueError("Naver index window does not cover requested start")
    rows = []
    for fields in entries:
        day = datetime.strptime(fields[0], "%Y%m%d").date()
        if start <= day < end:
            values = dict(
                zip(
                    ("open", "high", "low", "close", "volume"),
                    map(float, fields[1:]),
                    strict=True,
                )
            )
            if any(values[key] <= 0 for key in ("open", "high", "low", "close")):
                if rejections is not None:
                    rejections.append(
                        {
                            "symbol": code,
                            "date": day.isoformat(),
                            "reason": "nonpositive_ohlc",
                        }
                    )
                continue  # suspended/invalid OHLC stays a calendar gap
            if values["high"] < max(
                values["open"], values["close"], values["low"]
            ) or values["low"] > min(values["open"], values["close"], values["high"]):
                if rejections is not None:
                    rejections.append(
                        {
                            "symbol": code,
                            "date": day.isoformat(),
                            "reason": "inconsistent_ohlc",
                        }
                    )
                continue  # provider rounding anomalies are reported, never repaired
            rows.append(
                {
                    "date": day.isoformat(),
                    **values,
                    "traded_value": values["close"] * values["volume"],
                    "action": False,
                }
            )
    return rows


def yahoo(
    fetch, ticker: str, start: date, end: date, *, event_dates: set[str] | None = None
) -> list[dict]:
    params = urlencode(
        {
            "period1": int(
                datetime.combine(start, datetime.min.time(), UTC).timestamp()
            ),
            "period2": int(datetime.combine(end, datetime.min.time(), UTC).timestamp()),
            "interval": "1d",
            "events": "div,splits",
        }
    )
    response = fetch(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='')}?{params}"
    )
    if response.status != 200:
        raise ValueError(f"Yahoo HTTP {response.status} for {ticker}")
    result = json.loads(response.body)["chart"]["result"][0]
    prices = result["indicators"]["quote"][0]
    actions = {
        datetime.fromtimestamp(event["date"], UTC).date()
        for events in result.get("events", {}).values()
        for event in events.values()
    }
    if event_dates is not None:
        event_dates.update(day.isoformat() for day in actions if start <= day < end)
    rows = []
    for i, stamp in enumerate(result.get("timestamp", [])):
        day = datetime.fromtimestamp(stamp, UTC).date()
        if not start <= day < end:
            continue
        values = {
            name: prices[name][i] for name in ("open", "high", "low", "close", "volume")
        }
        if any(v is None for v in values.values()):
            continue  # counted as a gap by calendar alignment, never forward filled
        rows.append(
            {
                "date": day.isoformat(),
                **values,
                "traded_value": values["close"] * values["volume"],
                "action": day in actions,
            }
        )
    return rows


def upbit(fetch, ticker: str, start: date, end: date) -> list[dict]:
    cursor = end.isoformat() + "T00:00:00Z"
    found = {}
    for _ in range(40):
        params = urlencode({"market": ticker, "count": 200, "to": cursor})
        response = fetch(f"https://api.upbit.com/v1/candles/days?{params}")
        if response.status != 200:
            raise ValueError(f"Upbit HTTP {response.status} for {ticker}")
        page = json.loads(response.body)
        if not page:
            break
        oldest = min(c["candle_date_time_utc"] for c in page)
        for candle in page:
            day = candle["candle_date_time_utc"][:10]
            if start.isoformat() <= day < end.isoformat():
                found[day] = {
                    "date": day,
                    "open": candle["opening_price"],
                    "high": candle["high_price"],
                    "low": candle["low_price"],
                    "close": candle["trade_price"],
                    "volume": candle["candle_acc_trade_volume"],
                    "traded_value": candle["candle_acc_trade_price"],
                    "action": False,
                }
        if oldest[:10] <= start.isoformat() or len(page) < 200:
            break
        new_cursor = oldest + "Z"
        if new_cursor >= cursor:
            raise ValueError("nonadvancing Upbit page")
        cursor = new_cursor
    else:
        raise ValueError("Upbit page cap reached")
    return [found[d] for d in sorted(found)]


def fetch_snapshot(
    root: Path,
    market: str,
    tickers: list[str],
    start: date,
    end: date,
    *,
    benchmark: str | None = None,
    kr_source: str = "yahoo",
) -> Path:
    path = root / market / "bars.csv"
    if path.exists() or path.with_suffix(".json").exists():
        raise FileExistsError("snapshot already exists; choose a fresh cache directory")
    if start >= end or not tickers or len(set(tickers)) != len(tickers):
        raise ValueError("unique explicit symbols and start < end required")
    if kr_source not in ("yahoo", "naver") or (market != "kr" and kr_source != "yahoo"):
        raise ValueError("kr_source applies only to KR")
    fetch = make_public_fetch(timeout=15, attempts=2)
    rows, coverage, rejections = [], [], []
    for ticker in [*tickers, benchmark or BENCHMARK[market]]:
        is_benchmark = len(coverage) == len(tickers)
        if market == "kr" and not is_benchmark:
            match = re.fullmatch(r"(\d{6})\.(KS|KQ)", ticker)
            if not match:
                raise ValueError(
                    "KR tickers require explicit six-digit .KS or .KQ venue"
                )
            symbol = match[1]
        elif market == "us" and not is_benchmark:
            symbol = to_db_symbol(ticker)
            ticker = to_yahoo_symbol(symbol)
        else:
            symbol = ticker
        if ticker == "naver:KOSPI":
            if market != "kr" or not is_benchmark:
                raise ValueError("naver:KOSPI is a KR benchmark only")
            data = naver_daily("KOSPI", start, end, rejections=rejections)
        elif market == "kr" and kr_source == "naver" and not is_benchmark:
            events: set[str] = set()
            yahoo(fetch, ticker, start, end, event_dates=events)
            data = naver_daily(symbol, start, end, rejections=rejections)
            for row in data:
                row["action"] = row["date"] in events
        else:
            data = (
                upbit(fetch, ticker, start, end)
                if market == "crypto"
                else yahoo(fetch, ticker, start, end)
            )
        if not data:
            raise ValueError(
                f"no bars for {ticker}; do not silently drop a requested symbol"
            )
        for row in data:
            row["symbol"] = "__benchmark__" if is_benchmark else symbol
            row["available_at"] = decision_at(
                market, date.fromisoformat(row["date"])
            ).isoformat()
        rows += data
        coverage.append(
            {
                "ticker": ticker,
                "rows": len(data),
                "first": data[0]["date"],
                "last": data[-1]["date"],
            }
        )
    now = datetime.now(UTC)
    if any(pd.Timestamp(row["available_at"]) > now for row in rows):
        raise ValueError("forming/future daily bar refused")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    write_json(
        path.with_suffix(".json"),
        {
            "schema": "stock-selection-bars/v1",
            "market": market,
            "sha256": digest(path),
            "fetched_at": now.isoformat(),
            "coverage": coverage,
            "rejected_bars": rejections,
            "universe": "explicit requested symbols; survivor-biased diagnostic, not historical listing membership",
            "adjustment": "Upbit unadjusted"
            if market == "crypto"
            else f"{kr_source if market == 'kr' else 'yahoo'} current-vintage historical prices; Yahoo dividend/split-crossing labels unknown; no certified PIT vintage",
            "equity_price_source": kr_source
            if market == "kr"
            else "yahoo"
            if market == "us"
            else None,
            "availability": "nominal completed-session availability; retrospective snapshot, not a contemporaneous vintage",
            "traded_value_basis": "actual quote turnover"
            if market == "crypto"
            else "close*volume estimate; not observed exchange turnover",
        },
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_input_args(parser)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--symbols", help="comma-separated provider symbols")
    choice.add_argument(
        "--symbols-file",
        type=Path,
        help="one provider symbol per line; may enumerate the full universe",
    )
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--kr-source", choices=("yahoo", "naver"), default="yahoo")
    parser.add_argument(
        "--benchmark", help="explicit provider index/proxy; recorded in coverage"
    )
    parser.add_argument(
        "--end", type=date.fromisoformat, required=True, help="exclusive"
    )
    args = parser.parse_args()
    tickers = (
        args.symbols.split(",")
        if args.symbols
        else args.symbols_file.read_text().splitlines()
    )
    print(
        fetch_snapshot(
            args.cache_dir,
            args.market,
            tickers,
            args.start,
            args.end,
            benchmark=args.benchmark,
            kr_source=args.kr_source,
        )
    )


if __name__ == "__main__":
    main()
