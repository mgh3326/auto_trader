# tests/test_trade_retrospective_web_read.py
"""ROB-662 — web read helpers: filters + total + next_actions aggregation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeRetrospective
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
    await db_session.commit()


async def _add(
    db: AsyncSession,
    *,
    symbol: str,
    market: str,
    outcome: str = "filled",
    trigger_type: str | None = None,
    root_cause_class: str | None = None,
    realized_pnl: Decimal | None = None,
    next_actions: list | None = None,
    correlation_id: str,
) -> TradeRetrospective:
    row = TradeRetrospective(
        symbol=symbol,
        instrument_type="equity_kr" if market == "kr" else "equity_us",
        account_mode="kis_mock",
        market=market,
        outcome=outcome,
        trigger_type=trigger_type,
        root_cause_class=root_cause_class,
        realized_pnl=realized_pnl,
        next_actions=next_actions,
        correlation_id=correlation_id,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_get_retrospectives_filters_by_trigger_and_root_cause(
    db_session: AsyncSession,
):
    await _add(db_session, symbol="005930", market="kr", trigger_type="fill",
               root_cause_class="analysis", correlation_id="a")
    await _add(db_session, symbol="000660", market="kr", trigger_type="rejected_order",
               root_cause_class="execution", correlation_id="b")

    res = await svc.get_retrospectives(db_session, trigger_type="fill")
    assert [e["symbol"] for e in res["entries"]] == ["005930"]

    res2 = await svc.get_retrospectives(db_session, root_cause_class="execution")
    assert [e["symbol"] for e in res2["entries"]] == ["000660"]


@pytest.mark.asyncio
async def test_get_retrospectives_reports_total_across_pagination(
    db_session: AsyncSession,
):
    for i in range(3):
        await _add(db_session, symbol=f"00{i}000", market="kr", correlation_id=f"c{i}")

    res = await svc.get_retrospectives(db_session, limit=2, offset=0)
    assert res["summary"]["count"] == 2
    assert res["summary"]["total"] == 3

    res2 = await svc.get_retrospectives(db_session, limit=2, offset=2)
    assert res2["summary"]["count"] == 1
    assert res2["summary"]["total"] == 3


@pytest.mark.asyncio
async def test_get_open_next_actions_flattens_and_hides_done(
    db_session: AsyncSession,
):
    await _add(
        db_session, symbol="005930", market="kr", trigger_type="fill",
        realized_pnl=Decimal("1000"), correlation_id="na",
        next_actions=[
            {"action": "재진입 룰 재검토", "status": "open"},
            {"action": "완료된 액션", "status": "done"},
            {"action": "상태 없는 액션"},
        ],
    )

    res = await svc.get_open_next_actions(db_session)
    actions = [i["action"] for i in res["items"]]
    assert "재진입 룰 재검토" in actions
    assert "상태 없는 액션" in actions  # null status = incomplete
    assert "완료된 액션" not in actions  # done hidden
    assert res["scan_limit"] == 200
    # parent context enriched
    first = next(i for i in res["items"] if i["action"] == "재진입 룰 재검토")
    assert first["symbol"] == "005930"
    assert first["market"] == "kr"
    assert first["trigger_type"] == "fill"
    assert first["realized_pnl"] == 1000.0
    assert first["correlation_id"] == "na"


@pytest.mark.asyncio
async def test_get_open_next_actions_scopes_by_symbol_and_status(
    db_session: AsyncSession,
):
    await _add(db_session, symbol="005930", market="kr", correlation_id="s1",
               next_actions=[{"action": "A", "status": "in_progress"}])
    await _add(db_session, symbol="000660", market="kr", correlation_id="s2",
               next_actions=[{"action": "B", "status": "open"}])

    res = await svc.get_open_next_actions(db_session, symbol="005930")
    assert [i["action"] for i in res["items"]] == ["A"]

    res2 = await svc.get_open_next_actions(db_session, statuses=frozenset({"open"}))
    assert [i["action"] for i in res2["items"]] == ["B"]
