from datetime import UTC, datetime

import pytest

from app.services.trade_journal.aggregates import Fill, pair_fills_fifo


def _fill(side, qty, price, day, *, fee=0.0, item="i", corr="c"):
    return Fill(
        market="kr",
        symbol="005930",
        account="acct",
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        ts=datetime(2026, 6, day, tzinfo=UTC),
        item_uuid=item,
        correlation_id=corr,
        source="kis",
    )


def test_single_round_trip():
    trades = pair_fills_fifo([_fill("buy", 10, 100.0, 1), _fill("sell", 10, 110.0, 3)])
    assert len(trades) == 1
    t = trades[0]
    assert t.qty == 10
    assert t.entry_price == 100.0
    assert t.exit_price == 110.0
    assert t.pnl_pct == pytest.approx(0.10)
    assert t.pnl_abs == pytest.approx(100.0)
    assert t.entry_ts.day == 1 and t.exit_ts.day == 3


def test_two_buys_one_sell_weighted_entry():
    trades = pair_fills_fifo(
        [
            _fill("buy", 10, 100.0, 1),
            _fill("buy", 10, 120.0, 2),
            _fill("sell", 20, 130.0, 3),
        ]
    )
    assert len(trades) == 1
    assert trades[0].entry_price == pytest.approx(110.0)  # qty-weighted
    assert trades[0].qty == 20


def test_partial_close_leaves_open_residual():
    trades = pair_fills_fifo([_fill("buy", 10, 100.0, 1), _fill("sell", 4, 130.0, 3)])
    assert len(trades) == 1
    assert trades[0].qty == 4  # only closed portion counts


def test_oversell_without_prior_buy_is_dropped():
    assert pair_fills_fifo([_fill("sell", 5, 100.0, 1)]) == []


def test_fees_reduce_pnl_abs():
    trades = pair_fills_fifo(
        [
            _fill("buy", 10, 100.0, 1, fee=5.0),
            _fill("sell", 10, 110.0, 3, fee=5.0),
        ]
    )
    assert trades[0].fees == pytest.approx(10.0)
    assert trades[0].pnl_abs == pytest.approx(90.0)  # 100 gross - 10 fees
