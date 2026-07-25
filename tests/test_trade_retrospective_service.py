# tests/test_trade_retrospective_service.py
"""ROB-474 — TradeRetrospectiveService save/guard/derive/upsert."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, engine
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
async def test_concurrent_first_upsert_recovers_inside_savepoint():
    cid = f"race-{uuid.uuid4()}"
    base_payload = {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "account_mode": "kis_mock",
        "market": "kr",
        "outcome": "filled",
        "correlation_id": cid,
    }

    async with AsyncSessionLocal() as first, AsyncSessionLocal() as second:
        first_pid = (await first.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        second_pid = (
            await second.execute(text("SELECT pg_backend_pid()"))
        ).scalar_one()

        first_result, _ = await svc.TradeRetrospectiveRepository(first).upsert(
            {**base_payload, "lesson": "first writer"}
        )
        assert first_result == "created"

        second_task = asyncio.create_task(
            svc.TradeRetrospectiveRepository(second).upsert(
                {**base_payload, "lesson": "second writer"}
            )
        )
        try:
            async with engine.connect() as observer:
                for _ in range(200):
                    blockers = (
                        await observer.execute(
                            text("SELECT pg_blocking_pids(:pid)"),
                            {"pid": second_pid},
                        )
                    ).scalar_one()
                    if first_pid in blockers:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("second upsert never blocked on the first insert")

            await first.commit()
            second_result, row = await asyncio.wait_for(second_task, timeout=5)
            assert second_result == "updated"
            assert row.lesson == "second writer"

            # Prove the outer transaction remains usable after race recovery.
            count = (
                await second.execute(
                    select(func.count())
                    .select_from(TradeRetrospective)
                    .where(TradeRetrospective.correlation_id == cid)
                )
            ).scalar_one()
            assert count == 1
            await second.commit()
        finally:
            if not second_task.done():
                second_task.cancel()
                await asyncio.gather(second_task, return_exceptions=True)


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


@pytest.mark.asyncio
async def test_retrospective_derives_fx_fields_from_journal(
    db_session: AsyncSession,
):
    j = TradeJournal(
        symbol="AAPL",
        instrument_type="equity_us",
        side="buy",
        entry_price=Decimal("100"),
        quantity=Decimal("2"),
        thesis="t",
        account_type="live",
        account="toss",
        status="closed",
        exit_price=Decimal("130"),
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal("30"),
        buy_fx_rate=Decimal("1389.3300"),
        sell_fx_rate=Decimal("1503.1900"),
        fx_pnl_krw=Decimal("22772.0000"),
        security_pnl_usd=Decimal("60.0000"),
        security_pnl_krw=Decimal("90191.4000"),
        total_pnl_krw=Decimal("112963.4000"),
        fx_rate_source="reconcile_spot",
        fx_pnl_accuracy="approximate",
    )
    db_session.add(j)
    await db_session.commit()
    await db_session.refresh(j)

    _, row = await svc.save_retrospective(
        db_session,
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="toss_live",
        outcome="filled",
        journal_id=j.id,
    )
    await db_session.commit()

    assert row.realized_pnl == Decimal("60.0000")
    assert row.realized_pnl_currency == "USD"
    assert row.fx_pnl_krw == Decimal("22772.0000")
    assert row.security_pnl_usd == Decimal("60.0000")
    assert row.total_pnl_krw == Decimal("112963.4000")
    assert row.fx_rate_source == "reconcile_spot"
    assert row.fx_pnl_accuracy == "approximate"


@pytest.mark.asyncio
async def test_update_with_journal_id_does_not_overwrite_omitted_manual_values(
    db_session: AsyncSession,
):
    j = await _mock_journal(db_session, cid="journal-update-presence")
    j.buy_fx_rate = Decimal("1389.3300")
    j.fx_rate_source = "reconcile_spot"
    await db_session.commit()

    _, first = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        correlation_id="journal-update-presence",
        journal_id=j.id,
        realized_pnl=Decimal("777.0000"),
        realized_pnl_currency="KRW",
        buy_fx_rate=Decimal("999.0000"),
        fx_rate_source="manual",
    )
    await db_session.commit()
    assert first.realized_pnl == Decimal("777.0000")

    _, updated = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        correlation_id="journal-update-presence",
        journal_id=j.id,
    )
    await db_session.commit()

    assert updated.realized_pnl == Decimal("777.0000")
    assert updated.realized_pnl_source == "caller_supplied"
    assert updated.buy_fx_rate == Decimal("999.0000")
    assert updated.fx_rate_source == "manual"


# ---------------------------------------------------------------------------
# ROB-647 — postmortem structuring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_fields_persist(db_session: AsyncSession):
    action, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="rejected",
        trigger_type="rejected_order",
        root_cause_class="execution",
        guardrail_fired="loss_sell_guard",
        policy_version="p5-v3",
        intended_vs_happened={
            "summary": "order bounced",
            "deviations": [{"dimension": "price", "planned": 100, "actual": 0}],
        },
        next_actions=[{"action": "retry with limit", "issue_id": "ROB-1"}],
    )
    await db_session.commit()
    assert action == "created"
    assert row.trigger_type == "rejected_order"
    assert row.root_cause_class == "execution"
    assert row.guardrail_fired == "loss_sell_guard"
    assert row.policy_version == "p5-v3"
    assert row.intended_vs_happened["deviations"][0]["dimension"] == "price"
    assert row.next_actions[0]["action"] == "retry with limit"
    assert row.next_actions[0]["issue_id"] == "ROB-1"


@pytest.mark.asyncio
async def test_trigger_type_requires_next_actions(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kis_live",
            outcome="filled",
            trigger_type="fill",
        )


@pytest.mark.asyncio
async def test_trigger_type_rejects_empty_next_actions(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kis_live",
            outcome="filled",
            trigger_type="fill",
            next_actions=[],
        )


@pytest.mark.asyncio
async def test_invalid_trigger_type_rejected(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kis_live",
            outcome="filled",
            trigger_type="bogus",
            next_actions=[{"action": "x"}],
        )


@pytest.mark.asyncio
async def test_invalid_root_cause_class_rejected(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kis_live",
            outcome="filled",
            root_cause_class="bogus",
        )


@pytest.mark.asyncio
async def test_invalid_intended_vs_happened_rejected(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session,
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kis_live",
            outcome="filled",
            intended_vs_happened={"unknown_key": 1},
        )


@pytest.mark.asyncio
async def test_next_actions_allowed_without_trigger_type(db_session: AsyncSession):
    # Obligation is conditional: next_actions may exist with no trigger_type.
    _, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        next_actions=[{"action": "watch for pullback"}],
    )
    await db_session.commit()
    assert row.trigger_type is None
    assert row.next_actions[0]["action"] == "watch for pullback"


@pytest.mark.asyncio
async def test_upsert_preserves_omitted_postmortem_fields(db_session: AsyncSession):
    # First: rich postmortem keyed by correlation_id.
    await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        correlation_id="cid-preserve",
        trigger_type="fill",
        root_cause_class="analysis",
        next_actions=[{"action": "hold"}],
    )
    await db_session.commit()

    # Second: idempotent re-save (e.g. lean outcome update) omitting postmortem.
    action, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        correlation_id="cid-preserve",
        pnl_pct=2.0,
    )
    await db_session.commit()
    assert action == "updated"
    assert row.trigger_type == "fill"
    assert row.root_cause_class == "analysis"
    assert row.next_actions[0]["action"] == "hold"
    assert float(row.pnl_pct) == 2.0


@pytest.mark.asyncio
async def test_serialize_includes_postmortem_fields(db_session: AsyncSession):
    _, row = await svc.save_retrospective(
        db_session,
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="cancelled",
        trigger_type="expired",
        next_actions=[{"action": "resubmit tomorrow"}],
    )
    await db_session.commit()
    data = svc.serialize_retrospective(row)
    assert data["trigger_type"] == "expired"
    assert data["next_actions"][0]["action"] == "resubmit tomorrow"
    assert "intended_vs_happened" in data
    assert "guardrail_fired" in data
    assert "policy_version" in data
