"""Public-source URLs and pure parsers (no I/O here; tests feed fixture bodies).

Sources (all public, unauthenticated):

* universe — KR: KIS stock master zips (same files as ``kr_symbol_universe``,
  parsed by ``kr_delist_repair``); US: NASDAQ Trader symbol directory (parsed by
  ``us_universe``); crypto: Binance exchangeInfo and Upbit market/all (the
  ``crypto_universe`` snapshot sources).
* daily bars and dividends/splits — KR/US: Yahoo chart v8; crypto: Binance
  klines and Upbit day candles.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from research.crypto_universe.snapshot import (
    SNAPSHOT_SOURCES as CRYPTO_SNAPSHOT_SOURCES,
)
from research.kr_delist_repair.kind_parse import parse_kis_master
from research.us_universe.symdir import (
    SERIES,
    parse_symbol_directory,
)
from research.us_universe.symdir import (
    SOURCE_URLS as US_SYMDIR_URLS,
)
from research.us_universe.symdir import (
    decode_body as decode_symdir,
)

from .contract import market_spec

# Same public KIS master files as kr_delist_repair.kind_fetch (a test pins the
# equality); not imported from there because that module imports httpx.
KIS_MASTER = "https://new.real.download.dws.co.kr/common/master"
KIS_MASTER_FILES = {
    "KOSPI": ("kospi_code.mst.zip", 228),
    "KOSDAQ": ("kosdaq_code.mst.zip", 222),
}


class SourceError(ValueError):
    """A source body is missing, malformed or describes an error."""


# --------------------------------------------------------------------------
# universe


@dataclass(frozen=True)
class UniverseEntry:
    instrument: str
    venue: str
    listing_status: str
    tradeable: bool
    attrs: Mapping[str, Any]


def universe_urls(market: str) -> dict[str, str]:
    if market == "kr":
        return {
            f"kis_master_{venue.lower()}": f"{KIS_MASTER}/{name}"
            for venue, (name, _) in KIS_MASTER_FILES.items()
        }
    if market == "us":
        return {f"nasdaqtrader_{series}": US_SYMDIR_URLS[series] for series in SERIES}
    if market == "crypto":
        return dict(CRYPTO_SNAPSHOT_SOURCES)
    raise ValueError(market)


def parse_universe(market: str, bodies: Mapping[str, bytes]) -> list[UniverseEntry]:
    out: list[UniverseEntry] = []
    if market == "kr":
        for venue, (_, suffix) in KIS_MASTER_FILES.items():
            try:
                names = parse_kis_master(bodies[f"kis_master_{venue.lower()}"], suffix)
            except (ValueError, KeyError, OSError) as exc:
                raise SourceError(f"KIS master {venue}: {exc}") from exc
            out += [
                UniverseEntry(sym, venue, "listed", True, {"name": name})
                for sym, name in names.items()
            ]
    elif market == "us":
        for series in SERIES:
            try:
                obs = parse_symbol_directory(
                    series, decode_symdir(bodies[f"nasdaqtrader_{series}"])
                )
            except (ValueError, KeyError) as exc:
                raise SourceError(f"symbol directory {series}: {exc}") from exc
            out += [
                UniverseEntry(
                    row.symbol,
                    row.exchange,
                    "listed",
                    True,
                    {
                        "name": row.security_name,
                        "etf": row.etf,
                        "likely_common_stock": row.likely_common_stock,
                        "source_file_created_at": obs.created_at,
                    },
                )
                for row in obs.rows
            ]
    elif market == "crypto":
        out += _parse_binance_exchange_info(bodies["binance_spot_exchange_info"])
        out += _parse_upbit_markets(bodies["upbit_market_all"])
    else:
        raise ValueError(market)
    if not out:
        raise SourceError(f"{market}: empty universe")
    return out


def _json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SourceError(f"{what}: not JSON") from exc


def _parse_binance_exchange_info(body: bytes) -> list[UniverseEntry]:
    payload = _json(body, "binance exchangeInfo")
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list) or not symbols:
        raise SourceError("binance exchangeInfo: no symbols")
    out = []
    for item in symbols:
        if not isinstance(item, dict) or item.get("quoteAsset") != "USDT":
            continue
        status = str(item.get("status") or "")
        symbol = str(item.get("symbol") or "")
        if not symbol or not status:
            raise SourceError(f"binance exchangeInfo: malformed symbol {item!r}")
        out.append(
            UniverseEntry(
                symbol,
                "binance_usdt_spot",
                status,
                status == "TRADING",
                {"base_asset": item.get("baseAsset")},
            )
        )
    if not out:
        raise SourceError("binance exchangeInfo: no USDT pairs")
    return out


def _parse_upbit_markets(body: bytes) -> list[UniverseEntry]:
    payload = _json(body, "upbit market/all")
    if not isinstance(payload, list) or not payload:
        raise SourceError("upbit market/all: not a non-empty list")
    out = []
    for item in payload:
        market = item.get("market") if isinstance(item, dict) else None
        if not isinstance(market, str) or not market:
            raise SourceError(f"upbit market/all: malformed item {item!r}")
        if not market.startswith("KRW-"):
            continue
        event = (
            item.get("market_event")
            if isinstance(item.get("market_event"), dict)
            else {}
        )
        out.append(
            UniverseEntry(
                market,
                "upbit_krw",
                "listed",
                True,
                {
                    "name": item.get("english_name"),
                    "warning": bool(event.get("warning")) if event else None,
                },
            )
        )
    if not out:
        raise SourceError("upbit market/all: no KRW markets")
    return out


# --------------------------------------------------------------------------
# numbers


def dec(value: Any) -> str | None:
    """Exact decimal text of a source number (floats via ``repr``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise SourceError(f"boolean where a number was expected: {value!r}")
    text = repr(value) if isinstance(value, float) else str(value)
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise SourceError(f"not a number: {value!r}") from exc
    if not number.is_finite():
        raise SourceError(f"non-finite number: {value!r}")
    return text


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceError(f"not an integer volume: {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise SourceError(f"fractional volume: {value!r}")
    return int(value)


# --------------------------------------------------------------------------
# Yahoo chart (KR/US)

YAHOO_SOURCE = "yahoo_chart"
YAHOO_VERSION = "v8"
YAHOO_LOOKBACK_DAYS = 60


def yahoo_symbol(market: str, instrument: str, venue: str) -> str:
    if market == "kr":
        return f"{instrument}.{'KS' if venue == 'KOSPI' else 'KQ'}"
    if market == "us":
        from app.core.symbol import to_yahoo_symbol

        return to_yahoo_symbol(instrument)
    raise ValueError(market)


def yahoo_chart_url(
    market: str,
    instrument: str,
    venue: str,
    day: date,
    lookback_days: int = YAHOO_LOOKBACK_DAYS,
) -> str:
    tz = market_spec(market).tz
    start = datetime.combine(
        day - timedelta(days=lookback_days), datetime.min.time(), tz
    )
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
    query = urllib.parse.urlencode(
        {
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "interval": "1d",
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
    )
    path = urllib.parse.quote(yahoo_symbol(market, instrument, venue), safe="")
    return f"https://query2.finance.yahoo.com/v8/finance/chart/{path}?{query}"


@dataclass(frozen=True)
class YahooBar:
    day: date
    open: str | None
    high: str | None
    low: str | None
    close: str | None
    volume: int | None
    adj_close: str | None


@dataclass(frozen=True)
class YahooEvent:
    action_type: str  # cash_dividend | split
    ex_date: date
    event_time: datetime
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class YahooChart:
    currency: str | None
    bars: tuple[YahooBar, ...]
    events: tuple[YahooEvent, ...]


def _first_object(indicators: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    group = indicators.get(key) or [{}]
    if not isinstance(group, list) or not group or not isinstance(group[0], dict):
        raise SourceError(f"yahoo chart: indicators.{key} is malformed")
    return group[0]


def _event_items(raw_events: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    group = raw_events.get(kind) or {}
    if not isinstance(group, dict):
        raise SourceError(f"yahoo chart: events.{kind} is not an object")
    items = list(group.values())
    if not all(isinstance(i, dict) for i in items):
        raise SourceError(f"yahoo chart: malformed events.{kind} item")
    return items


def _event_time(item: Mapping[str, Any]) -> datetime:
    stamp = item.get("date")
    if not isinstance(stamp, int) or isinstance(stamp, bool):
        raise SourceError(f"yahoo chart: event without an integer date: {item!r}")
    return datetime.fromtimestamp(stamp, UTC)


def parse_yahoo_chart(market: str, body: bytes) -> YahooChart:
    tz = market_spec(market).tz
    payload = _json(body, "yahoo chart")
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise SourceError("yahoo chart: no chart object")
    if chart.get("error"):
        err = chart["error"]
        code = err.get("code") if isinstance(err, dict) else err
        raise SourceError(f"yahoo chart error: {code}")
    results = chart.get("result")
    if (
        not isinstance(results, list)
        or len(results) != 1
        or not isinstance(results[0], dict)
    ):
        raise SourceError("yahoo chart: expected exactly one result")
    result = results[0]
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    stamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    if not isinstance(stamps, list) or not isinstance(indicators, dict):
        raise SourceError("yahoo chart: malformed timestamp/indicators")
    quote = _first_object(indicators, "quote")
    adj = _first_object(indicators, "adjclose").get("adjclose") or []
    for name in ("open", "high", "low", "close", "volume"):
        if not isinstance(quote.get(name) or [], list):
            raise SourceError(f"yahoo chart: quote.{name} is not a list")
    if not isinstance(adj, list):
        raise SourceError("yahoo chart: adjclose is not a list")
    bars: dict[date, YahooBar] = {}
    for i, stamp in enumerate(stamps):
        if not isinstance(stamp, int) or isinstance(stamp, bool):
            raise SourceError(f"yahoo chart: bad timestamp {stamp!r}")

        def col(name: str, idx: int = i) -> Any:
            values = quote.get(name) or []
            return values[idx] if idx < len(values) else None

        day = datetime.fromtimestamp(stamp, UTC).astimezone(tz).date()
        if day in bars:
            raise SourceError(f"yahoo chart: two bars for {day}")
        bars[day] = YahooBar(
            day=day,
            open=dec(col("open")),
            high=dec(col("high")),
            low=dec(col("low")),
            close=dec(col("close")),
            volume=_int_or_none(col("volume")),
            adj_close=dec(adj[i]) if i < len(adj) else None,
        )
    events: list[YahooEvent] = []
    raw_events = result.get("events") if isinstance(result.get("events"), dict) else {}
    for item in _event_items(raw_events, "dividends"):
        when = _event_time(item)
        events.append(
            YahooEvent(
                "cash_dividend",
                when.astimezone(tz).date(),
                when,
                {
                    "cash_amount": dec(item.get("amount")),
                    "currency": meta.get("currency"),
                },
            )
        )
    for item in _event_items(raw_events, "splits"):
        when = _event_time(item)
        events.append(
            YahooEvent(
                "split",
                when.astimezone(tz).date(),
                when,
                {
                    "numerator": dec(item.get("numerator")),
                    "denominator": dec(item.get("denominator")),
                },
            )
        )
    events.sort(key=lambda e: (e.ex_date, e.action_type))
    return YahooChart(
        currency=meta.get("currency"),
        bars=tuple(bars[d] for d in sorted(bars)),
        events=tuple(events),
    )


# --------------------------------------------------------------------------
# crypto candles

BINANCE_SOURCE = "binance_klines"
BINANCE_VERSION = "api-v3"
UPBIT_SOURCE = "upbit_candles_days"
UPBIT_VERSION = "v1"


def candle_open_day(partition_day: date) -> date:
    """Partition ``P`` holds the candle that opened on ``P-1`` 00:00 UTC."""
    return partition_day - timedelta(days=1)


def binance_klines_url(symbol: str, partition_day: date) -> str:
    start = datetime.combine(candle_open_day(partition_day), datetime.min.time(), UTC)
    query = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": "1d",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int((start + timedelta(days=1)).timestamp() * 1000) - 1,
            "limit": 2,
        }
    )
    return f"https://data-api.binance.vision/api/v3/klines?{query}"


def parse_binance_kline(body: bytes, partition_day: date) -> dict[str, Any] | None:
    payload = _json(body, "binance klines")
    if isinstance(payload, dict):
        raise SourceError(f"binance klines error: {payload.get('code')}")
    if not isinstance(payload, list):
        raise SourceError("binance klines: not a list")
    want = int(
        datetime.combine(
            candle_open_day(partition_day), datetime.min.time(), UTC
        ).timestamp()
        * 1000
    )
    for k in payload:
        if not isinstance(k, list) or len(k) < 9:
            raise SourceError(f"binance klines: malformed kline {k!r}")
        if k[0] == want:
            return {
                "open": dec(k[1]),
                "high": dec(k[2]),
                "low": dec(k[3]),
                "close": dec(k[4]),
                "volume": dec(k[5]),
                "quote_volume": dec(k[7]),
                "trades": _int_or_none(k[8]),
            }
    return None


def upbit_candles_url(market: str, partition_day: date) -> str:
    end = datetime.combine(partition_day, datetime.min.time(), UTC)
    query = urllib.parse.urlencode(
        {"market": market, "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": 2}
    )
    return f"https://api.upbit.com/v1/candles/days?{query}"


def parse_upbit_candle(body: bytes, partition_day: date) -> dict[str, Any] | None:
    payload = _json(body, "upbit candles")
    if not isinstance(payload, list):
        raise SourceError("upbit candles: not a list")
    want = candle_open_day(partition_day).isoformat() + "T00:00:00"
    for c in payload:
        if not isinstance(c, dict):
            raise SourceError(f"upbit candles: malformed candle {c!r}")
        if c.get("candle_date_time_utc") == want:
            return {
                "open": dec(c.get("opening_price")),
                "high": dec(c.get("high_price")),
                "low": dec(c.get("low_price")),
                "close": dec(c.get("trade_price")),
                "volume": dec(c.get("candle_acc_trade_volume")),
                "quote_volume": dec(c.get("candle_acc_trade_price")),
                "trades": None,
            }
    return None
