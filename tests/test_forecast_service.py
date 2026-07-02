# tests/test_forecast_service.py
"""ROB-650 — forecast ledger save/resolve/aggregate integration tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.trade_journal import forecast_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]

_KR_TEST_SYMBOLS = ("998877", "997766")


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeForecast))
    await db_session.execute(
        text("DELETE FROM public.kr_candles_1d WHERE symbol = ANY(:syms)"),
        {"syms": list(_KR_TEST_SYMBOLS)},
    )
    await db_session.commit()


def _price_target(direction: str = "at_or_above", target_price: float = 130.0) -> dict:
    return {
        "kind": "price_target",
        "direction": direction,
        "target_price": target_price,
    }


async def _seed_kr_candles(db: AsyncSession, symbol: str, highs: list[float]) -> None:
    rows = [
        DailyCandleRow(
            time_utc=datetime(2026, 6, 2 + i, tzinfo=UTC),
            symbol=symbol,
            partition="KRX",
            open=h - 5,
            high=h,
            low=h - 10,
            close=h - 2,
            adj_close=None,
            volume=1000.0,
            value=h * 1000.0,
            source="kis",
        )
        for i, h in enumerate(highs)
    ]
    await DailyCandlesRepository(session=db).upsert_rows(market=MarketKey.KR, rows=rows)
    await db.commit()


# --------------------------------------------------------------------------- #
# save validation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_save_rejects_probability_out_of_range(db_session: AsyncSession):
    with pytest.raises(svc.ForecastValidationError):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target=_price_target(),
            probability=1.5,
            review_date="2026-07-15",
        )


@pytest.mark.asyncio
async def test_save_rejects_probability_outside_band(db_session: AsyncSession):
    with pytest.raises(svc.ForecastValidationError):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target=_price_target(),
            probability=0.9,
            probability_range_low=0.5,
            probability_range_high=0.7,
            review_date="2026-07-15",
        )


@pytest.mark.asyncio
async def test_save_rejects_bad_price_target(db_session: AsyncSession):
    with pytest.raises(svc.ForecastValidationError):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target={"kind": "price_target", "direction": "up"},
            probability=0.6,
            review_date="2026-07-15",
        )


@pytest.mark.asyncio
async def test_save_stamps_policy_version_and_normalizes_symbol(
    db_session: AsyncSession,
):
    action, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="brk-b",
        instrument_type="equity_us",
        forecast_target=_price_target(),
        probability=0.65,
        review_date="2026-07-15",
    )
    await db_session.commit()
    assert action == "created"
    assert row.symbol == "BRK.B"
    assert row.policy_version == svc.POLICY_VERSION
    assert row.status == "open"


@pytest.mark.asyncio
async def test_save_idempotent_by_forecast_id_while_open(db_session: AsyncSession):
    a1, row1 = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target=_price_target(),
        probability=0.6,
        review_date="2026-07-15",
        contrary_evidence="v1",
    )
    await db_session.commit()
    fid = str(row1.forecast_id)
    a2, row2 = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target=_price_target(),
        probability=0.6,
        review_date="2026-07-15",
        forecast_id=fid,
        contrary_evidence="v2",
    )
    await db_session.commit()
    assert (a1, a2) == ("created", "updated")
    rows = (await db_session.execute(select(TradeForecast))).scalars().all()
    assert len(rows) == 1
    assert rows[0].contrary_evidence == "v2"


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_manual_requires_evidence(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.6,
        review_date="2026-07-15",
    )
    await db_session.commit()
    with pytest.raises(svc.ForecastValidationError):
        await svc.resolve_forecast(
            db_session,
            forecast_id=str(row.forecast_id),
            persist=True,
            manual_outcome=True,
        )


@pytest.mark.asyncio
async def test_resolve_dry_run_does_not_persist(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.5,
        review_date="2026-07-15",
    )
    await db_session.commit()
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=False,
        manual_outcome=True,
        manual_evidence=["closed above 130"],
    )
    assert result["status"] == "previewed"
    assert result["changed"] is False
    assert result["computed"]["brier_score"] == pytest.approx(0.25)
    await db_session.refresh(row)
    assert row.status == "open"
    assert row.brier_score is None


@pytest.mark.asyncio
async def test_resolve_manual_persists_and_scores(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.8,
        review_date="2026-07-15",
    )
    await db_session.commit()
    result = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        manual_outcome=True,
        manual_observed_value=131.0,
        manual_evidence=["broke resistance"],
    )
    await db_session.commit()
    assert result["status"] == "resolved"
    assert result["changed"] is True
    await db_session.refresh(row)
    assert row.status == "closed"
    assert row.outcome is True
    assert row.resolution_source == "manual"
    # (0.8 - 1)^2 = 0.04
    assert float(row.brier_score) == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_resolve_idempotent_closed_not_rescored(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.8,
        review_date="2026-07-15",
    )
    await db_session.commit()
    await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        manual_outcome=True,
        manual_evidence=["e"],
    )
    await db_session.commit()
    # Re-resolve with a contradictory outcome — must be ignored (idempotent).
    second = await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        manual_outcome=False,
        manual_evidence=["e2"],
    )
    await db_session.commit()
    assert second["status"] == "already_closed"
    assert second["changed"] is False
    await db_session.refresh(row)
    assert row.outcome is True
    assert float(row.brier_score) == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_save_cannot_modify_closed(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="005930",
        instrument_type="equity_kr",
        forecast_target={"kind": "thesis_holds"},
        probability=0.8,
        review_date="2026-07-15",
    )
    await db_session.commit()
    await svc.resolve_forecast(
        db_session,
        forecast_id=str(row.forecast_id),
        persist=True,
        manual_outcome=True,
        manual_evidence=["e"],
    )
    await db_session.commit()
    with pytest.raises(svc.ForecastValidationError):
        await svc.save_forecast(
            db_session,
            created_by="claude",
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target={"kind": "thesis_holds"},
            probability=0.9,
            review_date="2026-07-15",
            forecast_id=str(row.forecast_id),
        )


@pytest.mark.asyncio
async def test_resolve_price_target_from_ohlcv(db_session: AsyncSession):
    await _seed_kr_candles(db_session, "998877", highs=[120.0, 131.0, 125.0])
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="998877",
        instrument_type="equity_kr",
        forecast_target=_price_target(direction="at_or_above", target_price=130.0),
        probability=0.7,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()
    result = await svc.resolve_forecast(
        db_session, forecast_id=str(row.forecast_id), persist=True
    )
    await db_session.commit()
    assert result["status"] == "resolved"
    assert result["computed"]["resolution_source"] == "ohlcv_day"
    await db_session.refresh(row)
    assert row.status == "closed"
    assert row.outcome is True
    assert float(row.observed_value) == pytest.approx(131.0)
    # (0.7 - 1)^2 = 0.09
    assert float(row.brier_score) == pytest.approx(0.09)


@pytest.mark.asyncio
async def test_resolve_price_target_unresolved_no_data(db_session: AsyncSession):
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol="997766",
        instrument_type="equity_kr",
        forecast_target=_price_target(),
        probability=0.6,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()
    result = await svc.resolve_forecast(
        db_session, forecast_id=str(row.forecast_id), persist=True
    )
    assert result["status"] == "unresolved_no_data"
    assert result["changed"] is False
    await db_session.refresh(row)
    assert row.status == "open"


# --------------------------------------------------------------------------- #
# calibration aggregate
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_calibration_aggregate_groups_by_created_by(db_session: AsyncSession):
    async def _make(created_by: str, prob: float, outcome: bool) -> None:
        _, row = await svc.save_forecast(
            db_session,
            created_by=created_by,
            symbol="005930",
            instrument_type="equity_kr",
            forecast_target={"kind": "thesis_holds"},
            probability=prob,
            review_date="2026-07-15",
        )
        await db_session.commit()
        await svc.resolve_forecast(
            db_session,
            forecast_id=str(row.forecast_id),
            persist=True,
            manual_outcome=outcome,
            manual_evidence=["e"],
        )
        await db_session.commit()

    await _make("claude", 0.8, True)
    await _make("claude", 0.6, False)
    await _make("gpt", 0.9, True)

    agg = await svc.build_forecast_calibration_aggregate(
        db_session, group_by="created_by"
    )
    assert agg["group_by"] == "created_by"
    groups = {g["group"]: g for g in agg["groups"]}
    assert groups["claude"]["sample_size"] == 2
    assert groups["claude"]["hits"] == 1
    assert groups["gpt"]["sample_size"] == 1
    assert groups["gpt"]["hit_rate"] == pytest.approx(1.0)
    # claude avg brier = ((0.8-1)^2 + (0.6-0)^2)/2 = (0.04 + 0.36)/2 = 0.20
    assert groups["claude"]["avg_brier_score"] == pytest.approx(0.20)
