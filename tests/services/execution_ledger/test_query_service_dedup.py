from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas.execution_ledger import ExecutionLedgerRead
from app.services.execution_ledger.query_service import (
    _annotate_realized_profit,
    _supersede_provisional_fills,
)


def _item(
    *,
    source: str,
    side: str,
    qty: str,
    price: str,
    order_id: str,
    fill_seq: int,
    filled_at: datetime,
    symbol: str = "035420",
    instrument_type: str = "equity_kr",
    venue: str = "krx",
    currency: str = "KRW",
) -> ExecutionLedgerRead:
    quantity = Decimal(qty)
    unit_price = Decimal(price)
    return ExecutionLedgerRead(
        id=None,
        broker="kis",
        account_mode="live",
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
        filled_at=filled_at,
        currency=currency,
        source=source,
    )


def test_supersede_drops_websocket_when_reconciler_covers_order() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    rec = _item(
        source="reconciler",
        side="sell",
        qty="10",
        price="251000",
        order_id="0006366300",
        fill_seq=1511940115,
        filled_at=base,
    )
    ws = _item(
        source="websocket",
        side="sell",
        qty="10",
        price="251000",
        order_id="0006366300",
        fill_seq=654241537,
        filled_at=base + timedelta(minutes=2),
    )

    kept = _supersede_provisional_fills([rec, ws])

    assert len(kept) == 1
    assert kept[0].source == "reconciler"


def test_supersede_prefers_reconciler_even_when_price_disagrees() -> None:
    # 37 prod groups disagree on price; reconciler is authoritative.
    base = datetime(2026, 6, 1, tzinfo=UTC)
    ws = _item(
        source="websocket",
        side="sell",
        qty="26",
        price="299000",
        order_id="0000342400",
        fill_seq=1529477675,
        filled_at=base,
    )
    rec = _item(
        source="reconciler",
        side="sell",
        qty="26",
        price="300000",
        order_id="0000342400",
        fill_seq=1124223453,
        filled_at=base + timedelta(days=1),
    )

    kept = _supersede_provisional_fills([ws, rec])

    assert [k.source for k in kept] == ["reconciler"]
    assert kept[0].filled_price == Decimal("300000")


def test_supersede_keeps_websocket_only_orders() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    ws = _item(
        source="websocket",
        side="sell",
        qty="5",
        price="100",
        order_id="ws-only-1",
        fill_seq=42,
        filled_at=base,
    )

    assert _supersede_provisional_fills([ws]) == [ws]


def test_supersede_preserves_distinct_orders_and_order_is_stable() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    rec_a = _item(
        source="reconciler",
        side="buy",
        qty="2",
        price="196500",
        order_id="0018700900",
        fill_seq=1829863901,
        filled_at=base,
    )
    ws_a = _item(
        source="websocket",
        side="buy",
        qty="2",
        price="196500",
        order_id="0018700900",
        fill_seq=778408146,
        filled_at=base,
    )
    rec_b = _item(
        source="reconciler",
        side="buy",
        qty="2",
        price="252000",
        order_id="0011012000",
        fill_seq=313337176,
        filled_at=base + timedelta(days=1),
    )

    kept = _supersede_provisional_fills([rec_a, ws_a, rec_b])

    assert [k.broker_order_id for k in kept] == ["0018700900", "0011012000"]


def test_supersede_normalizes_leading_zero_order_id() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    rec = _item(
        source="reconciler",
        side="sell",
        qty="1",
        price="262500",
        order_id="0019990600",
        fill_seq=1889703609,
        filled_at=base,
    )
    ws = _item(
        source="websocket",
        side="sell",
        qty="1",
        price="262500",
        order_id="19990600",
        fill_seq=877103355,
        filled_at=base,
    )

    assert len(_supersede_provisional_fills([rec, ws])) == 1


def test_supersede_then_fifo_does_not_double_consume() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    buy = _item(
        source="reconciler",
        side="buy",
        qty="10",
        price="200000",
        order_id="buy-1",
        fill_seq=1,
        filled_at=base,
    )
    sell_rec = _item(
        source="reconciler",
        side="sell",
        qty="10",
        price="251000",
        order_id="sell-1",
        fill_seq=111,
        filled_at=base + timedelta(days=1),
    )
    sell_ws = _item(
        source="websocket",
        side="sell",
        qty="10",
        price="251000",
        order_id="sell-1",
        fill_seq=222,
        filled_at=base + timedelta(days=1),
    )

    history = _supersede_provisional_fills([buy, sell_rec, sell_ws])
    sells = [i for i in history if i.side == "sell"]
    annotated = _annotate_realized_profit(sells, history)

    # One sell, cost basis from the single 10-share lot (not double-consumed).
    assert len(annotated) == 1
    assert annotated[0].cost_basis_notional == Decimal("2000000")
    assert annotated[0].realized_profit == Decimal("510000")
