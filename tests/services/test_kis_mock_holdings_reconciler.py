"""Pure tests for KIS mock holdings-delta reconciler (ROB-102)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.kis_mock_holdings_reconciler import (
    HoldingsSnapshot,
    LedgerOrderInput,
    ReconcilerThresholds,
    classify_orders,
)


def _now() -> datetime:
    return datetime(2026, 5, 4, 9, 30, tzinfo=UTC)


def _order(
    *,
    side: str = "buy",
    state: str = "accepted",
    ordered_qty: Decimal = Decimal("10"),
    baseline: Decimal | None = Decimal("0"),
    accepted_age_sec: int = 0,
) -> LedgerOrderInput:
    return LedgerOrderInput(
        ledger_id=1,
        symbol="005930",
        side=side,
        ordered_qty=ordered_qty,
        lifecycle_state=state,
        holdings_baseline_qty=baseline,
        accepted_at=_now() - timedelta(seconds=accepted_age_sec),
    )


def _snap(qty: Decimal) -> HoldingsSnapshot:
    return HoldingsSnapshot(symbol="005930", quantity=qty, taken_at=_now())


@pytest.mark.unit
def test_full_buy_fill_detected():
    proposals = classify_orders(
        orders=[_order(baseline=Decimal("5"))],
        holdings={"005930": _snap(Decimal("15"))},  # delta = +10
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    assert len(proposals) == 1
    assert proposals[0].next_state == "fill"
    assert proposals[0].reason_code == "fill_detected"


@pytest.mark.unit
def test_partial_buy_fill_detected():
    proposals = classify_orders(
        orders=[_order(baseline=Decimal("5"))],
        holdings={"005930": _snap(Decimal("9"))},  # delta = +4 of ordered 10
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    assert proposals[0].next_state == "fill"
    assert proposals[0].reason_code == "partial_fill_detected"


@pytest.mark.unit
def test_buy_no_delta_recent_marks_pending():
    proposals = classify_orders(
        orders=[_order(baseline=Decimal("5"), accepted_age_sec=10)],
        holdings={"005930": _snap(Decimal("5"))},  # no delta
        thresholds=ReconcilerThresholds(
            pending_threshold_sec=60, stale_threshold_sec=1800
        ),
        now=_now(),
    )
    assert proposals[0].next_state == "pending"
    assert proposals[0].reason_code == "pending_unconfirmed"


@pytest.mark.unit
def test_buy_no_delta_after_stale_threshold_marks_stale():
    proposals = classify_orders(
        orders=[_order(baseline=Decimal("5"), accepted_age_sec=3600)],
        holdings={"005930": _snap(Decimal("5"))},
        thresholds=ReconcilerThresholds(
            pending_threshold_sec=60, stale_threshold_sec=1800
        ),
        now=_now(),
    )
    assert proposals[0].next_state == "stale"
    assert proposals[0].reason_code == "stale_unconfirmed"


@pytest.mark.unit
def test_full_sell_fill_detected():
    proposals = classify_orders(
        orders=[_order(side="sell", baseline=Decimal("20"))],
        holdings={"005930": _snap(Decimal("10"))},  # delta = -10
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    assert proposals[0].next_state == "fill"
    assert proposals[0].reason_code == "fill_detected"


@pytest.mark.unit
def test_baseline_missing_emits_anomaly():
    proposals = classify_orders(
        orders=[_order(baseline=None)],
        holdings={"005930": _snap(Decimal("5"))},
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    assert proposals[0].next_state == "anomaly"
    assert proposals[0].reason_code == "baseline_missing"


@pytest.mark.unit
def test_snapshot_missing_emits_anomaly():
    proposals = classify_orders(
        orders=[_order(baseline=Decimal("5"))],
        holdings={},
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    assert proposals[0].next_state == "anomaly"
    assert proposals[0].reason_code == "holdings_snapshot_missing"


@pytest.mark.unit
def test_fill_to_reconciled_when_holdings_match_expected():
    proposals = classify_orders(
        orders=[
            LedgerOrderInput(
                ledger_id=2,
                symbol="005930",
                side="buy",
                ordered_qty=Decimal("10"),
                lifecycle_state="fill",
                holdings_baseline_qty=Decimal("5"),
                accepted_at=_now(),
            )
        ],
        holdings={"005930": _snap(Decimal("15"))},
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    assert proposals[0].next_state == "reconciled"
    assert proposals[0].reason_code == "position_reconciled"


@pytest.mark.unit
def test_fill_to_anomaly_when_holdings_disagree():
    proposals = classify_orders(
        orders=[
            LedgerOrderInput(
                ledger_id=3,
                symbol="005930",
                side="buy",
                ordered_qty=Decimal("10"),
                lifecycle_state="fill",
                holdings_baseline_qty=Decimal("5"),
                accepted_at=_now(),
            )
        ],
        holdings={"005930": _snap(Decimal("3"))},  # holdings dropped
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    assert proposals[0].next_state == "anomaly"
    assert proposals[0].reason_code == "holdings_mismatch"


@pytest.mark.unit
def test_terminal_states_are_skipped():
    """reconciled, failed, stale orders should not be re-classified."""
    orders = [
        LedgerOrderInput(
            ledger_id=4,
            symbol="005930",
            side="buy",
            ordered_qty=Decimal("10"),
            lifecycle_state=state,
            holdings_baseline_qty=Decimal("5"),
            accepted_at=_now(),
        )
        for state in ("reconciled", "failed", "stale")
    ]
    proposals = classify_orders(
        orders=orders,
        holdings={"005930": _snap(Decimal("99"))},
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    assert proposals == []


@pytest.mark.unit
def test_reconciler_does_not_import_db_or_broker():
    """Reconciler must remain pure; no DB / broker imports."""
    import app.services.kis_mock_holdings_reconciler as mod

    src = open(mod.__file__).read()
    forbidden = [
        "from app.core.db",
        "AsyncSession",
        "KISClient",
        "from app.services.brokers",
        "import sqlalchemy",
    ]
    for tok in forbidden:
        assert tok not in src, f"forbidden import in reconciler: {tok}"


@pytest.mark.unit
def test_ledger_order_input_has_price_default():
    from app.services.kis_mock_holdings_reconciler import LedgerOrderInput

    order = LedgerOrderInput(
        ledger_id=1,
        symbol="005930",
        side="buy",
        ordered_qty=Decimal("10"),
        lifecycle_state="accepted",
        holdings_baseline_qty=Decimal("0"),
        accepted_at=_now(),
    )
    assert order.price == Decimal("0")  # default keeps existing call sites working

    priced = LedgerOrderInput(
        ledger_id=2,
        symbol="005930",
        side="buy",
        ordered_qty=Decimal("10"),
        lifecycle_state="accepted",
        holdings_baseline_qty=Decimal("0"),
        accepted_at=_now(),
        price=Decimal("15900"),
    )
    assert priced.price == Decimal("15900")


@pytest.mark.unit
def test_proposal_attributed_fill_qty_defaults_none():
    from app.services.kis_mock_holdings_reconciler import LifecycleTransitionProposal

    p = LifecycleTransitionProposal(
        ledger_id=1,
        symbol="005930",
        prior_state="accepted",
        next_state="pending",
        reason_code="pending_unconfirmed",
        observed_holdings_qty=Decimal("0"),
        observed_delta=Decimal("0"),
    )
    assert p.attributed_fill_qty is None


def _buy(
    *,
    ledger_id: int,
    price: Decimal,
    ordered_qty: Decimal = Decimal("10"),
    baseline: Decimal = Decimal("0"),
    state: str = "accepted",
    accepted_age_sec: int = 0,
) -> LedgerOrderInput:
    return LedgerOrderInput(
        ledger_id=ledger_id,
        symbol="0148J0",
        side="buy",
        ordered_qty=ordered_qty,
        lifecycle_state=state,
        holdings_baseline_qty=baseline,
        accepted_at=_now() - timedelta(seconds=accepted_age_sec),
        price=price,
    )


@pytest.mark.unit
def test_same_symbol_double_buy_single_delta_attributed_to_higher_price():
    # ROB-400 demo: ledger23 @15,500 / ledger24 @15,900, actual holdings +10 (one fill)
    orders = [
        _buy(ledger_id=23, price=Decimal("15500"), accepted_age_sec=120),
        _buy(ledger_id=24, price=Decimal("15900"), accepted_age_sec=60),
    ]
    proposals = classify_orders(
        orders=orders,
        holdings={"0148J0": HoldingsSnapshot(
            symbol="0148J0", quantity=Decimal("10"), taken_at=_now()
        )},
        thresholds=ReconcilerThresholds(),
        now=_now(),
    )
    by_id = {p.ledger_id: p for p in proposals}
    # higher price (15,900) wins the single +10 budget
    assert by_id[24].next_state == "fill"
    assert by_id[24].reason_code == "fill_detected"
    assert by_id[24].attributed_fill_qty == Decimal("10")
    # the other stays pending — no double count
    assert by_id[23].next_state == "pending"
    assert by_id[23].reason_code == "pending_unconfirmed"
    assert by_id[23].attributed_fill_qty == Decimal("0")


