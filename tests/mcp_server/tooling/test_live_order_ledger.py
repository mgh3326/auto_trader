import pytest


@pytest.mark.unit
def test_live_order_ledger_model_shape():
    from app.models.review import LiveOrderLedger

    assert LiveOrderLedger.__tablename__ == "live_order_ledger"
    cols = set(LiveOrderLedger.__table__.columns.keys())
    # 디스크리미네이터 + 시장 메타가 존재
    for c in (
        "broker",
        "account_scope",
        "market",
        "symbol",
        "exchange",
        "market_symbol",
        "order_no",
        "order_kind",
        "status",
        "filled_qty",
        "avg_fill_price",
        "trade_id",
        "journal_id",
    ):
        assert c in cols, f"missing column {c}"
    assert LiveOrderLedger.__table__.schema == "review"


import pytest_asyncio
from sqlalchemy import delete


@pytest_asyncio.fixture(autouse=True)
async def _clean_live_ledger():
    from app.models.review import LiveOrderLedger
    from app.mcp_server.tooling.live_order_ledger import _order_session_factory

    async with _order_session_factory()() as db:
        await db.execute(delete(LiveOrderLedger))
        await db.commit()
    yield


@pytest.mark.unit
@pytest.mark.asyncio
async def test_save_live_order_ledger_accepted_only():
    from app.mcp_server.tooling import live_order_ledger as ll

    lid = await ll._save_live_order_ledger(
        broker="kis",
        account_scope="kis_live",
        market="us",
        symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        quantity=2.0,
        price=190.0,
        amount=380.0,
        currency="USD",
        order_no="US-ACC-1",
        order_time="0930",
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={"odno": "US-ACC-1"},
        reason=None,
        thesis="t",
        strategy="s",
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    assert row is not None
    assert row.status == "accepted"
    assert row.trade_id is None and row.journal_id is None  # no booking at send
    assert row.filled_qty is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_filled_buy_books_once_and_idempotent():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch
    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis", account_scope="kis_live", market="us", symbol="AAPL",
        exchange="NASD", market_symbol=None, side="buy", order_kind="limit",
        quantity=3.0, price=190.0, amount=570.0, currency="USD",
        order_no="US-RC-1", order_time="0930", status="accepted",
        response_code="0", response_message=None, raw_response=None,
        reason=None, thesis="t", strategy="s", target_price=None, stop_loss=None,
        min_hold_days=None, notes=None, exit_reason=None, indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    filled = FillEvidence(FillVerdict.FILLED, Decimal("3"), Decimal("191.5"), None, "filled", "")

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=filled)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock(return_value=111)) as m_fill,
        patch.object(
            ll, "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 9, "journal_status": "draft"}),
        ) as m_buy,
        patch.object(ll, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
    ):
        out1 = await ll._reconcile_one_live_row(row, dry_run=False)
        # 재실행: 이미 booked → 델타 0 → 추가 booking 없음
        row2 = await ll._load_live_ledger_row(lid)
        out2 = await ll._reconcile_one_live_row(row2, dry_run=False)

    assert out1["verdict"] == "filled"
    # broker 확정 qty/price로 1회만 fill booking
    _, fkw = m_fill.await_args
    assert float(fkw["quantity"]) == 3.0
    assert float(fkw["price"]) == 191.5
    assert m_fill.await_count == 1  # 멱등: 두번째 reconcile은 booking 안 함
    assert m_buy.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_cancelled_no_journal():
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch
    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        FillEvidence,
        FillVerdict,
    )

    lid = await ll._save_live_order_ledger(
        broker="kis", account_scope="kis_live", market="us", symbol="AAPL",
        exchange="NASD", market_symbol=None, side="buy", order_kind="limit",
        quantity=3.0, price=190.0, amount=570.0, currency="USD",
        order_no="US-RC-2", order_time="0930", status="accepted",
        response_code="0", response_message=None, raw_response=None,
        reason=None, thesis=None, strategy=None, target_price=None, stop_loss=None,
        min_hold_days=None, notes=None, exit_reason=None, indicators_snapshot=None,
    )
    row = await ll._load_live_ledger_row(lid)
    none_ev = FillEvidence(FillVerdict.NONE, Decimal("0"), None, None, "cancelled", "")

    class _Adapter:
        broker = "kis"
        fetch_evidence = AsyncMock(return_value=none_ev)

    with (
        patch.object(ll, "get_evidence_adapter", return_value=_Adapter()),
        patch.object(ll, "_save_order_fill", new=AsyncMock()) as m_fill,
        patch.object(ll, "_create_trade_journal_for_buy", new=AsyncMock()) as m_buy,
    ):
        out = await ll._reconcile_one_live_row(row, dry_run=False)

    assert out["verdict"] == "none"
    m_fill.assert_not_awaited()
    m_buy.assert_not_awaited()
    after = await ll._load_live_ledger_row(lid)
    assert after.status == "cancelled"
    assert after.journal_id is None


