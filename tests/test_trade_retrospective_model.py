# tests/test_trade_retrospective_model.py
"""ROB-474 — review.trade_retrospectives model round-trip."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeRetrospective

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()


@pytest.mark.asyncio
async def test_insert_and_read_back(db_session: AsyncSession):
    row = TradeRetrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        side="buy",
        strategy_key="oversold_bounce",
        correlation_id="cid-1",
        realized_pnl=Decimal("12345.6700"),
        realized_pnl_currency="KRW",
        realized_pnl_source="caller_supplied",
        pnl_pct=Decimal("3.2100"),
        lesson="hold longer",
        next_strategy="scale in on dip",
    )
    db_session.add(row)
    await db_session.commit()

    got = (
        await db_session.execute(
            select(TradeRetrospective).where(
                TradeRetrospective.correlation_id == "cid-1"
            )
        )
    ).scalar_one()
    assert got.account_mode == "kis_mock"
    assert got.outcome == "filled"
    assert got.realized_pnl == Decimal("12345.6700")
    assert got.fill_evidence_available is True  # server_default
    assert got.created_at is not None


def test_trade_retrospective_us_fx_columns_present():
    cols = set(TradeRetrospective.__table__.columns.keys())
    for col in (
        "buy_fx_rate",
        "sell_fx_rate",
        "fx_pnl_krw",
        "security_pnl_usd",
        "security_pnl_krw",
        "total_pnl_krw",
        "fx_rate_source",
        "fx_pnl_accuracy",
    ):
        assert col in cols, f"missing column {col}"


def test_trade_retrospective_account_mode_constraint_matches_migration():
    constraint_names = {c.name for c in TradeRetrospective.__table__.constraints}
    assert "ck_trade_retrospectives_account_mode" in constraint_names
