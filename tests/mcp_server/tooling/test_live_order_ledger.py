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

