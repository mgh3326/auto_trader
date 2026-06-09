# tests/services/execution_ledger/test_opening_lots.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.services.execution_ledger.opening_lots import (
    OpeningLotCandidate,
    build_opening_lot_plan,
)


def _candidate(**overrides) -> OpeningLotCandidate:  # noqa: ANN003
    data = {
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "raw_symbol": "005930",
        "currency": "KRW",
        "current_qty": Decimal("10"),
        "avg_price": Decimal("70000"),
        "avg_price_modified": False,
    }
    data.update(overrides)
    return OpeningLotCandidate(**data)


def test_opening_lot_quantity_subtracts_ledger_net_since_cutover() -> None:
    cutover = datetime(2026, 5, 10, tzinfo=UTC)
    plan = build_opening_lot_plan(
        candidates=[_candidate()],
        ledger_net_by_key={("kis", "live", "krx", "equity_kr", "005930", "KRW"): Decimal("3")},
        cutover=cutover,
    )

    assert len(plan.upserts) == 1
    upsert = plan.upserts[0]
    assert upsert.source == "manual_import"
    assert upsert.side == "buy"
    assert upsert.filled_qty == Decimal("7")
    assert upsert.filled_price == Decimal("70000")
    assert upsert.filled_at == cutover
    assert upsert.broker_order_id == "SEED-20260510-kis-krx-005930"


def test_opening_lot_skips_when_ledger_net_covers_current_position() -> None:
    plan = build_opening_lot_plan(
        candidates=[_candidate(current_qty=Decimal("10"))],
        ledger_net_by_key={("kis", "live", "krx", "equity_kr", "005930", "KRW"): Decimal("10")},
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
    )

    assert plan.upserts == []
    assert plan.skipped[0].reason == "covered_by_ledger_net"


def test_opening_lot_skips_modified_upbit_average_price() -> None:
    plan = build_opening_lot_plan(
        candidates=[
            _candidate(
                broker="upbit",
                venue="upbit_krw",
                instrument_type="crypto",
                symbol="SOL",
                raw_symbol="KRW-SOL",
                avg_price_modified=True,
            )
        ],
        ledger_net_by_key={},
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
    )

    assert plan.upserts == []
    assert plan.skipped[0].reason == "upbit_avg_price_modified"


def test_opening_lot_skips_zero_average_price() -> None:
    plan = build_opening_lot_plan(
        candidates=[_candidate(avg_price=Decimal("0"))],
        ledger_net_by_key={},
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
    )

    assert plan.upserts == []
    assert plan.skipped[0].reason == "non_positive_avg_price"
