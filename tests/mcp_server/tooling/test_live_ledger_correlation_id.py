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
    index_cols = {tuple(c.name for c in idx.columns) for idx in model.__table__.indexes}
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_live_order_us_buy_mints_corr_and_publishes(monkeypatch):
    from app.mcp_server.tooling import live_order_ledger as mod

    seen = {}

    async def spy_publish(**kwargs):
        seen.update(kwargs)
        return "fc-us-1"

    monkeypatch.setattr(mod, "publish_place_time_forecast", spy_publish)

    res = await mod._record_live_order(
        broker="kis",
        account_scope="kis_live",
        market="us",
        normalized_symbol="AAPL",
        exchange="NASD",
        market_symbol=None,
        side="buy",
        order_kind="limit",
        currency="USD",
        order_no="US-CORR-1",
        order_time=None,
        rt_cd="0",
        response_message=None,
        dry_run_result={"price": 190.0, "quantity": 2, "estimated_value": 380.0},
        execution_result={"rt_cd": "0"},
        reason=None,
        exit_reason=None,
        thesis="t",
        strategy=None,
        target_price=210.0,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
    )
    assert res["correlation_id"].startswith("live:kis_live:")
    assert seen["instrument_type"] == "equity_us"
    assert seen["side"] == "buy"
    assert seen["target_price"] == 210.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_toss_place_kr_buy_mints_and_publishes(monkeypatch):
    from decimal import Decimal as D

    from app.mcp_server.tooling import toss_live_ledger as mod

    seen = {}
    original_correlation = mod.live_correlation_id

    def spy_correlation(**kwargs):
        assert kwargs["rung"] == 3
        return original_correlation(**kwargs)

    async def spy_publish(**kwargs):
        seen.update(kwargs)
        return "fc-toss-1"

    async def fake_record_send(self, **kwargs):
        class _Row:
            id = 1
            status = "accepted"

        fake_record_send.kwargs = kwargs
        return _Row()

    monkeypatch.setattr(mod, "publish_place_time_forecast", spy_publish)
    monkeypatch.setattr(mod, "live_correlation_id", spy_correlation)
    monkeypatch.setattr(
        "app.services.toss_live_order_ledger_service."
        "TossLiveOrderLedgerService.record_send",
        fake_record_send,
    )

    res = await mod.record_toss_place_order(
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="day",
        quantity=D("1"),
        price=D("70000"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-1",
        broker_order_id="bord-1",
        raw_response={},
        reason=None,
        exit_reason=None,
        thesis="t",
        strategy=None,
        target_price=D("80000"),
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        report_item_uuid=None,
        rung=3,
    )
    assert res["correlation_id"].startswith("live:toss_live:")
    assert fake_record_send.kwargs["correlation_id"] == res["correlation_id"]
    assert seen["instrument_type"] == "equity_kr"
    assert seen["target_price"] == 80000.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_toss_place_uses_internal_correlation_override_and_rung(
    monkeypatch,
):
    from decimal import Decimal as D

    from app.mcp_server.tooling import toss_live_ledger as mod

    async def fake_publish(**kwargs):
        assert kwargs["correlation_id"] == "proposal-correlation-r1"
        return "fc-toss-r1"

    async def fake_record_send(self, **kwargs):
        fake_record_send.kwargs = kwargs

        class _Row:
            id = 2
            status = "accepted"

        return _Row()

    monkeypatch.setattr(mod, "publish_place_time_forecast", fake_publish)
    monkeypatch.setattr(
        "app.services.toss_live_order_ledger_service."
        "TossLiveOrderLedgerService.record_send",
        fake_record_send,
    )
    result = await mod.record_toss_place_order(
        market="kr",
        symbol="005930",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=D("1"),
        price=D("70000"),
        order_amount=None,
        currency="KRW",
        client_order_id="tosprop-r1",
        broker_order_id="broker-r1",
        raw_response={},
        reason=None,
        exit_reason=None,
        thesis="proposal",
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        report_item_uuid=None,
        correlation_id_override="proposal-correlation-r1",
        rung=1,
    )

    assert result["correlation_id"] == "proposal-correlation-r1"
    assert fake_record_send.kwargs["correlation_id"] == "proposal-correlation-r1"
