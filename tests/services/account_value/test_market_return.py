from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.services.account_value.market_return import market_row


def snapshot(day, quantity):
    return SimpleNamespace(
        day=day,
        observed_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
        holdings=[
            {
                "symbol": "KRW-BTC",
                "quantity": str(quantity),
                "currency": "KRW",
                "mark": "100",
                "mark_source": "daily",
                "mark_asof": "2026-01-01T00:00:00+00:00",
            }
        ],
        reasons=[],
    )


def fill(quantity):
    return SimpleNamespace(
        symbol="KRW-BTC",
        filled_at=datetime(2026, 1, 2, 6, tzinfo=UTC),
        currency="KRW",
        filled_qty=Decimal(quantity),
        filled_notional=Decimal(100),
        fee_amount=Decimal(0),
        fee_currency=None,
        side="buy",
    )


def test_holdings_only_reconciles_trades_and_removes_purchase_flow():
    start, end = date(2026, 1, 1), date(2026, 1, 2)
    row = market_row(
        "upbit:upbit:test",
        "crypto",
        start,
        end,
        [snapshot(start, 1), snapshot(end, 2)],
        [fill(1)],
        (None, None),
    )
    assert row.basis == "holdings_only"
    assert row.return_rate == 0
    assert row.flows == 100
    assert row.reasons == ()


def test_unmatched_quantity_is_unmeasured():
    start, end = date(2026, 1, 1), date(2026, 1, 2)
    row = market_row(
        "upbit:upbit:test",
        "crypto",
        start,
        end,
        [snapshot(start, 1), snapshot(end, 2)],
        [],
        (None, None),
    )
    assert row.return_rate is None
    assert "flow_incomplete:KRW-BTC" in row.reasons
