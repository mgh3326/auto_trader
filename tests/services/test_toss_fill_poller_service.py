from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.review import TossFillPollState, TossLiveOrderLedger
from app.services.brokers.toss.dto import TossOrder, TossOrdersPage
from app.services.toss_fill_poller_service import TossFillPollerService
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _order(
    order_id: str,
    *,
    currency: str = "KRW",
    status: str = "FILLED",
    execution: dict | None = None,
) -> TossOrder:
    if execution is None:
        execution = {
            "filledQuantity": Decimal("3") if currency == "KRW" else Decimal("2"),
            "averageFilledPrice": Decimal("85000")
            if currency == "KRW"
            else Decimal("190"),
        }
    return TossOrder(
        order_id=order_id,
        symbol="034020" if currency == "KRW" else "AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        status=status,
        price=Decimal("85000") if currency == "KRW" else Decimal("190"),
        quantity=Decimal("3") if currency == "KRW" else Decimal("2"),
        order_amount=None,
        currency=currency,
        ordered_at="2026-07-07T00:30:00Z",
        canceled_at=None,
        execution=execution,
    )


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

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
        self.calls.append(
            {
                "status": status,
                "symbol": symbol,
                "from_date": from_date,
                "to_date": to_date,
                "cursor": cursor,
                "limit": limit,
            }
        )
        if status == "OPEN":
            return TossOrdersPage(
                orders=[_order("app-open", status="PENDING")],
                next_cursor=None,
                has_next=False,
            )
        if cursor is None:
            return TossOrdersPage(
                orders=[_order("app-filled")], next_cursor="next", has_next=True
            )
        return TossOrdersPage(
            orders=[_order("app-us", currency="USD")], next_cursor=None, has_next=False
        )


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.execute(delete(TossFillPollState))
    await db_session.commit()


async def test_discovery_seeds_missing_open_and_closed_orders(db_session):
    client = _FakeClient()

    result = await TossFillPollerService(
        db_session, client=client
    ).discover_external_orders(
        dry_run=False,
        lookback_days=7,
        closed_page_cap=5,
    )

    rows = (
        (
            await db_session.execute(
                select(TossLiveOrderLedger).order_by(
                    TossLiveOrderLedger.broker_order_id
                )
            )
        )
        .scalars()
        .all()
    )

    assert result["seeded"] == 3
    assert {row.broker_order_id for row in rows} == {"app-filled", "app-open", "app-us"}
    assert {row.client_order_id for row in rows} == {
        "toss-external:app-filled",
        "toss-external:app-open",
        "toss-external:app-us",
    }
    assert {row.market for row in rows} == {"kr", "us"}
    assert all(row.status == "accepted" for row in rows)
    assert all(row.operation_kind == "place" for row in rows)
    state = await db_session.get(TossFillPollState, "orders")
    assert state is not None
    assert state.last_success_at is not None


async def test_discovery_is_idempotent_by_broker_order_id(db_session):
    await TossLiveOrderLedgerService(db_session).record_external_order(
        _order("app-filled"),
        market="kr",
    )

    result = await TossFillPollerService(
        db_session, client=_FakeClient()
    ).discover_external_orders(
        dry_run=False,
        lookback_days=7,
        closed_page_cap=5,
    )

    assert result["skipped_existing"] == 1
    rows = (
        (
            await db_session.execute(
                select(TossLiveOrderLedger).where(
                    TossLiveOrderLedger.broker_order_id == "app-filled"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_discovery_dry_run_does_not_seed_or_update_state(db_session):
    result = await TossFillPollerService(
        db_session, client=_FakeClient()
    ).discover_external_orders(
        dry_run=True,
        lookback_days=7,
        closed_page_cap=5,
    )

    assert result["would_seed"] == 3
    assert (await db_session.execute(select(TossLiveOrderLedger))).scalars().all() == []
    assert await db_session.get(TossFillPollState, "orders") is None


async def test_discovery_records_error_on_failure(db_session):
    class _FailingClient:
        async def list_orders(self, **kwargs):
            raise RuntimeError("toss api down")

    try:
        await TossFillPollerService(
            db_session, client=_FailingClient()
        ).discover_external_orders(
            dry_run=False,
            lookback_days=7,
            closed_page_cap=5,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError to propagate")

    state = await db_session.get(TossFillPollState, "orders")
    assert state is not None
    assert state.last_error is not None
    assert state.last_error["type"] == "RuntimeError"
    assert "toss api down" in state.last_error["message"]
    assert state.last_success_at is None


async def test_discovery_seeds_filled_closed_order_without_list_execution_summary(
    db_session,
):
    class _FilledWithoutSummaryClient:
        async def list_orders(self, *, status, **kwargs):
            if status == "OPEN":
                return TossOrdersPage(orders=[], next_cursor=None, has_next=False)
            return TossOrdersPage(
                orders=[_order("filled-no-summary", status="FILLED", execution={})],
                next_cursor=None,
                has_next=False,
            )

    result = await TossFillPollerService(
        db_session, client=_FilledWithoutSummaryClient()
    ).discover_external_orders(
        dry_run=False,
        lookback_days=7,
        closed_page_cap=5,
    )

    row = (
        await db_session.execute(
            select(TossLiveOrderLedger).where(
                TossLiveOrderLedger.broker_order_id == "filled-no-summary"
            )
        )
    ).scalar_one()
    assert result["seeded"] == 1
    assert row.status == "accepted"


async def test_discovery_incomplete_scan_records_error_without_advancing_success(
    db_session,
):
    class _CappedClient:
        async def list_orders(self, *, status, **kwargs):
            if status == "OPEN":
                return TossOrdersPage(orders=[], next_cursor=None, has_next=False)
            return TossOrdersPage(
                orders=[_order("cap-page-filled", status="FILLED")],
                next_cursor="next-page",
                has_next=True,
            )

    with pytest.raises(RuntimeError, match="incomplete Toss order scan"):
        await TossFillPollerService(
            db_session, client=_CappedClient()
        ).discover_external_orders(
            dry_run=False,
            lookback_days=7,
            closed_page_cap=1,
        )

    state = await db_session.get(TossFillPollState, "orders")
    assert state is not None
    assert state.last_success_at is None
    assert state.last_error is not None
    assert state.last_error["type"] == "TossFillPollIncompleteScanError"

    row = (
        await db_session.execute(
            select(TossLiveOrderLedger).where(
                TossLiveOrderLedger.broker_order_id == "cap-page-filled"
            )
        )
    ).scalar_one()
    assert row.status == "accepted"
