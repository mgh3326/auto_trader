"""ROB-711 — decision_history service unit tests.

Covers the symbol-keyed aggregation of past judgments, lessons, outcomes,
fills, open claims, and Brier calibration used to inject per-symbol context
into ``analyze_stock_batch`` responses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.services.decision_history import build_decision_context

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


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
