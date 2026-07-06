# tests/services/test_trade_retrospective_mirror_correlation.py
import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import TradeRetrospective
from app.services.trade_journal.trade_retrospective_service import (
    save_retrospective,
)

_CORRELATION_IDS = ("mirror:item-1", "mirror:item-2")


@pytest_asyncio.fixture(autouse=True)
async def _clean_trade_retrospective_rows(db_session):
    await db_session.execute(
        delete(TradeRetrospective).where(
            TradeRetrospective.correlation_id.in_(_CORRELATION_IDS)
        )
    )
    await db_session.commit()
    yield
    await db_session.execute(
        delete(TradeRetrospective).where(
            TradeRetrospective.correlation_id.in_(_CORRELATION_IDS)
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_same_correlation_id_allowed_for_live_and_mock(db_session):
    common = {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "outcome": "filled",
        "side": "buy",
        "correlation_id": "mirror:item-1",
    }
    first, live = await save_retrospective(
        db_session,
        **common,
        account_mode="kis_live",
        realized_pnl=1000,
        realized_pnl_currency="KRW",
    )
    second, mock = await save_retrospective(
        db_session,
        **common,
        account_mode="kis_mock",
        realized_pnl=1500,
        realized_pnl_currency="KRW",
    )
    await db_session.commit()

    assert first == "created"
    assert second == "created"
    assert live.id != mock.id
    assert live.correlation_id == mock.correlation_id
    assert live.account_mode == "kis_live"
    assert mock.account_mode == "kis_mock"


@pytest.mark.asyncio
async def test_same_correlation_id_same_account_updates(db_session):
    common = {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "account_mode": "kis_mock",
        "outcome": "filled",
        "side": "buy",
        "correlation_id": "mirror:item-2",
        "realized_pnl_currency": "KRW",
    }
    created, row1 = await save_retrospective(db_session, **common, realized_pnl=1000)
    updated, row2 = await save_retrospective(db_session, **common, realized_pnl=2000)
    await db_session.commit()

    assert created == "created"
    assert updated == "updated"
    assert row1.id == row2.id
    assert float(row2.realized_pnl) == 2000.0
