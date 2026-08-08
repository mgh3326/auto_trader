"""Contract §2-1 — 같은 표 + 같은 계좌상태 → 같은 주문 (byte-deterministic).

Plus the B0 rule mapping itself: L1/L2 ladder, 물타기, R1/R2 50/50, and the §4
caps that bound them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.b0x import kill_switch as kill_switch_module
from scripts.b0x.derivation import (
    SELL_LADDER_FRACTIONS,
    Leg,
    SkipReason,
    derive_orders,
)
from scripts.b0x.envelope import CRYPTO_SIDECAR_ENVELOPE, EnvelopeNotLocked
from scripts.b0x.state import B0XPosition, LaneAccountState
from scripts.b0x.table_source import PolicyTable, load_policy_table
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
ENVELOPE = CRYPTO_SIDECAR_ENVELOPE


def _table(tmp_path: Path, rows: list[dict]) -> PolicyTable:
    write_table(
        tmp_path, make_payload(rows=rows, generated_at=NOW - dt.timedelta(hours=1))
    )
    table = load_policy_table(market="crypto", now=NOW, table_dir=tmp_path)
    assert isinstance(table, PolicyTable)
    return table


def _state(**kwargs) -> LaneAccountState:
    base = {
        "lane": "test",
        "quote_currency": "USDT",
        "cash": Decimal("1000"),
    }
    return LaneAccountState(**{**base, **kwargs})


def _derive(table: PolicyTable, state: LaneAccountState, **kwargs):
    decision = kill_switch_module.evaluate(state=state, envelope=ENVELOPE)
    return derive_orders(
        table=table,
        state=state,
        envelope=ENVELOPE,
        kill_switch=decision,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_table_same_state_gives_byte_identical_orders(tmp_path: Path) -> None:
    table = _table(
        tmp_path,
        [
            make_row(
                symbol="KRW-BTC", previous_close="100", buy_l1="97", sell_r1="105"
            ),
            make_row(symbol="KRW-ETH", previous_close="50", buy_l1="48", buy_l2="46"),
            make_row(symbol="KRW-SOL", previous_close="10", buy_l1="9.7"),
        ],
    )
    state = _state()

    first = _derive(table, state)
    second = _derive(table, state)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.derivation_hash() == second.derivation_hash()
    assert first.cycle_id == second.cycle_id
    assert [order.order_key for order in first.orders] == [
        order.order_key for order in second.orders
    ]


def test_a_changed_account_state_changes_the_cycle_id(tmp_path: Path) -> None:
    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    first = _derive(table, _state())
    second = _derive(table, _state(cash=Decimal("999")))
    assert first.cycle_id != second.cycle_id


def test_order_keys_are_unique_within_a_cycle(tmp_path: Path) -> None:
    table = _table(
        tmp_path,
        [
            make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97", buy_l2="94"),
            make_row(symbol="KRW-ETH", previous_close="100", buy_l1="97", buy_l2="94"),
        ],
    )
    result = _derive(table, _state())
    keys = [order.order_key for order in result.orders]
    assert len(keys) == len(set(keys))


def test_rows_are_consumed_in_lexicographic_order(tmp_path: Path) -> None:
    """The scarce-cap tie-break must be a function of the input, not of order."""

    table = _table(
        tmp_path,
        [
            make_row(symbol="KRW-ZZZ", previous_close="100", buy_l1="97"),
            make_row(symbol="KRW-AAA", previous_close="100", buy_l1="97"),
            make_row(symbol="KRW-MMM", previous_close="100", buy_l1="97"),
        ],
    )
    result = _derive(table, _state())
    # 일 신규 ≤ 2 → the two lexicographically-first symbols win, every time.
    admitted = [order.symbol for order in result.orders]
    assert admitted == ["KRW-AAA", "KRW-MMM"]
    blocked = [s for s in result.skipped if s.reason == SkipReason.DAILY_NEW_ENTRY_CAP]
    assert [s.symbol for s in blocked] == ["KRW-ZZZ"]


# ---------------------------------------------------------------------------
# B0 rule mapping
# ---------------------------------------------------------------------------


def test_new_entry_emits_the_l1_l2_ladder(tmp_path: Path) -> None:
    table = _table(
        tmp_path,
        [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97", buy_l2="94")],
    )
    result = _derive(table, _state())
    legs = [(o.leg, o.table_price, o.notional) for o in result.orders]
    assert legs == [
        (Leg.BUY_L1, Decimal("97"), Decimal("10")),
        (Leg.BUY_L2, Decimal("94"), Decimal("10")),
    ]
    # Both legs are one *entry* against the daily cap.
    assert all(o.price_ratio < 1 for o in result.orders)


def test_price_ratio_is_the_dimensionless_transfer_form(tmp_path: Path) -> None:
    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="200", buy_l1="194")]
    )
    result = _derive(table, _state())
    assert result.orders[0].price_ratio == Decimal("0.97")


def test_missing_buy_l2_is_recorded_not_invented(tmp_path: Path) -> None:
    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    result = _derive(table, _state())
    assert [o.leg for o in result.orders] == [Leg.BUY_L1]
    l2_skip = [s for s in result.skipped if s.leg == Leg.BUY_L2]
    assert l2_skip and l2_skip[0].reason == SkipReason.MISSING_LEVEL


def test_sell_ladder_is_50_50_of_the_held_quantity(tmp_path: Path) -> None:
    table = _table(
        tmp_path,
        [
            make_row(
                symbol="KRW-BTC",
                previous_close="100",
                buy_l1="97",
                sell_r1="105",
                sell_r2="110",
            )
        ],
    )
    state = _state(
        positions=(
            B0XPosition(
                symbol="KRW-BTC",
                quantity=Decimal("2"),
                average_price=Decimal("90"),
                invested_notional=Decimal("10"),
                entry_count=1,
            ),
        )
    )
    result = _derive(table, state)
    sells = [o for o in result.orders if o.side == "sell"]
    assert [(o.leg, o.quantity_fraction) for o in sells] == list(SELL_LADDER_FRACTIONS)
    assert all("SELL_SIDE_MODEL_MISMATCH" in o.labels for o in sells)
    assert sum(o.quantity_fraction for o in sells) == Decimal("1.0")


def test_sell_rung_below_the_loss_guard_floor_is_refused(tmp_path: Path) -> None:
    """average 100 x 1.01 = 101 floor; a 100.5 rung would book a loss."""

    table = _table(
        tmp_path,
        [
            make_row(
                symbol="KRW-BTC",
                previous_close="100",
                buy_l1="97",
                sell_r1="100.5",
                sell_r2="120",
            )
        ],
    )
    state = _state(
        positions=(
            B0XPosition(
                symbol="KRW-BTC",
                quantity=Decimal("1"),
                average_price=Decimal("100"),
                invested_notional=Decimal("10"),
                entry_count=1,
            ),
        )
    )
    result = _derive(table, state)
    refused = [
        s for s in result.skipped if s.reason == SkipReason.BELOW_LOSS_GUARD_FLOOR
    ]
    assert [s.leg for s in refused] == ["sell_r1"]
    assert [o.leg for o in result.orders if o.side == "sell"] == ["sell_r2"]


def test_sell_rung_not_above_close_is_refused(tmp_path: Path) -> None:
    table = _table(
        tmp_path,
        [
            make_row(
                symbol="KRW-BTC",
                previous_close="100",
                buy_l1="97",
                sell_r1="99",
                sell_r2="105",
            )
        ],
    )
    state = _state(
        positions=(
            B0XPosition(
                symbol="KRW-BTC",
                quantity=Decimal("1"),
                average_price=Decimal("10"),
                invested_notional=Decimal("10"),
                entry_count=1,
            ),
        )
    )
    result = _derive(table, state)
    assert any(
        s.reason == SkipReason.SELL_LEVEL_NOT_ABOVE_CLOSE for s in result.skipped
    )


def test_averaging_uses_b0x_position_not_the_tables_real_account_block(
    tmp_path: Path,
) -> None:
    """The table's precomputed averaging_math describes the operator's account.
    B0-X must recompute against its own inventory."""

    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    state = _state(
        positions=(
            B0XPosition(
                symbol="KRW-BTC",
                quantity=Decimal("1"),
                average_price=Decimal("200"),  # deeply underwater
                invested_notional=Decimal("10"),
                entry_count=1,
            ),
        )
    )
    result = _derive(table, state)
    adds = [o for o in result.orders if o.leg == Leg.AVERAGING]
    assert len(adds) == 1
    assert adds[0].detail["rule"] == "b0_averaging_down"
    assert adds[0].detail["k"] == "0.05"
    # Capped by the per-order envelope, never by the raw A(k) figure.
    assert adds[0].notional == Decimal("10")
    assert adds[0].detail["capped"] is True


def test_averaging_already_satisfied_emits_nothing(tmp_path: Path) -> None:
    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    state = _state(
        positions=(
            B0XPosition(
                symbol="KRW-BTC",
                quantity=Decimal("1"),
                average_price=Decimal("50"),  # far in profit
                invested_notional=Decimal("10"),
                entry_count=1,
            ),
        )
    )
    result = _derive(table, state)
    assert not [o for o in result.orders if o.side == "buy"]
    assert any(
        s.reason == SkipReason.AVERAGING_ALREADY_SATISFIED for s in result.skipped
    )


def test_insufficient_history_rows_are_skipped_with_a_reason(tmp_path: Path) -> None:
    table = _table(
        tmp_path,
        [
            make_row(
                symbol="KRW-BTC",
                previous_close="",
                buy_l1=None,
                insufficient_history=True,
            )
        ],
    )
    result = _derive(table, _state())
    assert result.orders == ()
    assert result.skipped[0].reason == SkipReason.INSUFFICIENT_HISTORY


# ---------------------------------------------------------------------------
# §4 caps
# ---------------------------------------------------------------------------


def test_per_symbol_total_cap_blocks_further_adds(tmp_path: Path) -> None:
    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    state = _state(
        positions=(
            B0XPosition(
                symbol="KRW-BTC",
                quantity=Decimal("1"),
                average_price=Decimal("200"),
                invested_notional=Decimal("50"),  # cap already deployed
                entry_count=5,
            ),
        )
    )
    result = _derive(table, state)
    assert not [o for o in result.orders if o.side == "buy"]
    assert any(s.reason == SkipReason.SYMBOL_TOTAL_CAP for s in result.skipped)


def test_concurrent_position_cap_blocks_a_new_symbol(tmp_path: Path) -> None:
    table = _table(
        tmp_path, [make_row(symbol="KRW-ZZZ", previous_close="100", buy_l1="97")]
    )
    held = tuple(
        B0XPosition(
            symbol=f"KRW-{name}",
            quantity=Decimal("1"),
            average_price=Decimal("50"),
            invested_notional=Decimal("10"),
            entry_count=1,
        )
        for name in ("AAA", "BBB", "CCC")
    )
    result = _derive(table, _state(positions=held))
    assert result.orders == ()
    assert any(s.reason == SkipReason.CONCURRENT_POSITION_CAP for s in result.skipped)


def test_insufficient_cash_blocks_the_leg(tmp_path: Path) -> None:
    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    result = _derive(table, _state(cash=Decimal("1")))
    assert result.orders == ()
    assert any(s.reason == SkipReason.INSUFFICIENT_CASH for s in result.skipped)


def test_lane_universe_restricts_the_rows_consumed(tmp_path: Path) -> None:
    table = _table(
        tmp_path,
        [
            make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97"),
            make_row(symbol="KRW-DOGE", previous_close="1", buy_l1="0.97"),
        ],
    )
    result = _derive(table, _state(), lane_universe=frozenset({"KRW-BTC"}))
    assert {o.symbol for o in result.orders} == {"KRW-BTC"}
    assert all(s.symbol != "KRW-DOGE" for s in result.skipped)


def test_synthetic_lane_uses_b0_sizing_not_the_envelope(tmp_path: Path) -> None:
    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    result = _derive(table, _state(cash=Decimal("1000000")), apply_envelope=False)
    # sizing.new_entry_notional_krw = 10000, not the 10 USDT sidecar cap.
    assert result.orders[0].notional == Decimal("10000")


def test_derivation_refuses_a_widened_envelope(tmp_path: Path) -> None:
    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    state = _state()
    # Internally consistent (so __post_init__ accepts it) but not the contract
    # value — exactly the shape a well-meaning widening would take.
    widened = replace(
        ENVELOPE,
        per_order_notional=Decimal("1000"),
        per_symbol_total_notional=Decimal("5000"),
    )
    decision = kill_switch_module.evaluate(state=state, envelope=ENVELOPE)
    with pytest.raises(EnvelopeNotLocked):
        derive_orders(table=table, state=state, envelope=widened, kill_switch=decision)


def test_same_day_reentry_still_respects_the_concurrent_position_cap(
    tmp_path: Path,
) -> None:
    """The daily-entry counter is exempt on re-entry; the position cap is not.

    Without this the lane could exceed 동시 포지션 ≤ 3 by re-entering a symbol it
    had already entered and exited earlier the same UTC day.
    """

    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    held = tuple(
        B0XPosition(
            symbol=f"KRW-{name}",
            quantity=Decimal("1"),
            average_price=Decimal("50"),
            invested_notional=Decimal("10"),
            entry_count=1,
        )
        for name in ("AAA", "BBB", "CCC")
    )
    state = _state(
        positions=held,
        # KRW-BTC was entered earlier today, so the daily counter would exempt it.
        new_entry_symbols_today=("KRW-BTC",),
    )
    result = _derive(table, state)
    assert result.orders == ()
    assert any(s.reason == SkipReason.CONCURRENT_POSITION_CAP for s in result.skipped)


def test_same_day_reentry_is_allowed_when_there_is_position_headroom(
    tmp_path: Path,
) -> None:
    """The exemption must still work — re-entry is not a *new* daily decision."""

    table = _table(
        tmp_path, [make_row(symbol="KRW-BTC", previous_close="100", buy_l1="97")]
    )
    state = _state(
        # Daily cap (2) already saturated by two other symbols...
        new_entry_symbols_today=("KRW-BTC", "KRW-ETH"),
    )
    result = _derive(table, state)
    # ...but KRW-BTC is one of them, so its re-entry is not blocked.
    assert [o.symbol for o in result.orders] == ["KRW-BTC"]


def test_admitting_entries_consumes_the_position_cap_within_one_cycle(
    tmp_path: Path,
) -> None:
    """Caps are consumed as the cycle walks rows, not evaluated against the
    starting state only."""

    table = _table(
        tmp_path,
        [
            make_row(symbol="KRW-AAA", previous_close="100", buy_l1="97"),
            make_row(symbol="KRW-BBB", previous_close="100", buy_l1="97"),
            make_row(symbol="KRW-CCC", previous_close="100", buy_l1="97"),
        ],
    )
    held = (
        B0XPosition(
            symbol="KRW-HELD",
            quantity=Decimal("1"),
            average_price=Decimal("50"),
            invested_notional=Decimal("10"),
            entry_count=1,
        ),
    )
    # 1 held + max 3 concurrent => room for exactly 2 more, and the daily cap is
    # also 2, so both bind at the same place.
    result = _derive(table, _state(positions=held))
    assert [o.symbol for o in result.orders] == ["KRW-AAA", "KRW-BBB"]
    assert any(
        s.symbol == "KRW-CCC"
        and s.reason
        in {SkipReason.CONCURRENT_POSITION_CAP, SkipReason.DAILY_NEW_ENTRY_CAP}
        for s in result.skipped
    )
