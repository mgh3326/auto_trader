import pytest
import pytest_asyncio

from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
)


@pytest_asyncio.fixture(autouse=True)
async def clean_kis_live_ledger(db_session):
    from sqlalchemy import text

    from app.mcp_server.tooling.kis_live_ledger import _order_session_factory

    async with _order_session_factory()() as db:
        await db.execute(text("TRUNCATE TABLE review.kis_live_order_ledger CASCADE"))
        await db.execute(text("TRUNCATE TABLE review.live_order_ledger CASCADE"))
        await db.execute(text("TRUNCATE TABLE review.toss_live_order_ledger CASCADE"))
        await db.commit()


@pytest.mark.unit
@pytest.mark.parametrize(
    "model", [KISLiveOrderLedger, LiveOrderLedger, TossLiveOrderLedger]
)
def test_correlation_id_column_present_and_nullable(model):
    col = model.__table__.c.correlation_id
    assert col is not None
    assert col.nullable is True
    # indexed for join lookups
    index_cols = {
        tuple(c.name for c in idx.columns) for idx in model.__table__.indexes
    }
    assert ("correlation_id",) in index_cols


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_kis_live_ledger_persists_correlation_id(db_session):
    from sqlalchemy import select

    from app.mcp_server.tooling.kis_live_ledger import (
        _order_session_factory,
        _save_kis_live_order_ledger,
    )
    from app.models.review import KISLiveOrderLedger

    ledger_id = await _save_kis_live_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=1.0,
        price=70000.0,
        amount=70000.0,
        currency="KRW",
        order_no="TEST-CORR-1",
        order_time=None,
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={},
        reason=None,
        thesis="t",
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        exit_reason=None,
        indicators_snapshot=None,
        correlation_id="live:kis_live:deadbeefdeadbeef",
    )
    async with _order_session_factory()() as db:
        row = (
            await db.execute(
                select(KISLiveOrderLedger).where(KISLiveOrderLedger.id == ledger_id)
            )
        ).scalar_one()
    assert row.correlation_id == "live:kis_live:deadbeefdeadbeef"
