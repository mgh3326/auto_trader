"""Network-free public-source fixtures for the data-plane tests.

``FakeSources`` answers every allowlisted URL the collectors build with a
synthetic body shaped like the real one (KIS master zip, NASDAQ Trader files,
Binance exchangeInfo/klines, Upbit market list/candles, Yahoo chart v8) and
counts the requests, so a test can assert that a second run fetched nothing.
"""

from __future__ import annotations

import io
import json
import urllib.parse
import zipfile
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from research.crypto_universe.public_http import FetchResponse
from research.data_plane.calendars import partition_days
from research.data_plane.contract import market_spec
from research.data_plane.public_http import check_url
from tests.research.us_universe.us_universe_fixtures import nasdaq_body, other_body

KR_KOSPI = {"005930": "SAMSUNG ELEC", "000660": "SK HYNIX"}
KR_KOSDAQ = {"247540": "ECOPRO BM"}
US_SYMBOLS = ("AAPL", "KO")
BINANCE_PAIRS = {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING", "LUNAUSDT": "BREAK"}
UPBIT_MARKETS = ("KRW-BTC", "KRW-ETH")


def kis_master_zip(names: dict[str, str], suffix_length: int, member: str) -> bytes:
    lines = [
        f"{sym:<9}{'K' * 12}{name}" + "0" * suffix_length for sym, name in names.items()
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, "\n".join(lines).encode("cp949"))
    return buf.getvalue()


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


class FakeSources:
    def __init__(
        self,
        *,
        binance_pairs: dict[str, str] | None = None,
        missing: frozenset[str] = frozenset(),
        future_bar: bool = False,
    ) -> None:
        self.binance_pairs = dict(binance_pairs or BINANCE_PAIRS)
        self.missing = missing  # instruments answered with HTTP 404
        self.future_bar = future_bar
        self.calls: Counter[str] = Counter()

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    def __call__(self, url: str) -> FetchResponse:
        check_url(url)  # fixtures must only answer URLs the real fetcher would allow
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.calls[parts.hostname or ""] += 1
        host, path = parts.hostname, parts.path
        if host == "new.real.download.dws.co.kr":
            if path.endswith("kospi_code.mst.zip"):
                return FetchResponse(
                    200, kis_master_zip(KR_KOSPI, 228, "kospi_code.mst")
                )
            return FetchResponse(200, kis_master_zip(KR_KOSDAQ, 222, "kosdaq_code.mst"))
        if host == "www.nasdaqtrader.com":
            day = date(2026, 10, 30)
            if path.endswith("nasdaqlisted.txt"):
                return FetchResponse(200, nasdaq_body(day, ("AAPL",)).encode())
            return FetchResponse(200, other_body(day, ("KO",)).encode())
        if host == "data-api.binance.vision" and path == "/api/v3/exchangeInfo":
            symbols = [
                {"symbol": s, "status": st, "baseAsset": s[:-4], "quoteAsset": "USDT"}
                for s, st in self.binance_pairs.items()
            ]
            symbols.append(
                {
                    "symbol": "ETHBTC",
                    "status": "TRADING",
                    "baseAsset": "ETH",
                    "quoteAsset": "BTC",
                }
            )
            return FetchResponse(200, json.dumps({"symbols": symbols}).encode())
        if host == "api.upbit.com" and path == "/v1/market/all":
            items = [{"market": m, "english_name": m[4:]} for m in UPBIT_MARKETS]
            items.append({"market": "BTC-ETH", "english_name": "ETH"})
            return FetchResponse(200, json.dumps(items).encode())
        if host == "data-api.binance.vision" and path == "/api/v3/klines":
            if query["symbol"] in self.missing:
                return FetchResponse(400, b'{"code":-1121,"msg":"Invalid symbol."}')
            start = int(query["startTime"])
            k = [
                start,
                "100.5",
                "110",
                "99",
                "105.25",
                "12.5",
                start + 86_399_999,
                "1300.1",
                42,
                "1",
                "1",
                "0",
            ]
            return FetchResponse(200, json.dumps([k]).encode())
        if host == "api.upbit.com" and path == "/v1/candles/days":
            to = datetime.strptime(query["to"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
            candles = []
            for back in (1, 2):
                opened = to - timedelta(days=back)
                candles.append(
                    {
                        "market": query["market"],
                        "candle_date_time_utc": opened.strftime("%Y-%m-%dT%H:%M:%S"),
                        "opening_price": 1000.0,
                        "high_price": 1100.0,
                        "low_price": 990.0,
                        "trade_price": 1050.0,
                        "candle_acc_trade_volume": 3.25,
                        "candle_acc_trade_price": 3412.5,
                    }
                )
            return FetchResponse(200, json.dumps(candles).encode())
        if host == "query2.finance.yahoo.com":
            return self._yahoo(path, query)
        raise AssertionError(f"unexpected URL {url}")

    def _yahoo(self, path: str, query: dict[str, str]) -> FetchResponse:
        ysym = urllib.parse.unquote(path.rsplit("/", 1)[1])
        base = ysym.split(".")[0].split("-")[0]
        if base in self.missing or ysym in self.missing:
            body = {"chart": {"result": None, "error": {"code": "Not Found"}}}
            return FetchResponse(404, json.dumps(body).encode())
        market = "kr" if ysym.endswith((".KS", ".KQ")) else "us"
        tz = market_spec(market).tz
        start = datetime.fromtimestamp(int(query["period1"]), UTC).astimezone(tz).date()
        end = datetime.fromtimestamp(int(query["period2"]), UTC).astimezone(tz).date()
        days = partition_days(market, start, end - timedelta(days=1))
        if self.future_bar:  # only a bar for a day after the requested one
            days = [end]
        opens = [
            int(
                datetime.combine(d, datetime.min.time(), tz).replace(hour=9).timestamp()
            )
            for d in days
        ]
        n = len(days)
        result = {
            "meta": {"currency": "KRW" if market == "kr" else "USD", "symbol": ysym},
            "timestamp": opens,
            "indicators": {
                "quote": [
                    {
                        "open": [100.0] * n,
                        "high": [110.0] * n,
                        "low": [95.0] * n,
                        "close": [105.5] * n,
                        "volume": [1000] * n,
                    }
                ],
                "adjclose": [{"adjclose": [105.5] * n}],
            },
        }
        ex_day = date(2026, 10, 15)  # a fixed ex-date, as the real source keeps it
        if base == "KO" and ex_day in days:
            ex = opens[days.index(ex_day)]
            result["events"] = {"dividends": {str(ex): {"amount": 0.53, "date": ex}}}
        return FetchResponse(
            200, json.dumps({"chart": {"result": [result], "error": None}}).encode()
        )


def tree_state(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every data file under ``root`` with its bytes and mtime (ns).

    Empty ``.lock`` files (flock targets, created on first use) are not data.
    """
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != ".lock"
    }


# Market-local evening of a session (after close + 30 min) as a UTC instant.
def evening(market: str, day: date) -> datetime:
    tz = market_spec(market).tz
    hour = 1 if market == "crypto" else 18
    return (
        datetime.combine(day, datetime.min.time(), tz)
        .replace(hour=hour)
        .astimezone(UTC)
    )


__all__ = ["FakeSources", "evening", "partition_days", "tree_state"]
