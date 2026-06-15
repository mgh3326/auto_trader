# tests/test_trade_retrospective_service.py
"""ROB-474 — TradeRetrospectiveService save/guard/derive/upsert."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeRetrospective
from app.models.trade_journal import TradeJournal
from app.services.trade_journal import trade_retrospective_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.execute(delete(TradeJournal))
    await db_session.commit()


async def _mock_journal(db, *, cid="j1"):
    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal("50000"),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=cid,
        status="closed",
        exit_price=Decimal("55000"),
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal("10"),
    )
    db.add(j)
    await db.commit()
    await db.refresh(j)
    return j


@pytest.mark.asyncio
async def test_invalid_outcome_rejected(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kis_mock",
            outcome="bogus",
        )


@pytest.mark.asyncio
async def test_invalid_account_mode_rejected(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="bogus_mode",
            outcome="filled",
        )


@pytest.mark.asyncio
async def test_kiwoom_guard_blocks_fabricated_pnl(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kiwoom_mock",
            outcome="filled",
            realized_pnl=1000.0,
            realized_pnl_currency="KRW",
        )


@pytest.mark.asyncio
async def test_kiwoom_forces_no_fill_evidence(db_session: AsyncSession):
    action, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kiwoom_mock",
        outcome="unfilled",
    )
    await db_session.commit()
    assert action == "created"
    assert row.fill_evidence_available is False


@pytest.mark.asyncio
async def test_caller_supplied_realized_pnl(db_session: AsyncSession):
    action, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        realized_pnl=12345.67,
        realized_pnl_currency="KRW",
    )
    await db_session.commit()
    assert row.realized_pnl == Decimal("12345.6700")
    assert row.realized_pnl_source == "caller_supplied"


@pytest.mark.asyncio
async def test_derive_realized_pnl_from_journal(db_session: AsyncSession):
    j = await _mock_journal(db_session, cid="j1")
    action, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        side="buy",
        journal_id=j.id,
        realized_pnl_currency="KRW",
    )
    await db_session.commit()
    # (55000 - 50000) * 10 = 50000
    assert row.realized_pnl == Decimal("50000.0000")
    assert row.realized_pnl_source == "derived_from_journal"


@pytest.mark.asyncio
async def test_upsert_idempotent_by_correlation_id(db_session: AsyncSession):
    a1, _ = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        correlation_id="dup",
        lesson="v1",
    )
    await db_session.commit()
    a2, _ = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        correlation_id="dup",
        lesson="v2",
    )
    await db_session.commit()
    assert a1 == "created"
    assert a2 == "updated"
    rows = (
        (
            await db_session.execute(
                select(TradeRetrospective).where(
                    TradeRetrospective.correlation_id == "dup"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].lesson == "v2"


@pytest.mark.asyncio
async def test_null_correlation_id_appends(db_session: AsyncSession):
    for _ in range(2):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kis_mock",
            outcome="filled",
        )
        await db_session.commit()
    rows = (await db_session.execute(select(TradeRetrospective))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_derive_realized_pnl_sell_side(db_session: AsyncSession):
    # buy journal (entry 50000 -> exit 55000, qty 10) but retro side='sell' (short):
    # (entry - exit) * qty = (50000 - 55000) * 10 = -50000
    j = await _mock_journal(db_session, cid="js")
    _, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        side="sell",
        journal_id=j.id,
        realized_pnl_currency="KRW",
    )
    await db_session.commit()
    assert row.realized_pnl == Decimal("-50000.0000")
    assert row.realized_pnl_source == "derived_from_journal"


@pytest.mark.asyncio
async def test_derive_uses_journal_side_when_side_none(db_session: AsyncSession):
    # journal itself is a sell; retro omits side -> derivation falls back to j.side
    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="sell",
        entry_price=Decimal("50000"),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id="js2",
        status="closed",
        exit_price=Decimal("55000"),
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal("-10"),
    )
    db_session.add(j)
    await db_session.commit()
    await db_session.refresh(j)
    _, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        side=None,
        journal_id=j.id,
        realized_pnl_currency="KRW",
    )
    await db_session.commit()
    assert row.realized_pnl == Decimal("-50000.0000")


@pytest.mark.asyncio
async def test_kiwoom_rejects_fill_price(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kiwoom_mock",
            outcome="filled",
            fill_price=55000.0,
        )


@pytest.mark.asyncio
async def test_realized_pnl_currency_inferred_krw(db_session: AsyncSession):
    # currency omitted -> inferred from instrument_type so the amount is countable
    _, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        realized_pnl=100.0,
    )
    await db_session.commit()
    assert row.realized_pnl_currency == "KRW"
    assert row.realized_pnl_source == "caller_supplied"


@pytest.mark.asyncio
async def test_realized_pnl_currency_inferred_usd(db_session: AsyncSession):
    _, row = await svc.save_retrospective(
        db_session,
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="kis_live",
        outcome="filled",
        realized_pnl=12.5,
    )
    await db_session.commit()
    assert row.realized_pnl_currency == "USD"


@pytest.mark.asyncio
async def test_crypto_symbol_dash_preserved(db_session: AsyncSession):
    # crypto keeps its dash (must NOT become KRW.BTC like bare to_db_symbol would)
    _, row = await svc.save_retrospective(
        db_session,
        symbol="krw-btc",
        instrument_type="crypto",
        account_mode="upbit_live",
        outcome="filled",
    )
    await db_session.commit()
    assert row.symbol == "KRW-BTC"


@pytest.mark.asyncio
async def test_equity_us_symbol_dotted(db_session: AsyncSession):
    _, row = await svc.save_retrospective(
        db_session,
        symbol="brk-b",
        instrument_type="equity_us",
        account_mode="kis_live",
        outcome="filled",
    )
    await db_session.commit()
    assert row.symbol == "BRK.B"


@pytest.mark.asyncio
async def test_create_retrospective_records_us_fx_fields(db_session: AsyncSession):
    _, row = await svc.save_retrospective(
        db_session,
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="toss_live",
        outcome="filled",
        buy_fx_rate=1389.33,
        sell_fx_rate=1503.19,
        fx_pnl_krw=22772.0,
        security_pnl_usd=60.0,
        security_pnl_krw=90191.4,
        total_pnl_krw=112963.4,
        fx_rate_source="reconcile_spot",
        fx_pnl_accuracy="approximate",
    )
    await db_session.commit()

    assert row.buy_fx_rate == Decimal("1389.3300")
    assert row.sell_fx_rate == Decimal("1503.1900")
    assert row.fx_pnl_krw == Decimal("22772.0000")
    assert row.fx_rate_source == "reconcile_spot"
    assert row.fx_pnl_accuracy == "approximate"
