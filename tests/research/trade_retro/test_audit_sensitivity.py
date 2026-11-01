"""The data checks count what dots asked about, and the sensitivity lines really differ."""

from __future__ import annotations

import pytest

from research.trade_retro.audit import (
    economic_duplicates,
    external_refs_on_other_rows,
    join_coverage,
    non_session_stamps,
    order_waterfall,
    quantity_mismatches,
)
from research.trade_retro.canonical import apply_splits, build_canon
from research.trade_retro.entry import build_executions, classify_orders
from research.trade_retro.export import load_export
from research.trade_retro.prices import load_snapshot
from research.trade_retro.sensitivity import run_scenarios
from tests.research.trade_retro.retro_fixtures import (
    fill_row,
    order_row,
    sample_export,
    sample_prices,
    write_export,
)

pytestmark = pytest.mark.unit


def _load(tmp_path, rows):
    return load_export(write_export(tmp_path / "export", rows))


def _piece(qty, ref="o1"):
    return fill_row(
        "upbit_live", "crypto", "AAA", "sell", qty, 10, "2026-08-16T21:23:00Z", ref
    )


def _order(qty, ref="o1"):
    return order_row(
        "upbit_live",
        "crypto",
        "KRW-AAA",
        "sell",
        qty,
        10,
        "2026-08-16T21:00:00Z",
        ref,
        "open",
    )


def test_identical_pieces_that_sum_to_the_order_are_counted_and_kept(tmp_path):
    export = _load(
        tmp_path,
        {
            "live_order_ledger": [_order(0.3)],
            "execution_ledger": [_piece(0.1), _piece(0.1), _piece(0.1)],
        },
    )
    canon = build_canon(export)
    result = economic_duplicates(export, canon)
    assert (result["groups"], result["extra_rows"]) == (1, 2)
    assert (result["orders_within_quantity"], result["orders_over_quantity"]) == (1, 0)
    assert len(canon.fills) == 3


def test_identical_pieces_over_the_order_quantity_are_reported_as_over(tmp_path):
    export = _load(
        tmp_path,
        {
            "live_order_ledger": [_order(0.1)],
            "execution_ledger": [_piece(0.1), _piece(0.1)],
        },
    )
    result = economic_duplicates(export, build_canon(export))
    assert (result["orders_within_quantity"], result["orders_over_quantity"]) == (0, 1)


def test_mismatch_counts_show_the_state_before_and_after_cleaning(tmp_path):
    rows = {
        "kis_live_order_ledger": [
            order_row(
                "kis_live",
                "kr",
                "BBB",
                "sell",
                1,
                100,
                "2026-08-04T23:00:00Z",
                "t",
                "filled",
                day="2026-08-05",
                filled_qty="1",
                fill_price="100",
            )
        ],
        "execution_ledger": [
            fill_row(
                "kis_live",
                "kr",
                "BBB",
                "sell",
                1,
                100,
                "2026-08-03T23:03:00Z",
                "t",
                day="2026-08-04",
                source="websocket",
                created="2026-08-04T23:03:00Z",
            ),
            fill_row(
                "kis_live",
                "kr",
                "BBB",
                "sell",
                1,
                100,
                "2026-08-04T23:02:00Z",
                "t",
                day="2026-08-05",
            ),
        ],
    }
    export = _load(tmp_path, rows)
    result = quantity_mismatches(export, build_canon(export))
    assert result["over_quantity_before"] == 1
    assert result["over_quantity_after"] == 0


def test_weekend_stamps_use_the_markets_local_day(tmp_path):
    seed = lambda market, symbol: fill_row(  # noqa: E731
        "kis_live", market, symbol, "buy", 1, 10, "2026-05-10T00:00:00Z", f"s{symbol}",
        day="2026-05-10", source="manual_import",
    )  # fmt: skip
    # Sunday in Seoul, Saturday in New York: a weekend in both.
    seeds = [seed("kr", "AAA"), seed("us", "BBB")]
    # Tuesday 00:00 UTC.
    weekday = fill_row(
        "kis_live",
        "kr",
        "CCC",
        "buy",
        1,
        10,
        "2026-09-29T00:00:00Z",
        "c",
        day="2026-09-29",
        source="manual_import",
    )
    stamps = non_session_stamps(
        _load(tmp_path, {"execution_ledger": [*seeds, weekday]})
    )
    by_market = stamps.set_index("market")
    assert (by_market.loc["kr", "rows"], by_market.loc["kr", "weekend"]) == (2, 1)
    assert (by_market.loc["us", "rows"], by_market.loc["us", "weekend"]) == (1, 1)


def test_an_account_with_fills_and_no_order_rows_does_not_break_the_join_table(
    tmp_path,
):
    book = load_snapshot(sample_prices(tmp_path / "prices"))
    export = load_export(sample_export(tmp_path / "export"))
    canon = build_canon(export)
    fills, _ = apply_splits(canon.fills, book)
    orders = classify_orders(canon.orders, book)
    executions = build_executions(fills, orders, book)
    table = join_coverage(executions, orders[orders["alias"] != "toss_live"])
    row = table[table["alias"] == "toss_live"].iloc[0]
    assert row["before_first_order_row"] == row["executed_orders"]


def test_order_waterfall_adds_up(tmp_path):
    rows = {
        "toss_live_order_ledger": [
            order_row(
                "toss_live",
                "us",
                "AAA",
                "buy",
                1,
                10,
                "2026-08-03T14:00:00Z",
                "a",
                "expired",
            ),
            order_row(
                "toss_live",
                "us",
                "AAA",
                "buy",
                1,
                10,
                "2026-08-03T14:00:00Z",
                "b",
                "rejected",
            ),
            order_row(
                "toss_live",
                "us",
                "AAA",
                "sell",
                1,
                10,
                "2026-08-03T14:00:00Z",
                "c",
                "cancelled",
                operation_kind="cancel",
            ),
            order_row(
                "toss_live",
                "us",
                "SGOV",
                "buy",
                1,
                10,
                "2026-08-03T14:00:00Z",
                "d",
                "expired",
            ),
        ]
    }
    export = _load(tmp_path, rows)
    book = load_snapshot(sample_prices(tmp_path / "prices"))
    row = order_waterfall(classify_orders(build_canon(export).orders, book)).iloc[0]
    assert (
        row["rows"],
        row["cancel_rows"],
        row["rejected"],
        row["parking"],
        row["accepted"],
    ) == (4, 1, 1, 1, 1)


def test_a_broken_join_key_would_show_up_as_external_refs_on_order_rows(tmp_path):
    book = load_snapshot(sample_prices(tmp_path / "prices"))
    export = load_export(sample_export(tmp_path / "export"))
    canon = build_canon(export)
    fills, _ = apply_splits(canon.fills, book)
    orders = classify_orders(canon.orders, book)
    executions = build_executions(fills, orders, book)
    assert external_refs_on_other_rows(executions, orders) == 0

    external = executions.iloc[[0]].assign(source="external")
    assert external_refs_on_other_rows(external, orders) == 1


def test_sensitivity_lines_are_labelled_and_the_naive_line_differs(tmp_path):
    export = load_export(sample_export(tmp_path / "export"))
    book = load_snapshot(sample_prices(tmp_path / "prices"))
    scenarios = {s.name: s for s in run_scenarios(export, book)}
    base = scenarios["Base: every rule in this report"]
    naive = scenarios["Naive: count opening lots as buys at their stated cost"]
    assert base.lots["origin"].eq("fill").all()
    # The fixture's opening lot becomes an in-window buy on the naive line.
    assert naive.lots["lot"].nunique() == base.lots["lot"].nunique() + 1
    assert base.cash_shares
