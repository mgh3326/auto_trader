"""Pure KIS quote-frame parser tests (ROB-321 PR2 Task 1)."""

from __future__ import annotations

import pytest

from app.services.brokers.kis.mock_scalping_ws.quote_parsers import (
    OrderBookSnapshot,
    QuoteTick,
    parse_quote_frame,
)
from app.services.brokers.kis.mock_scalping_ws.quote_protocol import (
    DOMESTIC_ORDERBOOK_TR,
    DOMESTIC_TRADE_TR,
)


def _trade_frame(
    symbol: str = "005930", time: str = "131502", price: str = "70500"
) -> str:
    # H0STCNT0: idx0 symbol, idx1 time(HHMMSS), idx2 last price. Pad to 20 fields.
    fields = [""] * 20
    fields[0] = symbol
    fields[1] = time
    fields[2] = price
    return f"0|{DOMESTIC_TRADE_TR}|001|" + "^".join(fields)


def _orderbook_frame(
    symbol: str = "005930",
    ask1: str = "70600",
    bid1: str = "70500",
    ask_qty1: str = "120",
    bid_qty1: str = "200",
) -> str:
    # H0STASP0: idx0 symbol, idx3 ASKP1, idx13 BIDP1, idx23 ASKP_RSQN1, idx33 BIDP_RSQN1.
    fields = [""] * 60
    fields[0] = symbol
    fields[3] = ask1
    fields[13] = bid1
    fields[23] = ask_qty1
    fields[33] = bid_qty1
    return f"0|{DOMESTIC_ORDERBOOK_TR}|001|" + "^".join(fields)


@pytest.mark.unit
def test_parse_trade_frame() -> None:
    result = parse_quote_frame(_trade_frame(price="70500"))
    assert isinstance(result, QuoteTick)
    assert result.symbol == "005930"
    assert result.last_price == 70500.0
    assert result.ts == "131502"


@pytest.mark.unit
def test_parse_orderbook_frame() -> None:
    result = parse_quote_frame(_orderbook_frame())
    assert isinstance(result, OrderBookSnapshot)
    assert result.symbol == "005930"
    assert result.ask == 70600.0
    assert result.bid == 70500.0
    assert result.ask_qty == 120.0
    assert result.bid_qty == 200.0


@pytest.mark.unit
def test_encrypted_frame_rejected() -> None:
    # Leading "1" = encrypted; quotes are never encrypted -> reject (return None).
    enc = _trade_frame().replace("0|", "1|", 1)
    assert parse_quote_frame(enc) is None


@pytest.mark.unit
def test_unknown_tr_returns_none() -> None:
    assert parse_quote_frame("0|H0STCNI0|001|005930^x^y") is None


@pytest.mark.unit
def test_malformed_frame_returns_none() -> None:
    assert parse_quote_frame("garbage") is None
    assert parse_quote_frame("") is None
    assert parse_quote_frame("0|H0STCNT0") is None
