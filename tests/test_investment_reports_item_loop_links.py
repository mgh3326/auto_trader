"""ROB-715 — exact-join batch loaders for item→forecast/retrospective links."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast, TradeRetrospective
from app.services.investment_reports.item_loop_links import (
    list_forecasts_for_item_uuids,
    list_retrospectives_for_item_uuids,
)


def _forecast(item_uuid: str, **kw) -> TradeForecast:
    base = dict(
        created_by="claude",
        symbol="000660",
        instrument_type="equity_kr",
        forecast_target={
            "kind": "price_target",
            "direction": "at_or_above",
            "target_price": 200000,
        },
        probability=Decimal("0.6"),
        review_date=date(2026, 7, 20),
        status="open",
        outcome=None,
        report_item_uuid=item_uuid,
    )
    base.update(kw)
    return TradeForecast(**base)


def _retro(item_uuid: str, **kw) -> TradeRetrospective:
    base = dict(
        symbol="000660",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        lesson="cut the position too late",
        report_item_uuid=item_uuid,
    )
    base.update(kw)
    return TradeRetrospective(**base)


@pytest.mark.asyncio
async def test_forecasts_grouped_by_item_uuid_exact_join(
    db_session: AsyncSession,
) -> None:
    item_a = uuid4()
    item_b = uuid4()
    db_session.add(
        _forecast(
            str(item_a),
            status="closed",
            outcome=True,
            brier_score=Decimal("0.09"),
        )
    )
    db_session.add(_forecast(str(item_b)))
    await db_session.flush()

    result = await list_forecasts_for_item_uuids(db_session, [item_a, item_b])

    assert set(result) == {str(item_a), str(item_b)}
    a = result[str(item_a)][0]
    assert a.status == "closed"
    assert a.outcome is True
    assert a.direction == "at_or_above"
    assert a.target_price == 200000.0
    assert a.brier_score == pytest.approx(0.09)


@pytest.mark.asyncio
async def test_forecasts_absent_item_uuid_not_in_dict(
    db_session: AsyncSession,
) -> None:
    item_a = uuid4()
    unlinked = uuid4()
    db_session.add(_forecast(str(item_a)))
    await db_session.flush()

    result = await list_forecasts_for_item_uuids(db_session, [item_a, unlinked])

    assert str(item_a) in result
    assert str(unlinked) not in result


@pytest.mark.asyncio
async def test_retrospectives_grouped_by_item_uuid(db_session: AsyncSession) -> None:
    item_a = uuid4()
    db_session.add(
        _retro(
            str(item_a),
            pnl_pct=Decimal("-3.5"),
            root_cause_class="execution",
        )
    )
    await db_session.flush()

    result = await list_retrospectives_for_item_uuids(db_session, [item_a])

    row = result[str(item_a)][0]
    assert row.outcome == "filled"
    assert row.lesson == "cut the position too late"
    assert row.pnl_pct == pytest.approx(-3.5)
    assert row.root_cause_class == "execution"


@pytest.mark.asyncio
async def test_empty_input_returns_empty_dict(db_session: AsyncSession) -> None:
    assert await list_forecasts_for_item_uuids(db_session, []) == {}
    assert await list_retrospectives_for_item_uuids(db_session, []) == {}
