# tests/test_forecast_web_read.py
"""ROB-663 — forecast web read helpers: due-queue ordering + recent scored history."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast
from app.services.trade_journal import forecast_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()


def _target() -> dict:
    return {"kind": "price_target", "direction": "at_or_above", "target_price": 130.0}


async def _add(
    db: AsyncSession,
    *,
    symbol: str,
    instrument_type: str = "equity_kr",
    created_by: str = "claude",
    review_date: date,
    status: str = "open",
    probability: float = 0.6,
    outcome: bool | None = None,
    brier_score: float | None = None,
    resolved_at: datetime | None = None,
) -> TradeForecast:
    row = TradeForecast(
        created_by=created_by,
        symbol=symbol,
        instrument_type=instrument_type,
        forecast_target=_target(),
        probability=Decimal(str(probability)),
        review_date=review_date,
        status=status,
        outcome=outcome,
        brier_score=Decimal(str(brier_score)) if brier_score is not None else None,
        resolved_at=resolved_at,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_list_open_forecasts_orders_by_review_date_asc(db_session: AsyncSession):
    await _add(db_session, symbol="000660", review_date=date(2026, 7, 20))
    await _add(db_session, symbol="005930", review_date=date(2026, 7, 5))
    await _add(db_session, symbol="035720", review_date=date(2026, 7, 12))
    await db_session.commit()

    result = await svc.list_open_forecasts(db_session)
    symbols = [e["symbol"] for e in result["entries"]]
    # soonest (and overdue) review dates first — the scoring-due queue
    assert symbols == ["005930", "035720", "000660"]
    assert result["summary"]["count"] == 3


@pytest.mark.asyncio
async def test_list_open_forecasts_excludes_closed_and_filters_symbol(
    db_session: AsyncSession,
):
    await _add(db_session, symbol="005930", review_date=date(2026, 7, 5))
    await _add(
        db_session,
        symbol="005930",
        review_date=date(2026, 6, 1),
        status="closed",
        outcome=True,
        brier_score=0.04,
        resolved_at=datetime(2026, 6, 2, tzinfo=UTC),
    )
    await _add(db_session, symbol="000660", review_date=date(2026, 7, 6))
    await db_session.commit()

    result = await svc.list_open_forecasts(db_session, symbol="005930")
    assert [e["symbol"] for e in result["entries"]] == ["005930"]
    assert result["entries"][0]["status"] == "open"


@pytest.mark.asyncio
async def test_list_open_forecasts_normalizes_us_symbol(db_session: AsyncSession):
    await _add(
        db_session,
        symbol="BRK.B",
        instrument_type="equity_us",
        review_date=date(2026, 7, 8),
    )
    await db_session.commit()

    # query in Yahoo/dash form still matches the stored dot form
    result = await svc.list_open_forecasts(db_session, symbol="BRK-B")
    assert [e["symbol"] for e in result["entries"]] == ["BRK.B"]


@pytest.mark.asyncio
async def test_list_closed_forecasts_orders_by_resolved_at_desc(
    db_session: AsyncSession,
):
    await _add(
        db_session,
        symbol="005930",
        review_date=date(2026, 6, 1),
        status="closed",
        outcome=True,
        brier_score=0.04,
        resolved_at=datetime(2026, 6, 2, tzinfo=UTC),
    )
    await _add(
        db_session,
        symbol="000660",
        review_date=date(2026, 6, 10),
        status="closed",
        outcome=False,
        brier_score=0.81,
        resolved_at=datetime(2026, 6, 15, tzinfo=UTC),
    )
    # an open row must never appear in the closed listing
    await _add(db_session, symbol="035720", review_date=date(2026, 7, 1))
    await db_session.commit()

    result = await svc.list_closed_forecasts(db_session)
    entries = result["entries"]
    assert [e["symbol"] for e in entries] == ["000660", "005930"]
    assert entries[0]["outcome"] is False
    assert entries[0]["brier_score"] == 0.81
    assert all(e["status"] == "closed" for e in entries)
