"""§2-4 kill switch firing, and the shadow lane's touch rule semantics."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from scripts.b0x import kill_switch as kill_switch_module
from scripts.b0x.crypto import shadow
from scripts.b0x.derivation import SkipReason, derive_orders
from scripts.b0x.envelope import CRYPTO_SIDECAR_ENVELOPE
from scripts.b0x.kill_switch import CapStatus, KillReason
from scripts.b0x.state import B0XPosition, LaneAccountState
from scripts.b0x.table_source import PolicyTable, load_policy_table
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
ENVELOPE = CRYPTO_SIDECAR_ENVELOPE


def _state(**kwargs) -> LaneAccountState:
    base = {"lane": "test", "quote_currency": "USDT", "cash": Decimal("1000")}
    return LaneAccountState(**{**base, **kwargs})


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_kill_fires_exactly_at_the_daily_loss_budget() -> None:
    just_under = kill_switch_module.evaluate(
        state=_state(realized_pnl_today=Decimal("-4.99")), envelope=ENVELOPE
    )
    assert just_under.allow_new_orders is True
    assert not just_under.tripped

    at_limit = kill_switch_module.evaluate(
        state=_state(realized_pnl_today=Decimal("-5")), envelope=ENVELOPE
    )
    assert at_limit.allow_new_orders is False
    assert at_limit.kill_reasons == (KillReason.DAILY_LOSS_BUDGET_REACHED,)


def test_profit_never_trips_the_kill() -> None:
    decision = kill_switch_module.evaluate(
        state=_state(realized_pnl_today=Decimal("500")), envelope=ENVELOPE
    )
    assert decision.allow_new_orders is True


def test_kill_produces_an_operator_notice() -> None:
    decision = kill_switch_module.evaluate(
        state=_state(realized_pnl_today=Decimal("-6")), envelope=ENVELOPE
    )
    notice = decision.operator_notice(lane="binance_spot_demo_sidecar")
    assert notice is not None
    assert "KILL SWITCH" in notice
    assert "재개는 운영자 결정" in notice
    assert (
        kill_switch_module.evaluate(state=_state(), envelope=ENVELOPE).operator_notice(
            lane="x"
        )
        is None
    )


def test_a_tripped_kill_yields_zero_orders(tmp_path: Path) -> None:
    write_table(
        tmp_path,
        make_payload(
            rows=[make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")],
            generated_at=NOW - dt.timedelta(hours=1),
        ),
    )
    table = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
    assert isinstance(table, PolicyTable)

    state = _state(realized_pnl_today=Decimal("-9"))
    decision = kill_switch_module.evaluate(state=state, envelope=ENVELOPE)
    result = derive_orders(
        table=table, state=state, envelope=ENVELOPE, kill_switch=decision
    )
    assert result.orders == ()
    assert result.skipped[0].reason == SkipReason.KILL_SWITCH_ACTIVE


def test_caps_are_reported_but_do_not_stop_the_lane() -> None:
    held = tuple(
        B0XPosition(
            symbol=f"KRW-{name}",
            quantity=Decimal("1"),
            average_price=Decimal("1"),
            invested_notional=Decimal("1"),
            entry_count=1,
        )
        for name in ("AAA", "BBB", "CCC")
    )
    decision = kill_switch_module.evaluate(
        state=_state(positions=held, new_entry_symbols_today=("KRW-AAA", "KRW-BBB")),
        envelope=ENVELOPE,
    )
    assert decision.allow_new_orders is True
    assert set(decision.cap_status) == {
        CapStatus.CONCURRENT_POSITIONS_SATURATED,
        CapStatus.DAILY_NEW_ENTRY_CAP_SATURATED,
    }


def test_all_tripped_reasons_are_reported_without_short_circuit() -> None:
    held = tuple(
        B0XPosition(
            symbol=f"KRW-{n}",
            quantity=Decimal("1"),
            average_price=Decimal("1"),
            invested_notional=Decimal("1"),
            entry_count=1,
        )
        for n in ("A", "B", "C")
    )
    decision = kill_switch_module.evaluate(
        state=_state(
            positions=held,
            new_entry_symbols_today=("KRW-A", "KRW-B"),
            realized_pnl_today=Decimal("-10"),
        ),
        envelope=ENVELOPE,
    )
    assert decision.kill_reasons == (KillReason.DAILY_LOSS_BUDGET_REACHED,)
    assert len(decision.cap_status) == 2


# ---------------------------------------------------------------------------
# Touch rule
# ---------------------------------------------------------------------------


def _frame(rows: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp(stamp),
                "high": float(high),
                "low": float(low),
                "close": float(close),
            }
            for stamp, high, low, close in rows
        ]
    )


def test_forming_bar_is_never_a_fill() -> None:
    """Bars are KST-naive; a 4h bar opening at 12:00 KST closes at 16:00 KST."""

    now = dt.datetime(2026, 8, 8, 5, 0, tzinfo=dt.UTC)  # 14:00 KST — mid-bar
    frame = _frame(
        [
            ("2026-08-08 08:00:00", "110", "90", "100"),  # closed 12:00 KST
            ("2026-08-08 12:00:00", "110", "90", "100"),  # still forming
        ]
    )
    bars = shadow.completed_bars(frame, now=now)
    assert len(bars) == 1
    assert bars[0].open_time == dt.datetime(2026, 8, 7, 23, 0, tzinfo=dt.UTC)


def test_buy_fills_when_a_bar_low_reaches_the_limit() -> None:
    order = shadow.VirtualOrder(
        order_key="k",
        symbol="KRW-BTC",
        side="buy",
        leg="buy_l1",
        price=Decimal("97"),
        quantity=Decimal("1"),
        notional=Decimal("97"),
        placed_at=NOW,
        cycle_id="c",
    )
    grazed = [
        shadow.Bar(
            NOW + dt.timedelta(hours=4), Decimal("105"), Decimal("97"), Decimal("100")
        )
    ]
    assert shadow.first_touch(order, grazed) is not None

    missed = [
        shadow.Bar(
            NOW + dt.timedelta(hours=4), Decimal("105"), Decimal("98"), Decimal("100")
        )
    ]
    assert shadow.first_touch(order, missed) is None


def test_sell_fills_when_a_bar_high_reaches_the_limit() -> None:
    order = shadow.VirtualOrder(
        order_key="k",
        symbol="KRW-BTC",
        side="sell",
        leg="sell_r1",
        price=Decimal("105"),
        quantity=Decimal("1"),
        notional=Decimal("105"),
        placed_at=NOW,
        cycle_id="c",
    )
    hit = [
        shadow.Bar(
            NOW + dt.timedelta(hours=4), Decimal("105"), Decimal("95"), Decimal("100")
        )
    ]
    assert shadow.first_touch(order, hit) is not None
    short = [
        shadow.Bar(
            NOW + dt.timedelta(hours=4), Decimal("104"), Decimal("95"), Decimal("100")
        )
    ]
    assert shadow.first_touch(order, short) is None


def test_bars_before_placement_cannot_fill_an_order() -> None:
    """Otherwise the sim would fill against history it could not have traded."""

    order = shadow.VirtualOrder(
        order_key="k",
        symbol="KRW-BTC",
        side="buy",
        leg="buy_l1",
        price=Decimal("97"),
        quantity=Decimal("1"),
        notional=Decimal("97"),
        placed_at=NOW,
        cycle_id="c",
    )
    past = [
        shadow.Bar(
            NOW - dt.timedelta(hours=8), Decimal("105"), Decimal("50"), Decimal("100")
        )
    ]
    assert shadow.first_touch(order, past) is None


def test_earliest_qualifying_bar_wins() -> None:
    order = shadow.VirtualOrder(
        order_key="k",
        symbol="KRW-BTC",
        side="buy",
        leg="buy_l1",
        price=Decimal("97"),
        quantity=Decimal("1"),
        notional=Decimal("97"),
        placed_at=NOW,
        cycle_id="c",
    )
    first = shadow.Bar(
        NOW + dt.timedelta(hours=4), Decimal("105"), Decimal("96"), Decimal("100")
    )
    later = shadow.Bar(
        NOW + dt.timedelta(hours=8), Decimal("105"), Decimal("90"), Decimal("100")
    )
    assert shadow.first_touch(order, [first, later]) is first


def test_buy_fill_charges_the_fee_and_opens_the_position() -> None:
    portfolio = shadow.VirtualPortfolio.seed(now=NOW)
    portfolio.open_orders = [
        shadow.VirtualOrder(
            order_key="k",
            symbol="KRW-BTC",
            side="buy",
            leg="buy_l1",
            price=Decimal("100"),
            quantity=Decimal("2"),
            notional=Decimal("200"),
            placed_at=NOW,
            cycle_id="c",
        )
    ]
    bars = {
        "KRW-BTC": [
            shadow.Bar(
                NOW + dt.timedelta(hours=4),
                Decimal("120"),
                Decimal("99"),
                Decimal("110"),
            )
        ]
    }
    fills = shadow.apply_fills(portfolio, bars)

    assert len(fills) == 1
    # Fill price is the limit, not the bar low — rule (2).
    assert fills[0].price == Decimal("100")
    assert fills[0].fee == Decimal("200") * shadow.UPBIT_KRW_FEE_RATE
    assert portfolio.cash == shadow.SEED_CASH_KRW - Decimal("200") - fills[0].fee
    assert portfolio.positions["KRW-BTC"].quantity == Decimal("2")
    assert portfolio.open_orders == []
    assert "KRW-BTC" in portfolio.new_entry_symbols_today


def test_sell_fill_realizes_pnl_net_of_fees() -> None:
    portfolio = shadow.VirtualPortfolio.seed(now=NOW)
    portfolio.positions["KRW-BTC"] = B0XPosition(
        symbol="KRW-BTC",
        quantity=Decimal("2"),
        average_price=Decimal("100"),
        invested_notional=Decimal("200"),
        entry_count=1,
    )
    portfolio.open_orders = [
        shadow.VirtualOrder(
            order_key="s",
            symbol="KRW-BTC",
            side="sell",
            leg="sell_r1",
            price=Decimal("110"),
            quantity=Decimal("1"),
            notional=Decimal("110"),
            placed_at=NOW,
            cycle_id="c",
        )
    ]
    bars = {
        "KRW-BTC": [
            shadow.Bar(
                NOW + dt.timedelta(hours=4),
                Decimal("115"),
                Decimal("100"),
                Decimal("112"),
            )
        ]
    }
    fills = shadow.apply_fills(portfolio, bars)

    fee = Decimal("110") * shadow.UPBIT_KRW_FEE_RATE
    assert fills[0].realized_pnl == Decimal("10") - fee
    assert portfolio.realized_pnl_today == Decimal("10") - fee
    assert portfolio.positions["KRW-BTC"].quantity == Decimal("1")


def test_full_exit_removes_the_position() -> None:
    portfolio = shadow.VirtualPortfolio.seed(now=NOW)
    portfolio.positions["KRW-BTC"] = B0XPosition(
        symbol="KRW-BTC",
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        invested_notional=Decimal("100"),
        entry_count=1,
    )
    portfolio.open_orders = [
        shadow.VirtualOrder(
            order_key="s",
            symbol="KRW-BTC",
            side="sell",
            leg="sell_r1",
            price=Decimal("110"),
            quantity=Decimal("1"),
            notional=Decimal("110"),
            placed_at=NOW,
            cycle_id="c",
        )
    ]
    shadow.apply_fills(
        portfolio,
        {
            "KRW-BTC": [
                shadow.Bar(
                    NOW + dt.timedelta(hours=4),
                    Decimal("115"),
                    Decimal("100"),
                    Decimal("112"),
                )
            ]
        },
    )
    assert "KRW-BTC" not in portfolio.positions


def test_untouched_orders_keep_resting() -> None:
    portfolio = shadow.VirtualPortfolio.seed(now=NOW)
    order = shadow.VirtualOrder(
        order_key="k",
        symbol="KRW-BTC",
        side="buy",
        leg="buy_l1",
        price=Decimal("50"),
        quantity=Decimal("1"),
        notional=Decimal("50"),
        placed_at=NOW,
        cycle_id="c",
    )
    portfolio.open_orders = [order]
    fills = shadow.apply_fills(
        portfolio,
        {
            "KRW-BTC": [
                shadow.Bar(
                    NOW + dt.timedelta(hours=4),
                    Decimal("120"),
                    Decimal("99"),
                    Decimal("110"),
                )
            ]
        },
    )
    assert fills == []
    assert portfolio.open_orders == [order]


def test_portfolio_round_trips_through_json() -> None:
    portfolio = shadow.VirtualPortfolio.seed(now=NOW)
    portfolio.positions["KRW-BTC"] = B0XPosition(
        symbol="KRW-BTC",
        quantity=Decimal("1.5"),
        average_price=Decimal("100.25"),
        invested_notional=Decimal("150.375"),
        entry_count=2,
    )
    portfolio.realized_pnl_today = Decimal("-3.5")
    restored = shadow.VirtualPortfolio.from_json(portfolio.to_json())
    assert restored.to_json() == portfolio.to_json()


def test_utc_day_roll_resets_daily_counters() -> None:
    portfolio = shadow.VirtualPortfolio.seed(now=NOW)
    portfolio.new_entry_symbols_today.add("KRW-BTC")
    portfolio.realized_pnl_today = Decimal("-4")
    assert portfolio.roll_utc_day(now=NOW + dt.timedelta(days=1)) is True
    assert portfolio.new_entry_symbols_today == set()
    assert portfolio.realized_pnl_today == Decimal("0")
    # Total P&L is a running figure and must survive the roll.
    assert portfolio.roll_utc_day(now=NOW + dt.timedelta(days=1)) is False


def test_place_derived_orders_replaces_rather_than_accumulates() -> None:
    from scripts.b0x.derivation import DerivedOrder

    portfolio = shadow.VirtualPortfolio.seed(now=NOW)
    portfolio.open_orders = [
        shadow.VirtualOrder(
            order_key="stale",
            symbol="KRW-BTC",
            side="buy",
            leg="buy_l1",
            price=Decimal("1"),
            quantity=Decimal("1"),
            notional=Decimal("1"),
            placed_at=NOW - dt.timedelta(days=1),
            cycle_id="old",
        )
    ]
    fresh = (
        DerivedOrder(
            sequence=0,
            symbol="KRW-BTC",
            side="buy",
            leg="buy_l1",
            price_ratio=Decimal("0.97"),
            table_price=Decimal("97"),
            table_previous_close=Decimal("100"),
            notional=Decimal("10000"),
            quantity_fraction=None,
            basis="A_buy_side.buy_l1.price",
            labels=(),
            detail={},
            order_key="new",
        ),
    )
    placed = shadow.place_derived_orders(portfolio, fresh, now=NOW, cycle_id="c")
    assert [order.order_key for order in portfolio.open_orders] == ["new"]
    assert placed[0].quantity == Decimal("10000") / Decimal("97")
