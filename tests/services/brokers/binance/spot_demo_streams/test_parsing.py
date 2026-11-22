import json

import pytest

from app.services.brokers.binance.spot_demo_streams.parsing import (
    FrameError,
    decode_json,
    parse_market_frame,
    parse_user_push,
)
from tests.services.brokers.binance.spot_demo_streams import fixtures as fx


def test_depth_keeps_exact_decimal_strings() -> None:
    symbol, rec = parse_market_frame(decode_json(fx.DEPTH))
    assert symbol == "BTCUSDT"
    assert rec["kind"] == "depth" and rec["last_update_id"] == 160
    assert rec["bids"][0] == ["67000.01000000", "1.50000000"]
    assert all(isinstance(x, str) for lvl in rec["asks"] for x in lvl)


def test_book_ticker_and_trade() -> None:
    _, bt = parse_market_frame(decode_json(fx.BOOK_TICKER))
    assert bt["bid"] == "67000.01000000" and bt["ask_qty"] == "0.75000000"
    _, tr = parse_market_frame(decode_json(fx.AGG_TRADE))
    assert tr["price"] == "67000.02000000" and tr["buyer_is_maker"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o["data"].__setitem__("bids", [["x", "1"]]),
        lambda o: o["data"].__setitem__("bids", [["-1", "1"]]),
        lambda o: o["data"].__setitem__("bids", [["NaN", "1"]]),
        lambda o: o["data"].__setitem__("lastUpdateId", "160"),
        lambda o: o.__setitem__("stream", "btcusdt@kline_1m"),
        lambda o: o.__setitem__("stream", "nodelimiter"),
        lambda o: o.__setitem__("data", []),
    ],
)
def test_malformed_depth_rejected(mutate) -> None:
    obj = json.loads(fx.DEPTH)
    mutate(obj)
    with pytest.raises(FrameError):
        parse_market_frame(obj)


def test_book_ticker_symbol_must_match_stream() -> None:
    obj = json.loads(fx.BOOK_TICKER)
    obj["data"]["s"] = "ETHUSDT"
    with pytest.raises(FrameError):
        parse_market_frame(obj)


def test_execution_report_normalized() -> None:
    rec = parse_user_push(decode_json(fx.EXECUTION_REPORT))
    assert rec is not None
    assert rec["kind"] == "execution_report"
    assert (
        rec["order_status"] == "PARTIALLY_FILLED"
        and rec["cumulative_qty"] == "0.00400000"
    )
    assert rec["client_order_id"] == "mock-abc-1" and rec["trade_id"] == 999


def test_account_position_and_ignored_events() -> None:
    rec = parse_user_push(decode_json(fx.ACCOUNT_POSITION))
    assert rec is not None and rec["balances"][0]["asset"] == "USDT"
    assert parse_user_push(decode_json(fx.LIST_STATUS)) is None


def test_non_json_rejected() -> None:
    with pytest.raises(FrameError):
        decode_json("not json")
    with pytest.raises(FrameError):
        decode_json("[1]")
