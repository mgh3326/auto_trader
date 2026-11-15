"""TrendDemoClient gates/reads and the bounded kline collectors on fake transports."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D

import pytest

from app.services.brokers.binance.trend.constants import DEMO_URL
from app.services.brokers.binance.trend.executor import (
    TrendBrokerTruthUnavailable,
    TrendDemoClient,
)
from app.services.brokers.binance.trend.history import (
    TrendKlineGap,
    collect_hard_stop_minutes,
    collect_signal_bars,
    minute_high,
    minute_low,
)
from app.services.brokers.binance.trend.strategy import DAY_MS, bar_close_text
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS, MINUTE_MS

pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class _Transport:
    base_url = DEMO_URL

    def __init__(self, routes=None, klines=None):
        self.routes = routes or {}
        self.klines = klines
        self.posts: list[dict] = []
        self.gets: list[tuple[str, dict]] = []

    async def post(self, path, *, params):
        self.posts.append({"path": path, **params})
        return _Response(
            {
                "clientOrderId": params["newClientOrderId"],
                "orderId": 42,
                "symbol": params["symbol"],
                "side": params["side"],
                "type": "MARKET",
                "origQty": params["quantity"],
                "executedQty": "0",
                "avgPrice": "0",
                "status": "NEW",
                "reduceOnly": params.get("reduceOnly", "false") == "true",
                "positionSide": params["positionSide"],
            }
        )

    async def get(self, path, *, params=None):
        self.gets.append((path, dict(params or {})))
        if path == "/fapi/v1/klines" and self.klines is not None:
            lo, hi = params["startTime"], params["endTime"]
            rows = [r for r in self.klines(params["symbol"]) if lo <= r[0] <= hi]
            return _Response(rows[: params["limit"]])
        return _Response(self.routes[path])


def make_client(transport=None, *, base_url=DEMO_URL) -> TrendDemoClient:
    client = TrendDemoClient.__new__(TrendDemoClient)
    client._client = transport or _Transport()
    client._base_url = base_url
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


# ---------------------------------------------------------- identity / gate


@pytest.mark.parametrize(
    "origin", ["https://fapi.binance.com", "http://demo-fapi.binance.com", ""]
)
def test_construction_off_the_exact_demo_origin_is_refused(origin) -> None:
    with pytest.raises(ValueError, match="exact Futures Demo origin"):
        TrendDemoClient(api_key="k", api_secret="s", base_url=origin)


def test_from_env_needs_the_trend_flag_before_anything_else(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_TREND_DEMO_ENABLED", raising=False)
    with pytest.raises(ValueError, match="BINANCE_TREND_DEMO_ENABLED"):
        TrendDemoClient.from_env()
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "TRUE")
    with pytest.raises(ValueError, match="BINANCE_TREND_DEMO_ENABLED"):
        TrendDemoClient.from_env()


def test_submit_posts_exactly_one_gated_one_way_market_order(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    transport = _Transport()
    result = run(make_client(transport).submit_order(**SEND))
    assert len(transport.posts) == 1
    posted = transport.posts[0]
    assert posted["positionSide"] == "BOTH" and posted["type"] == "MARKET"
    assert posted["newClientOrderId"] == "trend-abc"
    assert "reduceOnly" not in posted  # an entry carries no reduceOnly
    assert (result.status, result.position_side) == ("NEW", "BOTH")


def test_a_close_carries_reduce_only(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    transport = _Transport()
    run(
        make_client(transport).submit_order(
            **{**SEND, "side": "SELL", "reduce_only": True}
        )
    )
    assert transport.posts[0]["reduceOnly"] == "true"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"symbol": "DOGEUSDT"}, "universe"),
        ({"client_order_id": "h5-abc"}, "identity"),
        ({"client_order_id": None}, "identity"),
        ({"order_type": "LIMIT"}, "MARKET"),
        ({"position_side": "LONG"}, "MARKET"),
        ({"position_side": None}, "MARKET"),
        ({"confirm": False}, "confirm"),
        ({"confirm": None}, "confirm"),
        ({"confirm": 1}, "confirm"),
    ],
)
def test_submit_refuses_every_deviation_with_zero_posts(
    monkeypatch, change, message
) -> None:
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    transport = _Transport()
    with pytest.raises(ValueError, match=message):
        run(make_client(transport).submit_order(**{**SEND, **change}))
    assert transport.posts == []


def test_submit_refuses_without_the_trend_flag_and_off_origin(monkeypatch) -> None:
    transport = _Transport()
    monkeypatch.delenv("BINANCE_TREND_DEMO_ENABLED", raising=False)
    with pytest.raises(ValueError, match="BINANCE_TREND_DEMO_ENABLED"):
        run(make_client(transport).submit_order(**SEND))
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    with pytest.raises(ValueError, match="exact Futures Demo origin"):
        run(
            make_client(transport, base_url="https://fapi.binance.com").submit_order(
                **SEND
            )
        )
    assert transport.posts == []


def test_a_transport_that_drifts_off_the_demo_origin_is_refused_per_call(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BINANCE_TREND_DEMO_ENABLED", "true")
    transport = _Transport()
    transport.base_url = "https://fapi.binance.com"
    with pytest.raises(ValueError, match="exact Futures Demo origin"):
        run(make_client(transport).submit_order(**SEND))
    with pytest.raises(ValueError, match="exact Futures Demo origin"):
        make_client(transport).kline_transport()
    assert transport.posts == []


# ---------------------------------------------------------------- reads


def test_the_book_quote_and_filters_are_validated() -> None:
    transport = _Transport(
        {
            "/fapi/v1/ticker/bookTicker": {
                "symbol": "BTCUSDT",
                "bidPrice": "99.5",
                "askPrice": "100",
            },
            "/fapi/v1/exchangeInfo": {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "quantityPrecision": 3,
                        "filters": [
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "stepSize": "0.001",
                                "minQty": "0.001",
                                "maxQty": "120",
                            },
                            {"filterType": "MIN_NOTIONAL", "notional": "100"},
                        ],
                    }
                ]
            },
        }
    )
    client = make_client(transport)
    quote = run(client.get_book_quote("BTCUSDT"))
    assert (quote.executable_price("BUY"), quote.executable_price("SELL")) == (
        D("100"),
        D("99.5"),
    )
    filters = run(client.get_trend_filters("BTCUSDT"))
    assert (filters.step_size, filters.min_notional_usdt) == (D("0.001"), D("100"))
    with pytest.raises(ValueError):
        run(client.get_book_quote("DOGEUSDT"))


def test_a_crossed_or_mismatched_quote_is_refused() -> None:
    for body in (
        {"symbol": "BTCUSDT", "bidPrice": "101", "askPrice": "100"},
        {"symbol": "ETHUSDT", "bidPrice": "1", "askPrice": "2"},
        {"symbol": "BTCUSDT", "bidPrice": "0", "askPrice": "2"},
    ):
        client = make_client(_Transport({"/fapi/v1/ticker/bookTicker": body}))
        with pytest.raises(TrendBrokerTruthUnavailable):
            run(client.get_book_quote("BTCUSDT"))


def _position_risk(**overrides):
    row = {
        "symbol": "BTCUSDT",
        "positionAmt": "0",
        "entryPrice": "0",
        "leverage": "1",
        "positionSide": "BOTH",
        "marginType": "isolated",
    }
    row.update(overrides)
    return row


def test_isolated_1x_both_must_be_proven_by_readback() -> None:
    def client_for(row):
        return make_client(_Transport({"/fapi/v2/positionRisk": [row]}))

    run(client_for(_position_risk()).assert_symbol_isolated_1x("BTCUSDT"))
    for bad in (
        {"marginType": "cross"},
        {"leverage": "5"},
        {"positionSide": "LONG"},
    ):
        with pytest.raises(TrendBrokerTruthUnavailable):
            run(client_for(_position_risk(**bad)).assert_symbol_isolated_1x("BTCUSDT"))


def test_account_reads_must_be_complete_one_way_and_unique() -> None:
    good = make_client(
        _Transport(
            {
                "/fapi/v2/positionRisk": [
                    _position_risk(),
                    _position_risk(symbol="ETHUSDT", positionAmt="-2"),
                ],
                "/fapi/v1/openOrders": [],
            }
        )
    )
    positions = run(good.get_all_positions())
    assert {p.symbol: p.position_amt for p in positions} == {
        "BTCUSDT": D("0"),
        "ETHUSDT": D("-2"),
    }
    assert run(good.get_all_open_orders()).orders == []
    for body in (
        [],
        {"not": "a list"},
        [_position_risk(), _position_risk()],
        [_position_risk(positionSide="LONG")],
        [_position_risk(positionAmt="x")],
        [_position_risk(leverage="0")],
    ):
        with pytest.raises(TrendBrokerTruthUnavailable):
            run(
                make_client(
                    _Transport({"/fapi/v2/positionRisk": body})
                ).get_all_positions()
            )
    with pytest.raises(TrendBrokerTruthUnavailable):
        run(
            make_client(
                _Transport({"/fapi/v1/openOrders": {"x": 1}})
            ).get_all_open_orders()
        )


def test_get_order_needs_the_trend_identity_and_a_broker_echo() -> None:
    client = make_client()
    with pytest.raises(ValueError, match="identity"):
        run(client.get_order(symbol="BTCUSDT", client_order_id="h5-abc"))
    with pytest.raises(ValueError):
        run(client.get_order(symbol="DOGEUSDT", client_order_id="trend-abc"))


def test_there_is_no_leverage_margin_cancel_or_order_test_surface() -> None:
    client = make_client()
    for call in (
        client.set_leverage(symbol="BTCUSDT", leverage=1),
        client.set_margin_type(symbol="BTCUSDT"),
        client.cancel_order(symbol="BTCUSDT", client_order_id="trend-x"),
        client.order_test(symbol="BTCUSDT"),
    ):
        with pytest.raises(ValueError):
            run(call)


# ------------------------------------------------------- kline collectors

# A fixed decision time well in the past so the in-progress-row filter of the
# ROB-993 reader (which compares with the wall clock) never drops a fixture row.
T = 1_000 * DAY_MS


def klines_for(close_ts_with_gap=None, flat="100"):
    """1m rows for every minute of every 4h bucket the signal asks for."""

    def rows(symbol):
        out = []
        for day in range(0, 121):
            end = T - day * DAY_MS
            for n in range(240):
                ts = end - FOUR_HOUR_MS + n * MINUTE_MS
                if close_ts_with_gap == end and n == 100:
                    continue  # one missing minute: the bucket is incomplete
                price = flat if not (day == 0 and n == 239) else "101.5"
                out.append([ts, price, price, price, price, "1", ts + MINUTE_MS - 1])
        return sorted(out)

    return rows


def test_signal_bars_cover_only_the_needed_buckets_and_keep_exact_close_text() -> None:
    transport = _Transport(klines=klines_for())
    client = make_client(transport)
    bars = run(
        collect_signal_bars(
            client.kline_transport(),
            "BTCUSDT",
            decision_ts=T,
            lookback_days=120,
            check=client._assert_trend,
        )
    )
    assert len(bars) == 22  # 21 daily samples + t-120d, not 720 4h bars
    assert bars[-1].close_ts == T and bar_close_text(bars[-1]) == "101.5"
    assert len(transport.gets) == 22  # one bounded page per bucket
    assert all(p["limit"] == 500 for _, p in transport.gets)


def test_a_bucket_missing_one_minute_yields_no_bar() -> None:
    gap_at = T - 20 * DAY_MS
    client = make_client(_Transport(klines=klines_for(close_ts_with_gap=gap_at)))
    bars = run(
        collect_signal_bars(
            client.kline_transport(),
            "BTCUSDT",
            decision_ts=T,
            lookback_days=20,
            check=client._assert_trend,
        )
    )
    assert gap_at not in {b.close_ts for b in bars}  # NO_SIGNAL, never filled
    assert len(bars) == 20


def test_the_origin_is_revalidated_before_every_page() -> None:
    client = make_client(_Transport(klines=klines_for()))
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 3:
            raise ValueError("origin drifted")

    with pytest.raises(ValueError, match="drifted"):
        run(
            collect_signal_bars(
                client.kline_transport(),
                "BTCUSDT",
                decision_ts=T,
                lookback_days=20,
                check=check,
            )
        )
    assert len(client._client.gets) == 2


def test_the_collectors_refuse_a_symbol_outside_the_universe_and_bad_alignment() -> (
    None
):
    client = make_client(_Transport(klines=klines_for()))
    with pytest.raises(ValueError, match="window"):
        run(
            collect_signal_bars(
                client.kline_transport(),
                "DOGEUSDT",
                decision_ts=T,
                lookback_days=20,
                check=client._assert_trend,
            )
        )
    with pytest.raises(ValueError, match="aligned"):
        run(
            collect_signal_bars(
                client.kline_transport(),
                "BTCUSDT",
                decision_ts=T + 1,
                lookback_days=20,
                check=client._assert_trend,
            )
        )


def test_the_hard_stop_window_is_bounded_and_includes_the_fill_minute() -> None:
    def rows(symbol):
        out = []
        for n in range(0, 3 * 24 * 60):
            ts = T - 3 * DAY_MS + n * MINUTE_MS
            low = "90" if n == 4 else "99"
            out.append([ts, "100", "101", low, "100", "1", ts + MINUTE_MS - 1])
        return out

    transport = _Transport(klines=rows)
    client = make_client(transport)
    entered_ms = T - 3 * DAY_MS + 3 * MINUTE_MS + 500  # the fill is minute 3
    minutes = run(
        collect_hard_stop_minutes(
            client.kline_transport(),
            "BTCUSDT",
            entered_ms=entered_ms,
            now_ms=T,
            check=client._assert_trend,
        )
    )
    # Only the last 24h are read even though the fill was three days ago.
    assert minutes[0].ts >= T - 24 * 60 * MINUTE_MS
    assert minute_low(minutes[0]) == D("99") and minute_high(minutes[0]) == D("101")
    assert len(transport.gets) <= 3  # 1440 minutes / 500 per page

    recent = run(
        collect_hard_stop_minutes(
            client.kline_transport(),
            "BTCUSDT",
            entered_ms=T - 3 * DAY_MS + 4 * MINUTE_MS + 1,
            now_ms=T - 3 * DAY_MS + 8 * MINUTE_MS,
            check=client._assert_trend,
        )
    )
    # fill in minute 4 (low 90 inside that very minute): the fill minute counts,
    # otherwise a stop crossed after the fill but within it is never seen.
    assert min(minute_low(m) for m in recent) == D("90")
    assert recent[0].ts == T - 3 * DAY_MS + 4 * MINUTE_MS


def test_an_empty_window_reads_nothing() -> None:
    transport = _Transport(klines=lambda symbol: [])
    client = make_client(transport)
    assert (
        run(
            collect_hard_stop_minutes(
                client.kline_transport(),
                "BTCUSDT",
                entered_ms=T,
                now_ms=T + 30_000,
                check=client._assert_trend,
            )
        )
        == ()
    )
    assert transport.gets == []


def _continuous(skip=(), only=None):
    def rows(symbol):
        out = []
        for n in range(0, 3 * 24 * 60):
            ts = T - 3 * DAY_MS + n * MINUTE_MS
            if n in skip:
                continue
            out.append([ts, "100", "101", "99", "100", "1", ts + MINUTE_MS - 1])
        return out if only is None else out[:only]

    return rows


def _watch(klines, *, now_ms=T):
    client = make_client(_Transport(klines=klines))
    return run(
        collect_hard_stop_minutes(
            client.kline_transport(),
            "BTCUSDT",
            entered_ms=T - 3 * DAY_MS,
            now_ms=now_ms,
            check=client._assert_trend,
        )
    )


def test_a_missing_minute_in_the_watch_window_is_a_gap_not_a_watch() -> None:
    missing = 3 * 24 * 60 - 100  # inside the last 24 hours
    with pytest.raises(TrendKlineGap):
        _watch(_continuous(skip={missing}))


def test_an_empty_answer_for_the_watch_window_is_a_gap() -> None:
    with pytest.raises(TrendKlineGap):
        _watch(_continuous(only=0))


def test_a_truncated_page_is_a_gap_and_a_complete_window_passes() -> None:
    with pytest.raises(TrendKlineGap):
        _watch(_continuous(only=3 * 24 * 60 - 5))  # the newest minutes missing
    minutes = _watch(_continuous())
    assert len(minutes) == 24 * 60


def _filters_body(market, plain=None):
    filters = [{"filterType": "MIN_NOTIONAL", "notional": "100"}]
    if market is not None:
        filters.append({"filterType": "MARKET_LOT_SIZE", **market})
    if plain is not None:
        filters.append({"filterType": "LOT_SIZE", **plain})
    return {
        "/fapi/v1/exchangeInfo": {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "quantityPrecision": 3,
                    "filters": filters,
                }
            ]
        }
    }


def test_a_zero_market_min_qty_means_no_minimum_and_falls_back_to_lot_size() -> None:
    market = {"stepSize": "0.001", "minQty": "0.00000000", "maxQty": "120"}
    plain = {"stepSize": "0.001", "minQty": "0.005", "maxQty": "1000"}
    client = make_client(_Transport(_filters_body(market, plain)))
    filters = run(client.get_trend_filters("BTCUSDT"))
    assert (filters.step_size, filters.min_qty, filters.max_qty) == (
        D("0.001"),
        D("0.005"),
        D("120"),
    )


def test_a_zero_market_min_qty_without_lot_size_uses_one_step() -> None:
    market = {"stepSize": "0.001", "minQty": "0", "maxQty": "120"}
    client = make_client(_Transport(_filters_body(market)))
    assert run(client.get_trend_filters("BTCUSDT")).min_qty == D("0.001")


def test_a_zero_market_step_and_max_fall_back_to_lot_size_and_junk_is_refused() -> None:
    market = {"stepSize": "0", "minQty": "0", "maxQty": "0"}
    plain = {"stepSize": "0.01", "minQty": "0.01", "maxQty": "500"}
    filters = run(
        make_client(_Transport(_filters_body(market, plain))).get_trend_filters(
            "BTCUSDT"
        )
    )
    assert (filters.step_size, filters.min_qty, filters.max_qty) == (
        D("0.01"),
        D("0.01"),
        D("500"),
    )
    for bad in (
        {"stepSize": "0.001", "minQty": "-1", "maxQty": "9"},
        {"stepSize": "0.001", "minQty": "x", "maxQty": "9"},
        {"stepSize": "0.001", "minQty": "0", "maxQty": "0"},
    ):
        with pytest.raises(TrendBrokerTruthUnavailable):
            run(
                make_client(_Transport(_filters_body(bad))).get_trend_filters("BTCUSDT")
            )


@pytest.mark.parametrize(
    ("wire", "hedge"),
    [(True, True), (False, False), ("true", True), ("false", False)],
)
def test_position_mode_accepts_the_bool_and_the_string_wire_forms(wire, hedge) -> None:
    client = make_client(
        _Transport({"/fapi/v1/positionSide/dual": {"dualSidePosition": wire}})
    )
    assert run(client.get_position_mode()).is_hedge_mode is hedge


@pytest.mark.parametrize("wire", [None, "True", "FALSE", 1, 0, "", "yes", [], {}])
def test_position_mode_refuses_anything_else_and_never_reads_a_string_as_hedge(
    wire,
) -> None:
    client = make_client(
        _Transport({"/fapi/v1/positionSide/dual": {"dualSidePosition": wire}})
    )
    with pytest.raises(TrendBrokerTruthUnavailable):
        run(client.get_position_mode())
    with pytest.raises(TrendBrokerTruthUnavailable):
        run(
            make_client(
                _Transport({"/fapi/v1/positionSide/dual": ["x"]})
            ).get_position_mode()
        )
