"""ROB-1470 fill attribution: which ledger rows are a sleeve's fills (pure)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.trading import InstrumentType
from app.services.execution_ledger.supersede import order_trading_day
from app.services.strategy_sleeves import store
from tests.services.strategy_sleeves.support import D, order, sleeve

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)  # 10:00 KST
DAY0 = order_trading_day("kis", "equity_kr", T0)  # T0's KST trading day


def _row(ledger_id: int, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": ledger_id,
        "broker": "kis",
        "account_mode": "live",
        "instrument_type": InstrumentType.equity_kr,
        "symbol": "005930",
        "raw_symbol": "005930",
        "side": "buy",
        "broker_order_id": "0000123",
        "filled_qty": D("3"),
        "filled_price": D("70000"),
        "fee_amount": None,
        "fee_currency": None,
        "filled_at": T0 + timedelta(minutes=5),
        "currency": "KRW",
    }
    return SimpleNamespace(**{**values, **overrides})


def _kr_sleeve():
    return sleeve(rule_id="rob1458.kr_cut_minus20", market="equity_kr", currency="KRW")


def _ref(rung_id: int, *, created: datetime = T0, order_day: Any = DAY0, **kw):
    return store._RungRef(order(rung_id, symbol="005930", **kw), created, order_day)


def test_a_matching_row_becomes_the_rungs_fill() -> None:
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1)], by_order_id={"123": [_ref(10)]}, sleeve=_kr_sleeve()
    )
    assert [(f.ledger_id, f.rung_id, f.quantity) for f in fills] == [(1, 10, D("3"))]
    assert not skipped
    assert not unattributed


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"currency": "USD"}, "currency_mismatch"),
        ({"side": "sell"}, "no_matching_order"),
        ({"symbol": "000660", "raw_symbol": "000660"}, "no_matching_order"),
        ({"filled_at": T0 - timedelta(minutes=1)}, "no_matching_order"),
        ({"broker_order_id": "999"}, "no_matching_order"),
        ({"filled_qty": D("0")}, "unreadable_fill"),
    ],
)
def test_a_row_that_is_not_the_rungs_order_is_skipped(override, reason) -> None:
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1, **override)], by_order_id={"123": [_ref(10)]}, sleeve=_kr_sleeve()
    )
    assert fills == []
    assert skipped == {reason: 1}
    assert not unattributed


def test_a_kis_fill_on_another_day_than_the_rungs_order_is_not_its_fill() -> None:
    """KIS reuses order numbers across days: only the rung's own day counts."""
    later = T0 + timedelta(days=1)
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1), _row(2, filled_at=later)],
        by_order_id={"123": [_ref(10)]},
        sleeve=_kr_sleeve(),
    )
    assert [f.ledger_id for f in fills] == [1]
    assert skipped == {"order_id_reused_on_another_day": 1}
    assert not unattributed


def test_a_rung_with_no_own_fill_never_takes_a_later_days_fill() -> None:
    """The reviewer's case: nothing on the rung's day, a reused number later."""
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(2, filled_at=T0 + timedelta(days=1))],
        by_order_id={"123": [_ref(10)]},
        sleeve=_kr_sleeve(),
    )
    assert fills == []
    assert skipped == {"order_id_reused_on_another_day": 1}
    assert not unattributed


def test_a_kis_rung_without_a_known_order_day_gets_no_fill_and_flags_pnl() -> None:
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1)],
        by_order_id={"123": [_ref(10, order_day=None)]},
        sleeve=_kr_sleeve(),
    )
    assert fills == []
    assert skipped == {"kis_order_day_unknown": 1}
    assert unattributed == {10}


def test_a_shared_number_with_an_unknown_day_rung_is_ambiguous() -> None:
    """A day-proven rung does not win while another candidate's day is unknown."""
    proven = _ref(10)
    unknown = _ref(11, created=T0 - timedelta(days=1), order_day=None)
    fills, skipped, unattributed = store.attribute_fills(
        rows=[_row(1)], by_order_id={"123": [proven, unknown]}, sleeve=_kr_sleeve()
    )
    assert fills == []
    assert skipped == {"kis_order_day_unknown": 1}
    assert unattributed == {11}


def test_the_rung_whose_order_day_matches_owns_a_shared_number() -> None:
    old = _ref(
        10,
        created=T0 - timedelta(days=2),
        order_day=order_trading_day("kis", "equity_kr", T0 - timedelta(days=3)),
    )
    new = _ref(11, created=T0)
    fills, _skipped, _unattributed = store.attribute_fills(
        rows=[_row(1)], by_order_id={"123": [old, new]}, sleeve=_kr_sleeve()
    )
    assert [f.rung_id for f in fills] == [11]


def test_a_non_kis_order_keeps_its_fills_on_every_day() -> None:
    """The later-day rule is KIS only: an Upbit order may fill across days."""
    crypto = sleeve(
        rule_id="rob1458.crypto_b3",
        account_mode="upbit",
        market="crypto",
        currency="KRW",
    )
    ref = store._RungRef(order(10, symbol="KRW-BTC"), T0, None)

    def upbit_row(ledger_id: int, filled_at: datetime) -> SimpleNamespace:
        return _row(
            ledger_id,
            broker="upbit",
            instrument_type=InstrumentType.crypto,
            symbol="BTC",
            raw_symbol="KRW-BTC",
            broker_order_id="uuid-1",
            filled_at=filled_at,
        )

    fills, skipped, unattributed = store.attribute_fills(
        rows=[upbit_row(1, T0), upbit_row(2, T0 + timedelta(days=2))],
        by_order_id={"uuid-1": [ref]},
        sleeve=crypto,
    )
    assert [f.ledger_id for f in fills] == [1, 2]
    assert not skipped and not unattributed


def _send(order_no: str, trade_date: datetime, *, idem=None, corr=None):
    return SimpleNamespace(
        order_no=order_no,
        idempotency_key=idem,
        correlation_id=corr,
        trade_date=trade_date,
    )


def test_a_rungs_order_day_comes_from_its_own_send_row() -> None:
    rung = store._SendKeys(10, "123", "idem-10", "corr-10")
    days = store.rung_order_days(
        rungs=[rung],
        send_rows=[
            _send("0000123", T0, idem="idem-10"),
            # Same number, another order (other key) on a later day: not ours.
            _send("123", T0 + timedelta(days=1), idem="idem-99"),
        ],
        instrument="equity_kr",
    )
    assert days == {10: DAY0}


@pytest.mark.parametrize(
    "rows",
    [
        [],  # no send row
        [_send("999", T0, idem="idem-10")],  # key matches, number does not
        [  # two days for one rung: ambiguous
            _send("123", T0, idem="idem-10"),
            _send("123", T0 + timedelta(days=1), corr="corr-10"),
        ],
    ],
)
def test_no_unambiguous_send_row_means_no_order_day(rows) -> None:
    rung = store._SendKeys(10, "123", "idem-10", "corr-10")
    assert (
        store.rung_order_days(rungs=[rung], send_rows=rows, instrument="equity_kr")
        == {}
    )


def test_crypto_symbol_forms_and_other_currency_fees() -> None:
    crypto = sleeve(
        rule_id="rob1458.crypto_b3",
        account_mode="upbit",
        market="crypto",
        currency="KRW",
    )
    ref = store._RungRef(order(10, symbol="KRW-BTC"), T0, None)
    row = _row(
        1,
        broker="upbit",
        instrument_type=InstrumentType.crypto,
        symbol="BTC",
        raw_symbol="KRW-BTC",
        broker_order_id="uuid-1",
        fee_amount=D("5"),
        fee_currency="BTC",
    )
    fills, skipped, _unattributed = store.attribute_fills(
        rows=[row], by_order_id={"uuid-1": [ref]}, sleeve=crypto
    )
    assert [(f.symbol, f.fee) for f in fills] == [("KRW-BTC", D("0"))]
    assert skipped == {"fee_other_currency": 1}


def test_us_symbols_compare_in_the_db_dot_form() -> None:
    assert store.sleeve_symbol("brk/b", "equity_us") == "BRK.B"
    assert store.sleeve_symbol("BRK-B", "equity_us") == "BRK.B"
    assert store.sleeve_symbol("btc", "crypto") == "KRW-BTC"
    assert store.sleeve_symbol("KRW-BTC", "crypto") == "KRW-BTC"
    assert store.sleeve_symbol(" 005930 ", "equity_kr") == "005930"
