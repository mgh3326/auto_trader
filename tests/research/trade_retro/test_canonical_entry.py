"""The fill union, the KIS key, entry classes and fill-rate denominators."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from research.trade_retro.canonical import (
    Cleaning,
    build_canon,
    next_open_session,
    normalize_symbol,
    reclassify_implausible,
    reference_close,
)
from research.trade_retro.entry import (
    classify_orders,
    distance_class,
    distance_pct,
    fill_rates,
)
from research.trade_retro.export import load_export
from research.trade_retro.prices import Bars, PriceBook
from research.trade_retro.rules import unfilled_buy_counterfactual
from tests.research.trade_retro.retro_fixtures import fill_row, order_row, write_export

pytestmark = pytest.mark.unit


def _canon(tmp_path, rows):
    return build_canon(load_export(write_export(tmp_path / "export", rows)))


def _filled(canon, ref):
    return canon.orders.set_index("order_ref").loc[ref]


def test_an_open_row_with_ledger_fills_counts_as_filled(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "live_order_ledger": [
                order_row(
                    "upbit_live",
                    "crypto",
                    "KRW-EEE",
                    "sell",
                    2,
                    100,
                    "2026-08-01T00:00:00Z",
                    "o1",
                    "open",
                )
            ],
            "execution_ledger": [
                fill_row(
                    "upbit_live",
                    "crypto",
                    "EEE",
                    "sell",
                    2,
                    100,
                    "2026-08-01T01:00:00Z",
                    "o1",
                )
            ],
        },
    )
    order = _filled(canon, "o1")
    assert (order["filled_qty"], order["outcome"]) == (2.0, "filled")
    assert canon.diagnostics["open_rows_with_ledger_fills"] == 1
    assert len(canon.fills) == 1


def test_a_filled_row_without_ledger_rows_adds_a_fill(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "toss_live_order_ledger": [
                order_row(
                    "toss_live",
                    "us",
                    "BBB",
                    "buy",
                    2,
                    210,
                    "2026-08-17T13:51:00Z",
                    "o2",
                    "filled",
                    filled_qty="2",
                    fill_price="95.5",
                )
            ],
        },
    )
    row = canon.fills.iloc[0]
    assert (row["source"], row["qty"], row["price"]) == (
        "order_ledger_only",
        2.0,
        95.5,
    )
    assert row["ts"] == datetime(2026, 8, 17, 13, 51, tzinfo=UTC)


def test_a_partial_ledger_quantity_adds_only_the_residual(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "toss_live_order_ledger": [
                order_row(
                    "toss_live",
                    "us",
                    "BBB",
                    "buy",
                    3,
                    200,
                    "2026-08-17T13:51:00Z",
                    "o3",
                    "filled",
                    filled_qty="3",
                    fill_price="200",
                )
            ],
            "execution_ledger": [
                fill_row(
                    "toss_live",
                    "us",
                    "BBB",
                    "buy",
                    1,
                    200,
                    "2026-08-17T14:00:00Z",
                    "o3",
                )
            ],
        },
    )
    assert sorted(canon.fills["qty"]) == [1.0, 2.0]
    assert _filled(canon, "o3")["filled_qty"] == 3.0


def test_a_reused_kis_order_number_keeps_two_days_apart(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "kis_live_order_ledger": [
                order_row(
                    "kis_live",
                    "kr",
                    "005930",
                    "buy",
                    1,
                    100,
                    "2026-06-01T01:00:00Z",
                    "same",
                    "expired",
                    day="2026-06-01",
                ),
                order_row(
                    "kis_live",
                    "kr",
                    "005930",
                    "buy",
                    1,
                    100,
                    "2026-06-05T01:00:00Z",
                    "same",
                    "open",
                    day="2026-06-05",
                ),
            ],
            "execution_ledger": [
                fill_row(
                    "kis_live",
                    "kr",
                    "005930",
                    "buy",
                    1,
                    100,
                    "2026-06-05T02:00:00Z",
                    "same",
                    day="2026-06-05",
                )
            ],
        },
    )
    by_day = canon.orders.set_index("trading_day")["filled_qty"]
    assert (by_day["2026-06-01"], by_day["2026-06-05"]) == (0.0, 1.0)


def test_a_kis_fill_one_day_from_its_order_still_links(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "kis_live_order_ledger": [
                order_row(
                    "kis_live",
                    "us",
                    "CCC",
                    "sell",
                    1,
                    100,
                    "2026-08-20T15:00:00Z",
                    "shift",
                    "filled",
                    day="2026-08-20",
                    filled_qty="1",
                    fill_price="100",
                )
            ],
            "execution_ledger": [
                fill_row(
                    "kis_live",
                    "us",
                    "CCC",
                    "sell",
                    1,
                    100,
                    "2026-08-20T15:10:00Z",
                    "shift",
                    day="2026-08-21",
                )
            ],
        },
    )
    assert canon.diagnostics["kis_day_fallback_fills"] == 1
    assert len(canon.fills) == 1


def test_superseded_rows_never_count(tmp_path):
    row = fill_row(
        "upbit_live", "crypto", "EEE", "buy", 5, 100, "2026-08-01T01:00:00Z", "o9"
    )
    row["superseded_duplicate"] = "true"
    canon = _canon(tmp_path, {"execution_ledger": [row]})
    assert canon.fills.empty


def test_a_rung_marks_its_order_as_proposal_sourced(tmp_path):
    rung = {
        "row_ref": "r1", "rung_ref": "rng1", "proposal_ref": "prp1", "rung_index": "2",
        "account_alias": "toss_live", "market": "us", "symbol": "BBB", "side": "buy",
        "state": "filled", "order_ref": "o1", "updated_at_utc": "2026-08-01T00:10:00Z",
    }  # fmt: skip
    proposal = {
        "row_ref": "p1", "proposal_ref": "prp1", "root_proposal_ref": "prp1", "revision": "1",
        "account_alias": "toss_live", "market": "us", "symbol": "BBB", "side": "buy",
        "strategy_tag": "discovery", "exit_intent": "",
    }  # fmt: skip
    canon = _canon(
        tmp_path,
        {
            "toss_live_order_ledger": [
                order_row(
                    "toss_live",
                    "us",
                    "BBB",
                    "buy",
                    1,
                    100,
                    "2026-08-01T00:00:00Z",
                    "o1",
                    "open",
                ),
                order_row(
                    "toss_live",
                    "us",
                    "BBB",
                    "buy",
                    1,
                    100,
                    "2026-08-01T00:00:00Z",
                    "o2",
                    "open",
                ),
            ],
            "order_proposal_rungs": [rung],
            "order_proposals": [proposal],
        },
    )
    assert canon.orders.set_index("order_ref")["source"].to_dict() == {
        "o1": "proposal",
        "o2": "session",
    }
    linked = _filled(canon, "o1")
    assert (linked["rung_index"], linked["proposal_tag"]) == (2, "discovery")


@pytest.mark.parametrize(
    ("market", "raw", "expected"),
    [
        ("crypto", "KRW-EEE", "EEE"),
        ("crypto", "EEE", "EEE"),
        ("us", "BRK/B", "BRK.B"),
        ("kr", "005930", "005930"),
    ],
)
def test_normalize_symbol(market, raw, expected):
    assert normalize_symbol(market, raw) == expected


def test_distance_is_positive_when_away_from_the_market():
    assert distance_pct("buy", 97.0, 100.0) == pytest.approx(3.0)
    assert distance_pct("sell", 103.0, 100.0) == pytest.approx(3.0)
    assert distance_pct("buy", 101.0, 100.0) == pytest.approx(-1.0)


@pytest.mark.parametrize(
    ("order_type", "distance", "expected"),
    [
        ("limit", 3.0, "deep_limit"),
        ("limit", 2.99, "near_market"),
        ("limit", -1.0, "near_market"),
        ("market", None, "near_market"),
        ("limit", None, "no_reference"),
    ],
)
def test_distance_class(order_type, distance, expected):
    assert distance_class(order_type, distance) == expected


def test_reference_close_never_uses_an_incomplete_bar():
    days = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    bars = Bars.from_rows([(d, 1.0, 100.0 + i) for i, d in enumerate(days)])
    # 2026-06-02 10:00 KST: 06-02 is still trading, so the reference is 06-01.
    assert reference_close(bars, "kr", datetime(2026, 6, 2, 1, tzinfo=UTC)) == 100.0
    # 2026-06-02 16:00 KST: the regular session has closed.
    assert reference_close(bars, "kr", datetime(2026, 6, 2, 7, tzinfo=UTC)) == 101.0
    # A crypto bar closes at the next UTC midnight.
    assert (
        reference_close(bars, "crypto", datetime(2026, 6, 2, 23, tzinfo=UTC)) == 100.0
    )


def test_fill_rate_excludes_rejections_and_cancel_rows_and_offers_both_open_treatments(
    tmp_path,
):
    rows = {
        "toss_live_order_ledger": [
            order_row(
                "toss_live",
                "us",
                "AAA",
                "buy",
                1,
                100,
                "2026-08-03T14:00:00Z",
                "a",
                "filled",
                filled_qty="1",
                fill_price="100",
            ),
            order_row(
                "toss_live",
                "us",
                "AAA",
                "buy",
                1,
                100,
                "2026-08-10T14:00:00Z",
                "b",
                "expired",
            ),
            order_row(
                "toss_live",
                "us",
                "AAA",
                "buy",
                1,
                100,
                "2026-08-11T14:00:00Z",
                "c",
                "open",
            ),
            order_row(
                "toss_live",
                "us",
                "AAA",
                "buy",
                1,
                100,
                "2026-08-12T14:00:00Z",
                "d",
                "rejected",
            ),
            order_row(
                "toss_live",
                "us",
                "AAA",
                "buy",
                1,
                100,
                "2026-08-13T14:00:00Z",
                "e",
                "cancelled",
                operation_kind="cancel",
            ),
        ]
    }
    canon = _canon(tmp_path, rows)
    orders = classify_orders(
        canon.orders, PriceBook(bars={}, benchmarks={}, missing={})
    )
    row = fill_rates(orders, ["side"]).iloc[0]
    assert (row["accepted"], row["filled"], row["open"]) == (3, 1, 1)
    assert row["fill_rate"] == pytest.approx(1 / 3)
    assert row["fill_rate_closed"] == pytest.approx(1 / 2)
    # Weeks 32 and 33: one intent filled, one not.
    assert (row["intents"], row["intent_fill_rate"]) == (2, 0.5)


def test_implausible_fill_keeps_its_quantity_but_stops_being_a_trade():
    day = date(2026, 6, 8)
    bars = Bars.from_rows([(day, 300.0, 300.0)])
    book = PriceBook(bars={("kr", "AAA"): bars}, benchmarks={}, missing={})
    fills = pd.DataFrame(
        [
            {"alias": "a", "market": "kr", "symbol": "AAA", "side": "buy", "qty": 4.0,
             "price": 75.0, "date": day, "seed": False, "source": "reconciler"},
            {"alias": "a", "market": "kr", "symbol": "AAA", "side": "buy", "qty": 1.0,
             "price": 295.0, "date": day, "seed": False, "source": "reconciler"},
        ]
    )  # fmt: skip
    out, flagged = reclassify_implausible(fills, book)
    assert out["source"].tolist() == ["implausible_price", "reconciler"]
    assert out["seed"].tolist() == [True, False]
    assert out["qty"].tolist() == [4.0, 1.0]
    assert flagged[0]["deviation"] == pytest.approx(-0.75)


def _ws(symbol, qty, price, stamp, ref, created, alias="kis_live", market="kr"):
    return fill_row(
        alias,
        market,
        symbol,
        "sell",
        qty,
        price,
        stamp,
        ref,
        source="websocket",
        created=created,
    )


def test_a_websocket_row_repeating_next_days_reconciler_row_is_removed(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "execution_ledger": [
                _ws("AAA", 7, 300, "2026-08-03T23:03:51Z", "twin", "2026-08-04T23:03:51Z"),
                fill_row(
                    "kis_live", "kr", "AAA", "sell", 7, 300, "2026-08-04T23:02:40Z", "twin",
                    day="2026-08-05",
                ),
            ]
        },
    )  # fmt: skip
    assert canon.fills["source"].tolist() == ["reconciler"]
    assert canon.diagnostics["cross_day_twins_dropped"] == 1


@pytest.mark.parametrize(
    ("ws_qty", "ws_price", "stamp"),
    [
        (6, 300, "2026-08-03T23:03:51Z"),
        (7, 301, "2026-08-03T23:03:51Z"),
        (7, 300, "2026-07-27T23:03:51Z"),
    ],
    ids=["other-quantity", "other-price", "too-far-apart"],
)
def test_a_websocket_row_that_differs_from_the_reconciler_row_stays(
    tmp_path, ws_qty, ws_price, stamp
):
    canon = _canon(
        tmp_path,
        {
            "execution_ledger": [
                _ws("AAA", ws_qty, ws_price, stamp, "ref", stamp),
                fill_row(
                    "kis_live",
                    "kr",
                    "AAA",
                    "sell",
                    7,
                    300,
                    "2026-08-04T23:02:40Z",
                    "ref",
                    day="2026-08-05",
                ),
            ]
        },
    )
    assert len(canon.fills) == 2


def test_a_row_recorded_on_time_is_a_real_fill_even_if_it_matches(tmp_path):
    stamp = "2026-08-03T23:03:51Z"
    canon = _canon(
        tmp_path,
        {
            "execution_ledger": [
                _ws("AAA", 7, 300, stamp, "ref", "2026-08-03T23:03:52Z"),
                fill_row(
                    "kis_live",
                    "kr",
                    "AAA",
                    "sell",
                    7,
                    300,
                    "2026-08-04T23:02:40Z",
                    "ref",
                    day="2026-08-05",
                ),
            ]
        },
    )
    assert len(canon.fills) == 2


def test_a_reused_reference_on_another_symbol_is_not_a_repeat(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "execution_ledger": [
                _ws(
                    "AAA", 7, 300, "2026-08-03T23:03:51Z", "ref", "2026-08-04T23:03:51Z"
                ),
                fill_row(
                    "kis_live",
                    "kr",
                    "BBB",
                    "sell",
                    7,
                    300,
                    "2026-08-04T23:02:40Z",
                    "ref",
                    day="2026-08-05",
                ),
            ]
        },
    )
    assert len(canon.fills) == 2


def test_a_seed_recorded_after_a_repeated_sell_is_reduced_and_an_earlier_one_is_not(
    tmp_path,
):
    seed = lambda symbol, qty, created: fill_row(  # noqa: E731
        "kis_live", "kr", symbol, "buy", qty, 200, "2026-05-10T00:00:00Z", f"seed{symbol}",
        day="2026-05-10", source="manual_import", created=created,
    )  # fmt: skip
    twin = lambda symbol, ref, created: [  # noqa: E731
        _ws(symbol, 7, 300, "2026-08-03T23:00:00Z", ref, created),
        fill_row(
            "kis_live",
            "kr",
            symbol,
            "sell",
            7,
            300,
            "2026-08-04T23:00:00Z",
            ref,
            day="2026-08-05",
        ),
    ]
    canon = _canon(
        tmp_path,
        {
            "execution_ledger": [
                seed("AAA", 20, "2026-08-06T04:00:00Z"),
                *twin("AAA", "t1", "2026-08-04T23:00:00Z"),
                seed("BBB", 20, "2026-07-28T04:00:00Z"),
                *twin("BBB", "t2", "2026-08-04T23:00:00Z"),
            ]
        },
    )
    seeds = canon.fills[canon.fills["seed"]].set_index("symbol")["qty"]
    assert seeds["AAA"] == 13.0
    assert seeds["BBB"] == 20.0
    assert canon.diagnostics["seeds_corrected_for_twins"] == 1


def _upbit_order(qty, stamp="2026-08-12T16:00:00Z"):
    return order_row(
        "upbit_live", "crypto", "KRW-DDD", "buy", qty, 100, stamp, "o", "filled",
        filled_qty=str(qty), fill_price="100",
    )  # fmt: skip


def _upbit_fill(qty):
    return fill_row(
        "upbit_live",
        "crypto",
        "DDD",
        "buy",
        qty,
        100,
        "2026-08-12T16:51:14Z",
        "o",
        source="websocket",
    )


def test_a_running_total_replaces_the_partial_fills_it_includes(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "live_order_ledger": [_upbit_order(0.25)],
            "execution_ledger": [
                _upbit_fill(0.11),
                _upbit_fill(0.09),
                _upbit_fill(0.25),
            ],
        },
    )
    assert canon.fills["qty"].tolist() == [0.25]
    assert canon.orders.iloc[0]["filled_qty"] == 0.25
    assert canon.diagnostics["running_total_rows_dropped"] == 2


def test_real_partial_fills_summing_to_the_order_are_kept(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "live_order_ledger": [_upbit_order(4.0)],
            "execution_ledger": [_upbit_fill(0.5), _upbit_fill(3.5)],
        },
    )
    assert sorted(canon.fills["qty"]) == [0.5, 3.5]
    assert canon.diagnostics["running_total_rows_dropped"] == 0


def test_an_overfilled_order_with_no_total_row_is_counted_not_guessed(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "live_order_ledger": [_upbit_order(1.0)],
            "execution_ledger": [_upbit_fill(0.7), _upbit_fill(0.6)],
        },
    )
    assert len(canon.fills) == 2
    assert canon.diagnostics["overfilled_orders_unresolved"] == 1


def test_a_fill_a_day_from_its_order_takes_the_orders_date(tmp_path):
    canon = _canon(
        tmp_path,
        {
            "kis_live_order_ledger": [
                order_row(
                    "kis_live",
                    "us",
                    "CCC",
                    "sell",
                    1,
                    100,
                    "2026-08-20T15:00:00Z",
                    "s",
                    "open",
                    day="2026-08-20",
                )
            ],
            "execution_ledger": [
                fill_row(
                    "kis_live",
                    "us",
                    "CCC",
                    "sell",
                    1,
                    100,
                    "2026-08-19T15:10:00Z",
                    "s",
                    day="2026-08-19",
                )
            ],
        },
    )
    assert canon.fills.iloc[0]["ts"] == datetime(2026, 8, 20, 15, 0, tzinfo=UTC)


def test_next_open_session_is_never_an_open_that_already_passed():
    days = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 5), date(2026, 6, 8)]
    bars = Bars.from_rows([(d, 1.0, 1.0) for d in days])
    # 08:30 KST on 06-01, before the 09:00 open.
    assert next_open_session(bars, "kr", datetime(2026, 5, 31, 23, 30, tzinfo=UTC)) == 0
    # 10:00 KST on 06-01, after the open.
    assert next_open_session(bars, "kr", datetime(2026, 6, 1, 1, tzinfo=UTC)) == 1
    # 10:00 New York on 06-02, after the 09:30 open: 06-03 has no bar, so 06-05.
    assert next_open_session(bars, "us", datetime(2026, 6, 2, 14, tzinfo=UTC)) == 2
    # A crypto day opens at 00:00 UTC, so the next open is always the next day.
    assert (
        next_open_session(bars, "crypto", datetime(2026, 6, 1, 0, 0, tzinfo=UTC)) == 1
    )


def test_limit_distance_converts_a_limit_sent_before_a_split():
    split_day = date(2026, 6, 3)
    days = [date(2026, 6, 1), date(2026, 6, 2), split_day]
    # The adjusted close is 100, so the raw close before the 1.3 split was 130.
    bars = Bars.from_rows([(d, 100.0, 100.0) for d in days], splits=[(split_day, 1.3)])
    book = PriceBook(bars={("kr", "AAA"): bars}, benchmarks={}, missing={})
    orders = pd.DataFrame(
        [
            {
                "alias": "a", "market": "kr", "symbol": "AAA", "side": "buy", "order_type": "limit",
                "limit_price": 126.1, "created": datetime(2026, 6, 2, 1, tzinfo=UTC), "source": "session",
                "accepted": True, "outcome": "expired", "operation": "",
            }
        ]
    )  # fmt: skip
    row = classify_orders(orders, book).iloc[0]
    assert row["distance"] == pytest.approx(3.0)
    assert row["klass"] == "deep_limit"


def _unfilled_frame():
    days = [date(2026, 6, 1) + pd.Timedelta(days=i) for i in range(30)]
    days = [d for d in days if d.weekday() < 5]
    return days


def test_r1_uses_the_next_open_and_skips_an_intent_that_filled(tmp_path):
    days = _unfilled_frame()
    bars = Bars.from_rows([(d, 100.0 + i, 110.0 + i) for i, d in enumerate(days)])
    index = Bars.from_rows([(d, 1000.0, 1100.0) for d in days])
    book = PriceBook(bars={("us", "AAA"): bars}, benchmarks={"us": index}, missing={})
    canon = _canon(
        tmp_path,
        {
            "toss_live_order_ledger": [
                # Week of 06-01: this one never fills.
                order_row(
                    "toss_live",
                    "us",
                    "AAA",
                    "buy",
                    1,
                    90,
                    "2026-06-01T15:00:00Z",
                    "a",
                    "expired",
                ),
                # Week of 06-08: one order expires, and another fills in the same intent.
                order_row(
                    "toss_live",
                    "us",
                    "AAA",
                    "buy",
                    1,
                    90,
                    "2026-06-08T15:00:00Z",
                    "b",
                    "expired",
                ),
                order_row(
                    "toss_live",
                    "us",
                    "AAA",
                    "buy",
                    1,
                    90,
                    "2026-06-09T15:00:00Z",
                    "c",
                    "filled",
                    filled_qty="1",
                    fill_price="90",
                ),
            ]
        },
    )
    orders = classify_orders(canon.orders, book)
    frame = unfilled_buy_counterfactual(orders, book)
    assert len(frame) == 1
    row = frame.iloc[0]
    # Placed after the 06-01 open, so it buys the 06-02 open (session 1, open 101).
    assert row["r5"] == pytest.approx((110.0 + 6) / 101.0 - 1.0)
    # The index leg starts at that same open: 1000 to 1100.
    assert row["x5"] == pytest.approx((110.0 + 6) / 101.0 - 1.1)


def test_a_partial_proven_by_inventory_is_removed_and_a_real_partial_stays(tmp_path):
    def sell(symbol, qty, stamp, ref):
        return fill_row(
            "upbit_live",
            "crypto",
            symbol,
            "sell",
            qty,
            10,
            stamp,
            ref,
            source="websocket",
        )

    def buy(symbol, qty):
        return fill_row(
            "upbit_live",
            "crypto",
            symbol,
            "buy",
            qty,
            9,
            "2026-08-01T00:00:00Z",
            f"b{symbol}",
        )

    canon = _canon(
        tmp_path,
        {
            "execution_ledger": [
                buy("AAA", 100),
                sell("AAA", 40, "2026-08-02T00:00:00Z", "o1"),
                sell("AAA", 100, "2026-08-02T00:00:06Z", "o1"),
                buy("BBB", 100),
                sell("BBB", 40, "2026-08-02T00:00:00Z", "o2"),
                sell("BBB", 60, "2026-08-02T00:00:06Z", "o2"),
            ]
        },
    )
    sold = (
        canon.fills[canon.fills["side"] == "sell"].groupby("symbol")["qty"].apply(list)
    )
    assert sold["AAA"] == [100.0]
    assert sold["BBB"] == [40.0, 60.0]
    assert canon.diagnostics["inventory_proven_partials_dropped"] == 1


def test_cleaning_flags_turn_each_rule_off(tmp_path):
    rows = {
        "execution_ledger": [
            _ws("AAA", 7, 300, "2026-08-03T23:03:51Z", "twin", "2026-08-04T23:03:51Z"),
            fill_row(
                "kis_live",
                "kr",
                "AAA",
                "sell",
                7,
                300,
                "2026-08-04T23:02:40Z",
                "twin",
                day="2026-08-05",
            ),
        ],
        "toss_live_order_ledger": [
            order_row(
                "toss_live",
                "us",
                "BBB",
                "buy",
                2,
                10,
                "2026-08-17T13:51:00Z",
                "only",
                "filled",
                filled_qty="2",
                fill_price="10",
            )
        ],
    }
    export = load_export(write_export(tmp_path / "export", rows))
    assert len(build_canon(export).fills) == 2
    assert len(build_canon(export, Cleaning(twins=False)).fills) == 3
    assert len(build_canon(export, Cleaning(order_only=False)).fills) == 1
