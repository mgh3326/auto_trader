"""ROB-317 — futures stream parsing + URL builder + host guard."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    AggTradeEvent,
    build_futures_stream_url,
    parse_futures_message,
)
from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent

_NOW = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.UTC)


def _wrap(stream: str, data: dict) -> str:
    return json.dumps({"stream": stream, "data": data})


def test_parse_agg_trade() -> None:
    raw = _wrap(
        "xrpusdt@aggTrade",
        {
            "e": "aggTrade",
            "s": "XRPUSDT",
            "p": "0.5123",
            "q": "100",
            "T": 1716724800000,
            "m": True,
        },
    )
    ev = parse_futures_message(raw, now=_NOW)
    assert isinstance(ev, AggTradeEvent)
    assert ev.symbol == "XRPUSDT"
    assert ev.price == Decimal("0.5123")
    assert ev.is_buyer_maker is True


def test_parse_book_ticker_futures_has_e_field() -> None:
    raw = _wrap(
        "xrpusdt@bookTicker",
        {
            "e": "bookTicker",
            "u": 400900217,
            "s": "XRPUSDT",
            "b": "0.5120",
            "B": "31.2",
            "a": "0.5125",
            "A": "40.6",
        },
    )
    ev = parse_futures_message(raw, now=_NOW)
    assert isinstance(ev, BookTickerEvent)
    assert ev.bid_price == Decimal("0.5120")
    assert ev.ask_price == Decimal("0.5125")
    assert ev.received_at == _NOW


def test_parse_closed_kline() -> None:
    raw = _wrap(
        "xrpusdt@kline_1m",
        {
            "e": "kline",
            "s": "XRPUSDT",
            "k": {
                "t": 1716724740000,
                "T": 1716724799999,
                "s": "XRPUSDT",
                "i": "1m",
                "o": "0.50",
                "h": "0.52",
                "l": "0.49",
                "c": "0.515",
                "v": "1000",
                "q": "515",
                "n": 42,
                "x": True,
            },
        },
    )
    ev = parse_futures_message(raw, now=_NOW)
    assert isinstance(ev, KlineEvent)
    assert ev.is_closed is True
    assert ev.close == Decimal("0.515")


def test_parse_drops_in_progress_kline() -> None:
    raw = _wrap(
        "xrpusdt@kline_1m",
        {
            "e": "kline",
            "s": "XRPUSDT",
            "k": {
                "t": 1716724740000,
                "T": 1716724799999,
                "s": "XRPUSDT",
                "i": "1m",
                "o": "0.50",
                "h": "0.52",
                "l": "0.49",
                "c": "0.515",
                "v": "1000",
                "q": "515",
                "n": 42,
                "x": False,
            },
        },
    )
    assert parse_futures_message(raw, now=_NOW) is None


def test_parse_ignores_garbage_and_unknown() -> None:
    assert parse_futures_message("not json", now=_NOW) is None
    assert (
        parse_futures_message(_wrap("x@depth", {"e": "depthUpdate"}), now=_NOW) is None
    )


def test_build_url_combines_streams_for_allowlisted_host() -> None:
    url = build_futures_stream_url(
        ["XRPUSDT", "DOGEUSDT"],
        streams=("aggTrade", "bookTicker", "kline_1m"),
        base_url="wss://fstream.binance.com",
    )
    assert url.startswith("wss://fstream.binance.com/stream?streams=")
    assert "xrpusdt@aggTrade" in url
    assert "dogeusdt@kline_1m" in url


def test_build_url_rejects_non_fstream_host() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        build_futures_stream_url(
            ["XRPUSDT"], streams=("aggTrade",), base_url="wss://fapi.binance.com"
        )
