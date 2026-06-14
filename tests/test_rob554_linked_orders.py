"""ROB-554 — linked-order read-back: schema, projection, lookup, serializers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytestmark = pytest.mark.asyncio


async def test_linked_order_view_defaults_and_item_field() -> None:
    # async (not sync) so the module-level asyncio marker stays uniform; no await needed.
    from app.schemas.investment_reports import (
        InvestmentReportItemResponse,
        LinkedOrderView,
    )

    view = LinkedOrderView(ledger_id=1, order_no="x", status="filled")
    assert view.market is None
    assert view.filled_qty is None
    assert "linked_orders" in InvestmentReportItemResponse.model_fields


async def test_list_linked_orders_groups_both_ledgers(session) -> None:
    from app.models.review import KISLiveOrderLedger, LiveOrderLedger
    from app.services.investment_reports.linked_orders import (
        list_linked_orders_for_item_uuids,
    )

    rid, other = uuid.uuid4(), uuid.uuid4()
    crypto_no = f"rob554-{uuid.uuid4().hex[:10]}"
    kr_no = f"rob554-{uuid.uuid4().hex[:10]}"

    session.add(
        LiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            broker="upbit",
            account_scope="upbit_live",
            market="crypto",
            symbol="BTC",
            side="buy",
            order_kind="limit",
            order_no=crypto_no,
            status="filled",
            lifecycle_state="filled",
            filled_qty=Decimal("0.01"),
            avg_fill_price=Decimal("96180000"),
            report_item_uuid=rid,
        )
    )
    session.add(
        KISLiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            symbol="005930",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            order_no=kr_no,
            account_mode="kis_live",
            broker="kis",
            status="filled",
            lifecycle_state="filled",
            filled_qty=Decimal("3"),
            avg_fill_price=Decimal("70100"),
            report_item_uuid=rid,
        )
    )
    # unrelated order under a different report item — must not leak into rid's group
    session.add(
        LiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            broker="upbit",
            account_scope="upbit_live",
            market="crypto",
            symbol="ETH",
            side="buy",
            order_kind="limit",
            order_no=f"rob554-{uuid.uuid4().hex[:10]}",
            status="accepted",
            lifecycle_state="accepted",
            report_item_uuid=other,
        )
    )
    await session.flush()

    grouped = await list_linked_orders_for_item_uuids(session, [rid])

    assert set(grouped) == {str(rid)}
    by_no = {v.order_no: v for v in grouped[str(rid)]}
    assert len(by_no) == 2
    assert by_no[crypto_no].market == "crypto"
    assert by_no[crypto_no].account_scope == "upbit_live"
    assert by_no[crypto_no].filled_qty == Decimal("0.01")
    # KR row: account_mode -> account_scope, market constant "kr"
    assert by_no[kr_no].market == "kr"
    assert by_no[kr_no].account_scope == "kis_live"
    assert by_no[kr_no].broker == "kis"
    assert by_no[kr_no].avg_fill_price == Decimal("70100")


async def test_list_linked_orders_empty_for_unlinked(session) -> None:
    from app.services.investment_reports.linked_orders import (
        list_linked_orders_for_item_uuids,
    )

    grouped = await list_linked_orders_for_item_uuids(session, [uuid.uuid4()])
    assert grouped == {}


def _action_item():
    from app.schemas.investment_reports import IngestReportItem

    return IngestReportItem(
        client_item_key="rob554-action-1",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        rationale="r",
    )


def _request():
    from app.schemas.investment_reports import IngestReportRequest

    return IngestReportRequest(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="rob554",
        summary="s",
        kst_date="2026-06-12",
        items=[_action_item()],
    )


async def _seed_report_with_linked_order(session):
    """Ingest a report+item, attach one crypto live order via report_item_uuid."""
    from app.models.review import LiveOrderLedger
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.repository import (
        InvestmentReportsRepository,
    )

    report = await InvestmentReportIngestionService(session).ingest(_request())
    item = (
        await InvestmentReportsRepository(session).list_items_for_report(report.id)
    )[0]
    order_no = f"rob554-{uuid.uuid4().hex[:10]}"
    session.add(
        LiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            broker="upbit",
            account_scope="upbit_live",
            market="crypto",
            symbol="BTC",
            side="buy",
            order_kind="limit",
            order_no=order_no,
            status="filled",
            lifecycle_state="filled",
            filled_qty=Decimal("0.01"),
            avg_fill_price=Decimal("96180000"),
            report_item_uuid=item.item_uuid,
        )
    )
    await session.flush()
    return report, item, order_no


async def test_get_bundle_attaches_linked_orders(session) -> None:
    from app.services.investment_reports.query_service import (
        InvestmentReportQueryService,
    )

    report, item, order_no = await _seed_report_with_linked_order(session)
    bundle = await InvestmentReportQueryService(session).get_bundle(report.report_uuid)

    assert bundle is not None
    linked = bundle["linked_orders_by_item_uuid"]
    assert str(item.item_uuid) in linked
    assert linked[str(item.item_uuid)][0].order_no == order_no
    assert linked[str(item.item_uuid)][0].filled_qty == Decimal("0.01")


async def test_both_serialisers_carry_linked_orders(session) -> None:
    from app.mcp_server.tooling.investment_reports_handlers import (
        _serialise_bundle as mcp_serialise,
    )
    from app.routers.investment_reports import _serialise_bundle as web_serialise
    from app.services.investment_reports.query_service import (
        InvestmentReportQueryService,
    )

    report, item, order_no = await _seed_report_with_linked_order(session)
    bundle = await InvestmentReportQueryService(session).get_bundle(report.report_uuid)

    web = web_serialise(bundle)
    mcp = mcp_serialise(bundle)

    web_item = next(i for i in web.items if str(i.item_uuid) == str(item.item_uuid))
    mcp_item = next(i for i in mcp.items if str(i.item_uuid) == str(item.item_uuid))
    assert web_item.linked_orders is not None
    assert web_item.linked_orders[0].order_no == order_no
    assert mcp_item.linked_orders is not None
    assert mcp_item.linked_orders[0].order_no == order_no


async def test_live_helper_delegates_to_shared_projection(db_session) -> None:
    from app.mcp_server.tooling import live_order_ledger as m

    rid = uuid.uuid4()
    order_no = f"rob554-{uuid.uuid4().hex[:10]}"
    await m._save_live_order_ledger(
        broker="upbit",
        account_scope="upbit_live",
        market="crypto",
        symbol="BTC",
        exchange=None,
        market_symbol="KRW-BTC",
        side="buy",
        order_kind="limit",
        quantity=0.01,
        price=96180000.0,
        amount=961800.0,
        currency="KRW",
        order_no=order_no,
        order_time="2026-06-12T00:00:00Z",
        status="accepted",
        response_code="0",
        response_message="ok",
        raw_response={},
        reason="r",
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        report_item_uuid=rid,
    )
    rows = await m.list_live_orders_by_report_item_uuid(rid)
    row = next(r for r in rows if r["order_no"] == order_no)
    # delegation now surfaces the fill-rollup fields the old projection lacked
    assert "filled_qty" in row
    assert row["market"] == "crypto"
    assert row["status"] == "accepted"


async def test_list_linked_orders_includes_toss(session) -> None:
    # ROB-554 — Toss is a live KR/US broker carrying report_item_uuid (ROB-545);
    # it must surface in linked_orders like LiveOrderLedger / KISLiveOrderLedger.
    from app.models.review import TossLiveOrderLedger
    from app.services.investment_reports.linked_orders import (
        list_linked_orders_for_item_uuids,
    )

    rid = uuid.uuid4()
    bo = f"toss-bo-{uuid.uuid4().hex[:8]}"
    co1 = f"toss-co-{uuid.uuid4().hex[:8]}"
    co2 = f"toss-co-{uuid.uuid4().hex[:8]}"

    session.add(
        TossLiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            broker="toss",
            account_mode="toss_live",
            operation_kind="place",
            market="us",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            client_order_id=co1,
            broker_order_id=bo,
            status="filled",
            filled_qty=Decimal("2"),
            avg_fill_price=Decimal("250.5"),
            report_item_uuid=rid,
        )
    )
    # broker_order_id NULL -> order_no falls back to client_order_id
    session.add(
        TossLiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            broker="toss",
            account_mode="toss_live",
            operation_kind="place",
            market="kr",
            symbol="005930",
            side="buy",
            order_type="limit",
            client_order_id=co2,
            broker_order_id=None,
            status="accepted",
            report_item_uuid=rid,
        )
    )
    await session.flush()

    grouped = await list_linked_orders_for_item_uuids(session, [rid])
    by_no = {v.order_no: v for v in grouped[str(rid)]}
    assert bo in by_no  # broker_order_id preferred for order_no
    assert co2 in by_no  # client_order_id fallback when broker_order_id is None
    assert by_no[bo].broker == "toss"
    assert by_no[bo].account_scope == "toss_live"  # account_mode -> account_scope
    assert by_no[bo].market == "us"  # passthrough (kr|us)
    assert by_no[bo].filled_qty == Decimal("2")
    assert by_no[bo].order_time is None  # Toss has no order_time column


async def test_list_linked_orders_same_ledger_id_desc(session) -> None:
    # ROB-554 — within a ledger, most-recent (highest id) first.
    from app.models.review import LiveOrderLedger
    from app.services.investment_reports.linked_orders import (
        list_linked_orders_for_item_uuids,
    )

    rid = uuid.uuid4()
    first = f"rob554-{uuid.uuid4().hex[:10]}"
    second = f"rob554-{uuid.uuid4().hex[:10]}"
    for no in (first, second):  # second flushed later -> higher id
        session.add(
            LiveOrderLedger(
                trade_date=datetime(2026, 6, 12, tzinfo=UTC),
                broker="upbit",
                account_scope="upbit_live",
                market="crypto",
                symbol="BTC",
                side="buy",
                order_kind="limit",
                order_no=no,
                status="accepted",
                lifecycle_state="accepted",
                report_item_uuid=rid,
            )
        )
        await session.flush()

    grouped = await list_linked_orders_for_item_uuids(session, [rid])
    order_nos = [v.order_no for v in grouped[str(rid)]]
    assert order_nos == [second, first]  # id desc


def _action_item_two():
    from app.schemas.investment_reports import IngestReportItem

    return IngestReportItem(
        client_item_key="rob554-action-2",
        item_kind="action",
        symbol="000660",
        side="buy",
        intent="buy_review",
        rationale="r2",
    )


async def test_serialisers_set_none_for_unlinked_item(session) -> None:
    # ROB-554 — the "missing key => None" contract the frontend null-maps on.
    from app.mcp_server.tooling.investment_reports_handlers import (
        _serialise_bundle as mcp_serialise,
    )
    from app.models.review import LiveOrderLedger
    from app.routers.investment_reports import _serialise_bundle as web_serialise
    from app.schemas.investment_reports import IngestReportRequest
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.query_service import (
        InvestmentReportQueryService,
    )
    from app.services.investment_reports.repository import (
        InvestmentReportsRepository,
    )

    req = IngestReportRequest(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="rob554-2items",
        summary="s",
        kst_date="2026-06-12",
        items=[_action_item(), _action_item_two()],
    )
    report = await InvestmentReportIngestionService(session).ingest(req)
    items = await InvestmentReportsRepository(session).list_items_for_report(report.id)
    linked_item, unlinked_item = items[0], items[1]
    session.add(
        LiveOrderLedger(
            trade_date=datetime(2026, 6, 12, tzinfo=UTC),
            broker="upbit",
            account_scope="upbit_live",
            market="crypto",
            symbol="BTC",
            side="buy",
            order_kind="limit",
            order_no=f"rob554-{uuid.uuid4().hex[:10]}",
            status="filled",
            lifecycle_state="filled",
            report_item_uuid=linked_item.item_uuid,
        )
    )
    await session.flush()

    bundle = await InvestmentReportQueryService(session).get_bundle(report.report_uuid)
    for serialise in (web_serialise, mcp_serialise):
        out = serialise(bundle)
        by_uuid = {str(i.item_uuid): i for i in out.items}
        assert by_uuid[str(linked_item.item_uuid)].linked_orders is not None
        assert by_uuid[str(unlinked_item.item_uuid)].linked_orders is None
