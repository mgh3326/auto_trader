"""ROB-711 — decision_history service unit tests.

Covers the symbol-keyed aggregation of past judgments, lessons, outcomes,
fills, open claims, and Brier calibration used to inject per-symbol context
into ``analyze_stock_batch`` responses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeForecast,
    TradeRetrospective,
)
from app.services.decision_history import build_decision_context

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_decision_history(db_session: AsyncSession):
    """Reset review-spine tables we touch (keeps tests parallel-safe via
    investment_reports_cleanup_lock at the module level)."""
    for model in (
        TradeRetrospective,
        TradeForecast,
        KISLiveOrderLedger,
        LiveOrderLedger,
        TossLiveOrderLedger,
    ):
        await db_session.execute(delete(model))
    await db_session.commit()
    try:
        yield
    finally:
        for model in (
            TradeRetrospective,
            TradeForecast,
            KISLiveOrderLedger,
            LiveOrderLedger,
            TossLiveOrderLedger,
        ):
            await db_session.execute(delete(model))
        await db_session.commit()


async def _make_report(db: AsyncSession, **overrides) -> InvestmentReport:

    payload = {
        "report_uuid": uuid.uuid4(),
        "idempotency_key": f"key-{uuid.uuid4()}",
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "t",
        "summary": "s",
        "status": "draft",
    }
    payload.update(overrides)
    row = InvestmentReport(**payload)
    db.add(row)
    await db.flush()
    return row


async def _add_item(db: AsyncSession, report_id: int, **overrides) -> None:
    payload = {
        "report_id": report_id,
        "item_uuid": uuid.uuid4(),
        "idempotency_key": f"item-{uuid.uuid4()}",
        "item_kind": "action",
        "symbol": "005930",
        "intent": "buy_review",
        "rationale": "지지선 눌림 재진입",
        "evidence_snapshot": {},
        "created_at": datetime(2026, 6, 1, tzinfo=UTC),
    }
    payload.update(overrides)
    db.add(InvestmentReportItem(**payload))
    await db.flush()


@pytest.mark.asyncio
async def test_prior_decisions_newest_first_capped_and_smoke_filtered(
    db_session: AsyncSession,
) -> None:
    report = await _make_report(db_session)
    # 8 real + 1 smoke; expect newest-6 real, smoke excluded
    for i in range(8):
        await _add_item(
            db_session,
            report.id,
            symbol="005930",
            confidence=60 + i,
            rationale=f"real decision {i}",
            created_at=datetime(2026, 6, 1 + i, tzinfo=UTC),
        )
    await _add_item(
        db_session,
        report.id,
        symbol="005930",
        rationale="Smoke-only action review item",
        created_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    await db_session.commit()

    ctx = await build_decision_context(db_session, symbol="005930", market="kr")

    assert ctx is not None
    assert ctx["symbol"] == "005930"
    assert ctx["market"] == "kr"
    assert ctx["link_quality"] == "symbol_window"
    decisions = ctx["prior_decisions"]
    assert len(decisions) == 6  # capped
    assert decisions[0]["rationale"] == "real decision 7"  # newest first
    assert all("Smoke" not in d["rationale"] for d in decisions)  # smoke excluded


async def _add_retro(db: AsyncSession, **overrides) -> None:
    payload = {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "account_mode": "upbit_live",
        "outcome": "filled",
        "side": "sell",
        "strategy_key": "resistance_ladder",
        "correlation_id": f"live:{uuid.uuid4()}",
        "realized_pnl": Decimal("33914.0000"),
        "realized_pnl_currency": "KRW",
        "realized_pnl_source": "caller_supplied",
        "pnl_pct": Decimal("11.9000"),
        "trigger_type": "fill",
        "lesson": "앵커+러너 분할이 작동",
        "next_strategy": None,
    }
    payload.update(overrides)
    db.add(TradeRetrospective(**payload))
    await db.flush()


@pytest.mark.asyncio
async def test_lessons_and_outcomes_smoke_filtered_and_capped(
    db_session: AsyncSession,
) -> None:
    await _add_retro(db_session, symbol="KRW-JUP", lesson="real lesson A")
    await _add_retro(db_session, symbol="KRW-JUP", lesson="real lesson B")
    # smoke row: created_by_profile carries SMOKE marker
    await _add_retro(
        db_session,
        symbol="KRW-JUP",
        created_by_profile="HERMES_OPERATOR_SMOKE",
        strategy_key="rob474_smoke_x",
        correlation_id="rob474-smoke-x",
        lesson="correlation_id upsert is idempotent",
    )
    await db_session.commit()

    ctx = await build_decision_context(db_session, symbol="KRW-JUP", market="crypto")

    assert ctx is not None
    assert "real lesson A" in ctx["prior_lessons"]
    assert "real lesson B" in ctx["prior_lessons"]
    assert all("idempotent" not in les for les in ctx["prior_lessons"])
    assert len(ctx["realized_outcomes"]) == 2  # smoke excluded
    first = ctx["realized_outcomes"][0]
    assert first["pnl_pct"] == 11.9
    assert first["realized_pnl"] == 33914.0
    assert first["outcome"] == "filled"


@pytest.mark.asyncio
async def test_recent_fills_and_open_claims(db_session: AsyncSession) -> None:
    db_session.add(
        KISLiveOrderLedger(
            trade_date=datetime(2026, 6, 10, tzinfo=UTC),
            symbol="000660",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            account_mode="kis_live",
            broker="kis",
            status="filled",
            lifecycle_state="filled",
            order_no="A1",
            quantity=Decimal("1"),
            filled_qty=Decimal("1"),
            avg_fill_price=Decimal("2000000"),
            target_price=Decimal("3035000"),
            stop_loss=Decimal("1888000"),
        )
    )
    db_session.add(
        TradeForecast(
            created_by="claude",
            symbol="000660",
            instrument_type="equity_kr",
            forecast_target={
                "kind": "price_target",
                "direction": "at_or_above",
                "target_price": "2463000",
            },
            probability=Decimal("0.55"),
            horizon="10 trading days",
            review_date=datetime(2026, 7, 17).date(),
            status="open",
        )
    )
    await db_session.commit()

    ctx = await build_decision_context(db_session, symbol="000660", market="kr")

    assert ctx is not None
    assert len(ctx["recent_fills"]) == 1
    fill = ctx["recent_fills"][0]
    assert fill["side"] == "buy"
    assert fill["avg_fill_price"] == 2000000.0
    assert fill["stop_loss"] == 1888000.0
    assert fill["source"] == "kis"
    assert len(ctx["open_claims"]) == 1
    claim = ctx["open_claims"][0]
    assert claim["direction"] == "at_or_above"
    assert claim["target_price"] == "2463000"
    assert claim["review_date"] == "2026-07-17"


@pytest.mark.asyncio
async def test_brier_insufficient_sample_and_empty_returns_none(
    db_session: AsyncSession,
) -> None:
    # symbol with zero scored forecasts → insufficient_sample
    report = await _make_report(db_session)
    await _add_item(db_session, report.id, symbol="000660", rationale="real")
    await db_session.commit()

    ctx = await build_decision_context(db_session, symbol="000660", market="kr")
    assert ctx is not None
    assert ctx["running_brier_symbol"] == {
        "n": 0,
        "mean_brier": None,
        "flag": "insufficient_sample",
    }
    assert ctx["running_brier_global"]["flag"] == "insufficient_sample"

    # a symbol with no history anywhere → None (nothing to inject)
    empty = await build_decision_context(db_session, symbol="ZZZZZ", market="kr")
    assert empty is None
