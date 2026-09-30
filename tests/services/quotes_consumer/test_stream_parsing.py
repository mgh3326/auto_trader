"""A6 + field-handling: session pass-through, drops, tick typing."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.quotes_consumer.stream import parse_quote_entry
from app.services.quotes_consumer.types import DroppedEntry, QuoteTick

pytestmark = pytest.mark.unit


def _fields(**over) -> dict[str, str]:
    base = {
        "symbol": "005930",
        "ts": "2026-09-30T13:15:02.123+09:00",
        "price": "71500",
        "bid1": "",
        "bid_qty": "",
        "ask1": "",
        "ask_qty": "",
        "session": "krx_regular",
    }
    base.update(over)
    return base


def test_trade_tick_parses_with_empty_orderbook_fields() -> None:
    tick = parse_quote_entry("1-1", _fields())
    assert isinstance(tick, QuoteTick)
    assert tick.kind == "trade"
    assert tick.price == Decimal("71500")
    assert tick.bid1 is None and tick.ask1 is None
    assert tick.bid_qty is None and tick.ask_qty is None
    assert tick.session == "krx_regular"
    assert tick.market == "kr"
    assert tick.source_symbol == "005930"
    assert tick.symbol == "005930"


def test_orderbook_tick_parses_with_empty_price() -> None:
    tick = parse_quote_entry(
        "1-2",
        _fields(
            price="",
            bid1="71400",
            bid_qty="120",
            ask1="71600",
            ask_qty="90",
        ),
    )
    assert isinstance(tick, QuoteTick)
    assert tick.kind == "orderbook"
    assert tick.price is None
    assert tick.bid1 == Decimal("71400")
    assert tick.ask1 == Decimal("71600")


@pytest.mark.parametrize(
    "session,market",
    [
        ("nxt_pre", "kr"),
        ("krx_regular", "kr"),
        ("nxt_after", "kr"),
        ("us_pre", "us"),
        ("us_regular", "us"),
        ("us_after", "us"),
    ],
)
def test_every_contract_session_passes_through_verbatim(
    session: str, market: str
) -> None:
    tick = parse_quote_entry("1-3", _fields(session=session))
    assert isinstance(tick, QuoteTick)
    assert tick.session == session
    assert tick.market == market


def test_unknown_session_is_dropped_not_coerced() -> None:
    dropped = parse_quote_entry("1-4", _fields(session="nxt_unknown"))
    assert isinstance(dropped, DroppedEntry)
    assert dropped.reason == "unknown_session"


def test_near_miss_session_label_is_dropped() -> None:
    for label in ("KRX_REGULAR", "krx regular", "regular", "", "nxt_after "):
        dropped = parse_quote_entry("1-5", _fields(session=label))
        assert isinstance(dropped, DroppedEntry)
        assert dropped.reason == "unknown_session"


def test_fully_empty_tick_is_dropped() -> None:
    dropped = parse_quote_entry(
        "1-6", _fields(price="", bid1="", bid_qty="", ask1="", ask_qty="")
    )
    assert isinstance(dropped, DroppedEntry)
    assert dropped.reason == "empty_tick"


def test_bad_ts_is_dropped() -> None:
    for ts in ("not-a-time", "2026-09-30 13:15:02", ""):
        dropped = parse_quote_entry("1-7", _fields(ts=ts))
        assert isinstance(dropped, DroppedEntry)
        assert dropped.reason == "bad_ts"


def test_bad_number_is_dropped() -> None:
    dropped = parse_quote_entry("1-8", _fields(price="abc"))
    assert isinstance(dropped, DroppedEntry)
    assert dropped.reason == "bad_number"


def test_empty_symbol_is_dropped() -> None:
    dropped = parse_quote_entry("1-9", _fields(symbol=" "))
    assert isinstance(dropped, DroppedEntry)
    assert dropped.reason == "bad_symbol"


def test_us_symbol_is_normalized_via_symbol_module() -> None:
    """Toss may spell BRK-B/BRK/B; the DB join key is always BRK.B."""
    tick = parse_quote_entry(
        "1-10", _fields(symbol="BRK-B", session="us_regular", price="450.25")
    )
    assert isinstance(tick, QuoteTick)
    assert tick.symbol == "BRK.B"
    assert tick.source_symbol == "BRK-B"
