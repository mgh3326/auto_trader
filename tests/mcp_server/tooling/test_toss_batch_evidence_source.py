from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling.toss_live_evidence import TossBatchEvidenceSource
from app.services.brokers.toss.dto import TossOrder, TossOrdersPage

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _order(order_id: str, status: str, filled: str = "0", avg: str | None = None):
    execution = {"filledQuantity": Decimal(filled)}
    if avg is not None:
        execution["averageFilledPrice"] = Decimal(avg)
    return TossOrder(
        order_id=order_id,
        symbol="034020",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        status=status,
        price=Decimal("85000"),
        quantity=Decimal("3"),
        order_amount=None,
        currency="KRW",
        ordered_at="2026-07-01T00:00:00Z",
        canceled_at=None,
        execution=execution,
    )


def _row(order_id: str, days_ago: int = 1):
    return SimpleNamespace(
        broker_order_id=order_id,
        trade_date=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
    )


class _FakeClient:
    def __init__(self, *, open_orders, closed_pages):
        self._open = open_orders
        self._closed_pages = list(closed_pages)
        self.list_calls: list[dict] = []
        self.get_order = AsyncMock(return_value=_order("older", "FILLED", "3", "85000"))
        self.aclose = AsyncMock()

    async def list_orders(
        self,
        *,
        status,
        symbol=None,
        from_date=None,
        to_date=None,
        cursor=None,
        limit=None,
    ):
        self.list_calls.append(
            {"status": status, "from": from_date, "to": to_date, "cursor": cursor}
        )
        if status == "OPEN":
            return TossOrdersPage(orders=self._open, next_cursor=None, has_next=False)
        page = self._closed_pages.pop(0)
        return page


async def test_build_maps_open_and_closed_without_per_row_get_order():
    client = _FakeClient(
        open_orders=[_order("open-1", "PENDING")],
        closed_pages=[
            TossOrdersPage(
                orders=[_order("closed-1", "FILLED", "3", "85000")],
                next_cursor=None,
                has_next=False,
            )
        ],
    )
    rows = [_row("open-1"), _row("closed-1")]
    source = await TossBatchEvidenceSource.build(rows=rows, client=client)

    ev_open = await source.evidence_for(_row("open-1"))
    ev_closed = await source.evidence_for(_row("closed-1"))

    assert ev_open.verdict == "pending"
    assert ev_closed.verdict == "filled"
    client.get_order.assert_not_awaited()  # everything came from the batch map
    assert source.single_fetch_count == 0
    # exactly: 1 OPEN call + 1 CLOSED page
    assert sum(1 for c in client.list_calls if c["status"] == "OPEN") == 1
    assert sum(1 for c in client.list_calls if c["status"] == "CLOSED") == 1


async def test_row_outside_window_falls_back_to_single_get_order():
    client = _FakeClient(
        open_orders=[],
        closed_pages=[TossOrdersPage(orders=[], next_cursor=None, has_next=False)],
    )
    source = await TossBatchEvidenceSource.build(rows=[_row("open-1")], client=client)
    ev = await source.evidence_for(_row("older"))

    assert ev.verdict == "filled"
    client.get_order.assert_awaited_once_with("older")
    assert source.single_fetch_count == 1


async def test_closed_pagination_is_capped_and_flagged(monkeypatch):
    import app.mcp_server.tooling.toss_live_evidence as ev_mod

    monkeypatch.setattr(ev_mod, "_TOSS_CLOSED_PAGE_CAP", 2)
    # 3 pages available, each says has_next -> cap stops at 2
    pages = [
        TossOrdersPage(
            orders=[_order(f"c{i}", "FILLED", "3", "85000")],
            next_cursor=f"cur{i}",
            has_next=True,
        )
        for i in range(3)
    ]
    client = _FakeClient(open_orders=[], closed_pages=pages)
    source = await TossBatchEvidenceSource.build(rows=[_row("c0")], client=client)

    assert source.closed_pages_capped is True
    assert sum(1 for c in client.list_calls if c["status"] == "CLOSED") == 2
