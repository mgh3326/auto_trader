"""ROB-1470 fill attribution: which ledger rows are a sleeve's fills (pure)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.trading import InstrumentType
from app.services.strategy_sleeves import store
from tests.services.strategy_sleeves.support import D, order, sleeve

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)  # 10:00 KST


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


def _ref(rung_id: int, *, created: datetime = T0, valid_until=None, **kw):
    return store._RungRef(
        order(rung_id, symbol="005930", **kw),
        created,
        valid_until or T0 + timedelta(hours=12),
    )


def test_a_matching_row_becomes_the_rungs_fill() -> None:
    fills, skipped = store.attribute_fills(
        rows=[_row(1)], by_order_id={"123": [_ref(10)]}, sleeve=_kr_sleeve()
    )
    assert [(f.ledger_id, f.rung_id, f.quantity) for f in fills] == [(1, 10, D("3"))]
    assert not skipped


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
    fills, skipped = store.attribute_fills(
        rows=[_row(1, **override)], by_order_id={"123": [_ref(10)]}, sleeve=_kr_sleeve()
    )
    assert fills == []
    assert skipped == {reason: 1}


def test_a_kis_order_number_reused_on_a_later_day_is_another_order() -> None:
    later = T0 + timedelta(days=1)
    ref = _ref(10, valid_until=T0 + timedelta(days=3))
    fills, skipped = store.attribute_fills(
        rows=[_row(1), _row(2, filled_at=later)],
        by_order_id={"123": [ref]},
        sleeve=_kr_sleeve(),
    )
    assert [f.ledger_id for f in fills] == [1]
    assert skipped == {"later_day_order_id_reuse": 1}


def test_a_fill_after_the_proposals_valid_until_day_is_not_its_fill() -> None:
    fills, skipped = store.attribute_fills(
        rows=[_row(1, filled_at=T0 + timedelta(days=2))],
        by_order_id={"123": [_ref(10)]},
        sleeve=_kr_sleeve(),
    )
    assert fills == []
    assert skipped == {"no_matching_order": 1}


def test_the_latest_proposal_created_before_the_fill_owns_a_shared_number() -> None:
    old = _ref(10, created=T0 - timedelta(days=2), valid_until=T0 + timedelta(days=1))
    new = _ref(11, created=T0)
    fills, _skipped = store.attribute_fills(
        rows=[_row(1)], by_order_id={"123": [old, new]}, sleeve=_kr_sleeve()
    )
    assert [f.rung_id for f in fills] == [11]


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
    fills, skipped = store.attribute_fills(
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
