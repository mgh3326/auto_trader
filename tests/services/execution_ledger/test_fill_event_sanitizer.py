from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.services.execution_ledger.fill_event_sanitizer import sanitize_fill


def test_sanitize_fill_exposes_explicit_kst_trade_day() -> None:
    row = SimpleNamespace(
        id=933,
        broker="kis",
        account_mode="live",
        venue="krx",
        instrument_type="equity_kr",
        symbol="214150",
        raw_symbol="214150",
        side="sell",
        filled_qty=Decimal("2"),
        filled_price=Decimal("100000"),
        filled_notional=Decimal("200000"),
        currency="KRW",
        broker_order_id="rob933-sanitizer",
        fill_seq=0,
        correlation_id=None,
        source="reconciler",
        filled_at=datetime(2026, 7, 15, 15, 17, 28, tzinfo=UTC),
        created_at=datetime(2026, 7, 15, 15, 18, tzinfo=UTC),
    )

    assert sanitize_fill(row)["trade_day_kst"] == "20260716"
