"""Wire-shape contract: raw Binance JSON text through the real client parsing layer.

The other trend tests script Python values, so they cannot notice a field that
arrives as a string, an absent field, or a differently spelled flag. Here every
response body is JSON *text* (``wire_fixtures``), wrapped in a real
``httpx.Response`` and parsed by the same code the executor uses. No network.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal as D

import httpx
import pytest

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.demo.quantity import venue_order_quantity
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoPositionSideMismatch,
)
from app.services.brokers.binance.trend.constants import DEMO_URL
from app.services.brokers.binance.trend.executor import (
    TrendBrokerTruthUnavailable,
    TrendDemoClient,
    TrendExecutor,
    affirmative_rejection,
)
from app.services.brokers.binance.trend.history import (
    collect_hard_stop_minutes,
    collect_signal_bars,
)
from app.services.brokers.binance.trend.state import TrendIntentSnapshot
from app.services.brokers.binance.trend.strategy import DAY_MS, bar_close_text
from research.nautilus_scalping.rob974_features import MINUTE_MS
from tests.services.brokers.binance.trend import wire_fixtures as wire

pytestmark = pytest.mark.unit

_REQUEST = httpx.Request("GET", DEMO_URL + "/fapi")


def response(raw: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=raw.encode(), request=_REQUEST)


class RawTransport:
    """Serves JSON text; ``routes`` values are text or ``(params) -> text``."""

    base_url = DEMO_URL

    def __init__(self, routes=None, *, post=None, post_status=200):
        self.routes = routes or {}
        self.post_raw = post
        self.post_status = post_status
        self.posted: list[dict] = []

    async def get(self, path, *, params=None):
        route = self.routes[path]
        raw = route(params or {}) if callable(route) else route
        return response(raw)

    async def post(self, path, *, params):
        self.posted.append(dict(params))
        return response(self.post_raw, self.post_status)


def client_for(transport: RawTransport) -> TrendDemoClient:
    client = TrendDemoClient.__new__(TrendDemoClient)
    client._client = transport
    client._base_url = DEMO_URL
    client._api_key = "k"
    client._api_secret = "s"
    return client


def run(coro):
    return asyncio.run(coro)


SEND = {
    "symbol": "BTCUSDT",
    "side": "BUY",
    "order_type": "MARKET",
    "qty": D("0.6"),
    "client_order_id": "trend-abc",
    "reduce_only": False,
    "position_side": "BOTH",
    "confirm": True,
}


def intent(**changes) -> TrendIntentSnapshot:
    values = {
        "client_order_id": "trend-abc",
        "signal_key": "k",
        "leg_key": "entry",
        "side": "BUY",
        "qty": D("0.600000000000"),  # a DB-scale value, as the state returns it
        "reduce_only": False,
        "state": "sending",
        "broker_order_id": None,
        "broker_status": None,
        "executed_qty": D(0),
    }
    values.update(changes)
    return TrendIntentSnapshot(**values)


def echoes(result, **changes) -> bool:
    return TrendExecutor._echo_matches(  # type: ignore[arg-type]
        object(), result, intent=intent(**changes), symbol="BTCUSDT"
    )


# --------------------------------------------------------------- submit


@pytest.mark.parametrize(
    "raw",
    [wire.NEW_ORDER_NEW, wire.NEW_ORDER_FILLED],
    ids=["new", "filled"],
)
def test_a_documented_submit_response_parses_and_echoes_the_intent(
    monkeypatch, raw
) -> None:
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    transport = RawTransport(post=raw)
    result = run(client_for(transport).submit_order(**SEND))
    assert transport.posted[0]["positionSide"] == "BOTH"
    assert (result.symbol, result.side, result.position_side) == (
        "BTCUSDT",
        "BUY",
        "BOTH",
    )
    assert result.broker_order_id == "22542179"  # an integer on the wire
    assert result.qty == D("0.6") and result.reduce_only is False
    assert echoes(result)  # DB-scale 0.600000000000 equals wire "0.600"


def test_a_reduce_only_close_response_echoes_a_close_intent(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    transport = RawTransport(post=wire.NEW_ORDER_CLOSE)
    result = run(
        client_for(transport).submit_order(
            **{
                **SEND,
                "side": "SELL",
                "reduce_only": True,
                "client_order_id": "trend-close",
            }
        )
    )
    assert transport.posted[0]["reduceOnly"] == "true"
    assert echoes(result, client_order_id="trend-close", side="SELL", reduce_only=True)
    assert not echoes(
        result, client_order_id="trend-close", side="SELL"
    )  # reduceOnly differs


def test_a_response_without_position_side_is_never_read_as_agreement(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    transport = RawTransport(post=wire.NEW_ORDER_NO_POSITION_SIDE)
    with pytest.raises(BinanceFuturesDemoPositionSideMismatch):
        run(client_for(transport).submit_order(**SEND))
    assert len(transport.posted) == 1  # sent once; the executor makes it uncertain


@pytest.mark.parametrize(
    ("raw", "status", "expected"),
    [
        (wire.ERROR_MARGIN_INSUFFICIENT, 400, "http_400:-2019"),
        (wire.ERROR_REDUCE_ONLY, 400, "http_400:-2022"),
        (wire.ERROR_SEND_STATUS_UNKNOWN, 503, None),
        (wire.ERROR_TOO_MANY_REQUESTS, 429, None),
        ("<html>Bad Gateway</html>", 502, None),
        ("", 400, None),
    ],
)
def test_real_http_errors_classify_only_an_explicit_rejection(
    monkeypatch, raw, status, expected
) -> None:
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    transport = RawTransport(post=raw, post_status=status)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        run(client_for(transport).submit_order(**SEND))
    assert affirmative_rejection(caught.value) == expected


# ---------------------------------------------------------------- query


def test_a_queried_order_parses_and_keeps_the_broker_echo() -> None:
    transport = RawTransport({"/fapi/v1/order": wire.QUERY_ORDER_FILLED})
    order = run(
        client_for(transport).get_order(symbol="BTCUSDT", client_order_id="trend-abc")
    )
    assert order.status == "FILLED"
    assert (order.orig_qty, order.executed_qty, order.avg_price) == (
        D("0.600"),
        D("0.600"),
        D("99874.50000"),
    )
    assert order.position_side == "BOTH" and order.reduce_only is False
    assert order.raw_response_redacted["updateTime"] == 1766000000123  # an int


def test_a_partially_filled_query_parses() -> None:
    transport = RawTransport({"/fapi/v1/order": wire.QUERY_ORDER_PARTIAL})
    order = run(
        client_for(transport).get_order(symbol="BTCUSDT", client_order_id="trend-abc")
    )
    assert (order.status, order.executed_qty) == ("PARTIALLY_FILLED", D("0.300"))


def test_an_unknown_order_is_the_not_found_error_and_nothing_else() -> None:
    class NotFound(RawTransport):
        async def get(self, path, *, params=None):
            return response(wire.ERROR_ORDER_NOT_FOUND, 400)

    with pytest.raises(BinanceDemoOrderNotFound):
        run(
            client_for(NotFound()).get_order(
                symbol="BTCUSDT", client_order_id="trend-abc"
            )
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.replace('"clientOrderId": "trend-abc"', '"clientOrderId": "other"'),
        lambda b: b.replace('"symbol": "BTCUSDT"', '"symbol": "ETHUSDT"'),
        lambda b: b.replace('"reduceOnly": false', '"reduceOnly": "false"'),
        lambda b: b.replace('  "reduceOnly": false,\n', ""),
    ],
    ids=["client-id", "symbol", "reduceOnly-string", "reduceOnly-absent"],
)
def test_a_query_response_that_does_not_echo_the_identity_is_refused(mutation) -> None:
    body = mutation(wire.QUERY_ORDER_FILLED)
    assert body != wire.QUERY_ORDER_FILLED
    transport = RawTransport({"/fapi/v1/order": body})
    with pytest.raises(TrendBrokerTruthUnavailable):
        run(
            client_for(transport).get_order(
                symbol="BTCUSDT", client_order_id="trend-abc"
            )
        )


# ------------------------------------------------------------ positions


def _position_risk(raw: str):
    def route(params):
        rows = json.loads(raw)
        symbol = params.get("symbol")
        return json.dumps([r for r in rows if symbol in (None, r["symbol"])])

    return route


def test_the_full_position_risk_array_parses_with_signed_string_amounts() -> None:
    transport = RawTransport({"/fapi/v2/positionRisk": wire.POSITION_RISK_ALL})
    rows = {p.symbol: p for p in run(client_for(transport).get_all_positions())}
    assert rows["BTCUSDT"].position_amt == D("0.600")
    assert rows["SOLUSDT"].position_amt == D("-12.5")
    assert rows["ETHUSDT"].is_flat and rows["XRPUSDT"].is_flat
    assert {p.leverage for p in rows.values()} == {1}
    assert {p.position_side for p in rows.values()} == {"BOTH"}


def test_a_hedge_mode_account_is_refused_as_incomplete_one_way_truth() -> None:
    transport = RawTransport({"/fapi/v2/positionRisk": wire.POSITION_RISK_HEDGE})
    with pytest.raises(TrendBrokerTruthUnavailable):
        run(client_for(transport).get_all_positions())


@pytest.mark.parametrize(
    ("symbol", "ok"),
    [("BTCUSDT", True), ("ETHUSDT", True), ("SOLUSDT", True), ("XRPUSDT", False)],
)
def test_isolated_1x_is_read_from_the_v2_position_risk_text(symbol, ok) -> None:
    transport = RawTransport(
        {"/fapi/v2/positionRisk": _position_risk(wire.POSITION_RISK_ALL)}
    )
    client = client_for(transport)
    if ok:
        run(client.assert_symbol_isolated_1x(symbol))
    else:  # XRPUSDT is "cross" in the fixture
        with pytest.raises(TrendBrokerTruthUnavailable):
            run(client.assert_symbol_isolated_1x(symbol))


@pytest.mark.parametrize("spelling", ["ISOLATED", "Isolated", "isolated"])
def test_the_margin_type_spelling_is_not_case_sensitive(spelling) -> None:
    body = wire.POSITION_RISK_ALL.replace(
        '"marginType": "isolated"', f'"marginType": "{spelling}"'
    )
    transport = RawTransport({"/fapi/v2/positionRisk": _position_risk(body)})
    run(client_for(transport).assert_symbol_isolated_1x("BTCUSDT"))


def test_a_leverage_other_than_one_is_refused_even_as_a_number() -> None:
    body = wire.POSITION_RISK_ALL.replace('"leverage": "1"', '"leverage": 3')
    transport = RawTransport({"/fapi/v2/positionRisk": _position_risk(body)})
    with pytest.raises(TrendBrokerTruthUnavailable):
        run(client_for(transport).assert_symbol_isolated_1x("BTCUSDT"))


# ---------------------------------------------------------- position mode


@pytest.mark.parametrize(
    ("raw", "hedge"),
    [
        (wire.POSITION_MODE_ONE_WAY, False),
        (wire.POSITION_MODE_HEDGE, True),
        (wire.POSITION_MODE_ONE_WAY_STRING, False),
        (wire.POSITION_MODE_HEDGE_STRING, True),
    ],
)
def test_position_mode_text_in_both_spellings(raw, hedge) -> None:
    transport = RawTransport({"/fapi/v1/positionSide/dual": raw})
    assert run(client_for(transport).get_position_mode()).is_hedge_mode is hedge


@pytest.mark.parametrize(
    "raw", ["{}", "[]", "null", '{"dualSidePosition": null}', '{"dualSidePosition": 1}']
)
def test_position_mode_text_that_is_not_a_flag_is_refused(raw) -> None:
    transport = RawTransport({"/fapi/v1/positionSide/dual": raw})
    with pytest.raises(TrendBrokerTruthUnavailable):
        run(client_for(transport).get_position_mode())


# ------------------------------------------------------------ open orders


def test_open_orders_text_parses_including_the_empty_array() -> None:
    one = run(
        client_for(
            RawTransport({"/fapi/v1/openOrders": wire.OPEN_ORDERS_ONE})
        ).get_all_open_orders()
    )
    assert [(o.symbol, o.client_order_id, o.broker_order_id) for o in one.orders] == [
        ("BTCUSDT", "web_7aQ2", "1917641")
    ]
    assert one.orders[0].qty == D("0.40") and one.orders[0].reduce_only is False
    none = run(
        client_for(
            RawTransport({"/fapi/v1/openOrders": wire.OPEN_ORDERS_NONE})
        ).get_all_open_orders()
    )
    assert none.orders == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.replace('"reduceOnly": false', '"reduceOnly": "false"'),
        lambda b: b.replace('"positionSide": "BOTH"', '"positionSide": "LONG"'),
        lambda b: b.replace('  "orderId": 1917641,\n', ""),
        lambda b: b.replace('"origQty": "0.40"', '"origQty": "0"'),
    ],
    ids=["reduceOnly-string", "hedge-side", "no-order-id", "zero-qty"],
)
def test_open_orders_text_with_an_unreadable_field_is_refused(mutation) -> None:
    body = mutation(wire.OPEN_ORDERS_ONE)
    assert body != wire.OPEN_ORDERS_ONE
    with pytest.raises(TrendBrokerTruthUnavailable):
        run(
            client_for(
                RawTransport({"/fapi/v1/openOrders": body})
            ).get_all_open_orders()
        )


# ----------------------------------------------- exchange info and quotes


def test_exchange_info_text_gives_the_market_lot_filters_as_decimals() -> None:
    transport = RawTransport({"/fapi/v1/exchangeInfo": wire.EXCHANGE_INFO_BTC})
    filters = run(client_for(transport).get_trend_filters("BTCUSDT"))
    assert (filters.step_size, filters.min_qty, filters.max_qty) == (
        D("0.001"),
        D("0.001"),
        D("120"),  # MARKET_LOT_SIZE, not LOT_SIZE's 1000
    )
    assert (filters.min_notional_usdt, filters.quantity_precision) == (D("100"), 3)


def test_a_zero_market_min_qty_in_the_documented_text_is_valid() -> None:
    assert wire.EXCHANGE_INFO_ZERO_MARKET_MIN != wire.EXCHANGE_INFO_BTC
    transport = RawTransport(
        {"/fapi/v1/exchangeInfo": wire.EXCHANGE_INFO_ZERO_MARKET_MIN}
    )
    filters = run(client_for(transport).get_trend_filters("BTCUSDT"))
    assert filters.min_qty == D("0.001")  # LOT_SIZE.minQty


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.replace('"status": "TRADING"', '"status": "BREAK"'),
        lambda b: b.replace('"quantityPrecision": 3', '"quantityPrecision": "3"'),
        lambda b: b.replace('{"filterType": "MIN_NOTIONAL", "notional": "100"},', ""),
    ],
    ids=["not-trading", "precision-string", "no-notional"],
)
def test_exchange_info_text_that_cannot_size_an_order_is_refused(mutation) -> None:
    body = mutation(wire.EXCHANGE_INFO_BTC)
    assert body != wire.EXCHANGE_INFO_BTC
    with pytest.raises(TrendBrokerTruthUnavailable):
        run(
            client_for(RawTransport({"/fapi/v1/exchangeInfo": body})).get_trend_filters(
                "BTCUSDT"
            )
        )


def test_book_ticker_text_with_extra_fields_gives_the_executable_sides() -> None:
    transport = RawTransport({"/fapi/v1/ticker/bookTicker": wire.BOOK_TICKER_BTC})
    quote = run(client_for(transport).get_book_quote("BTCUSDT"))
    assert (quote.executable_price("BUY"), quote.executable_price("SELL")) == (
        D("99890.20"),
        D("99890.10"),
    )


# ---------------------------------------------------------------- klines

T = 1_000 * DAY_MS


def _klines_route(params):
    start, end = params["startTime"], params["endTime"]
    rows = [
        (ts, "100.5") for ts in range(start, end + 1, MINUTE_MS) if start <= ts <= end
    ]
    return wire.klines(rows)


def test_kline_text_keeps_the_exchange_close_text_for_the_signal_key() -> None:
    client = client_for(RawTransport({"/fapi/v1/klines": _klines_route}))
    bars = run(
        collect_signal_bars(
            client.kline_transport(),
            "BTCUSDT",
            decision_ts=T,
            lookback_days=20,
            check=client._assert_trend,
        )
    )
    assert len(bars) == 21
    assert {bar_close_text(b) for b in bars} == {"100.5"}  # text, never a float repr


def test_kline_text_feeds_a_continuous_hard_stop_window() -> None:
    client = client_for(RawTransport({"/fapi/v1/klines": _klines_route}))
    minutes = run(
        collect_hard_stop_minutes(
            client.kline_transport(),
            "BTCUSDT",
            entered_ms=T - 3 * 60 * MINUTE_MS,
            now_ms=T,
            check=client._assert_trend,
        )
    )
    assert len(minutes) == 3 * 60 and minutes[0].ts == T - 3 * 60 * MINUTE_MS


def test_a_db_scale_quantity_goes_on_the_wire_without_storage_zeros(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    transport = RawTransport(post=wire.NEW_ORDER_NEW)
    run(
        client_for(transport).submit_order(
            **{**SEND, "qty": venue_order_quantity(D("0.600000000000"))}
        )
    )
    assert transport.posted[0]["quantity"] == "0.6"
