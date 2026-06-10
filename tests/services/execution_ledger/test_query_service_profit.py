from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas.execution_ledger import ExecutionLedgerRead
from app.services.execution_ledger.query_service import _annotate_realized_profit


def _item(
    *,
    side: str,
    qty: str,
    price: str,
    filled_at: datetime,
    order_id: str,
    fill_seq: int = 0,
    broker: str = "kis",
    account_mode: str = "live",
    venue: str = "krx",
    instrument_type: str = "equity_kr",
    symbol: str = "005930",
    currency: str = "KRW",
    fee_amount: str | None = None,
) -> ExecutionLedgerRead:
    quantity = Decimal(qty)
    unit_price = Decimal(price)
    return ExecutionLedgerRead(
        id=None,
        broker=broker,
        account_mode=account_mode,
        venue=venue,
        instrument_type=instrument_type,
        symbol=symbol,
        raw_symbol=symbol,
        side=side,
        broker_order_id=order_id,
        fill_seq=fill_seq,
        filled_qty=quantity,
        filled_price=unit_price,
        filled_notional=quantity * unit_price,
        fee_amount=Decimal(fee_amount) if fee_amount is not None else None,
        fee_currency=currency if fee_amount is not None else None,
        filled_at=filled_at,
        currency=currency,
        source="reconciler",
    )


def test_annotate_realized_profit_uses_multilot_fifo() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    buy_a = _item(side="buy", qty="5", price="100", filled_at=base, order_id="buy-a")
    buy_b = _item(
        side="buy",
        qty="5",
        price="120",
        filled_at=base + timedelta(days=1),
        order_id="buy-b",
    )
    sell = _item(
        side="sell",
        qty="7",
        price="150",
        filled_at=base + timedelta(days=2),
        order_id="sell-a",
    )

    annotated = _annotate_realized_profit([sell], [buy_a, buy_b, sell])

    assert annotated[0].cost_basis_notional == Decimal("740")
    assert annotated[0].realized_profit == Decimal("310")
    assert annotated[0].realized_profit_rate == Decimal("41.89189189189189189189189189")


def test_annotate_realized_profit_keeps_null_when_partially_uncovered() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    buy = _item(side="buy", qty="5", price="100", filled_at=base, order_id="buy-a")
    sell = _item(
        side="sell",
        qty="7",
        price="150",
        filled_at=base + timedelta(days=1),
        order_id="sell-a",
    )

    annotated = _annotate_realized_profit([sell], [buy, sell])

    assert annotated[0].cost_basis_notional is None
    assert annotated[0].realized_profit is None
    assert annotated[0].realized_profit_rate is None


def test_annotate_realized_profit_isolates_venue_in_match_key() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    nasd_buy = _item(
        side="buy",
        qty="10",
        price="100",
        filled_at=base,
        order_id="buy-a",
        venue="NASD",
        instrument_type="equity_us",
        symbol="AAPL",
        currency="USD",
    )
    nyse_sell = _item(
        side="sell",
        qty="1",
        price="150",
        filled_at=base + timedelta(days=1),
        order_id="sell-a",
        venue="NYSE",
        instrument_type="equity_us",
        symbol="AAPL",
        currency="USD",
    )

    annotated = _annotate_realized_profit([nyse_sell], [nasd_buy, nyse_sell])

    assert annotated[0].realized_profit is None


def test_annotate_realized_profit_remains_gross_and_ignores_fees() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    buy = _item(
        side="buy",
        qty="1",
        price="100",
        filled_at=base,
        order_id="buy-a",
        fee_amount="10",
    )
    sell = _item(
        side="sell",
        qty="1",
        price="130",
        filled_at=base + timedelta(days=1),
        order_id="sell-a",
        fee_amount="10",
    )

    annotated = _annotate_realized_profit([sell], [buy, sell])

    assert annotated[0].cost_basis_notional == Decimal("100")
    assert annotated[0].realized_profit == Decimal("30")
    assert annotated[0].realized_profit_rate == Decimal("30.0")
