# tests/test_rob1283_negative_class_db.py
"""ROB-1283 — DB-level proof that the negative class records, queries, and scores.

The point of the column is not that it stores a string. It is that a rejected
candidate recorded through ``forecast_save`` is later *findable* and *gradeable*
by the same machinery that grades acted-on calls — otherwise ROB-1301's A/B
comparison inherits a cohort it cannot score, which is the orphan-record failure
this issue exists to prevent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast
from app.services.trade_journal import forecast_service as svc
from app.services.trade_journal.negative_class import (
    NEGATIVE_CLASS_BUCKET,
    load_negative_class_health,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]

_TOUCH_RULE_VERSION = "window-touch-v1-high-gte-low-lte"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()


def _target(target_price: float = 130.0) -> dict:
    return {
        "kind": "price_target",
        "direction": "at_or_above",
        "target_price": target_price,
        "outcome_rule_version": _TOUCH_RULE_VERSION,
    }


async def _save(db: AsyncSession, **overrides):
    kwargs = {
        "created_by": "kr-open-trade",
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "forecast_target": _target(),
        "probability": 0.3,
        "review_date": "2026-09-01",
    }
    kwargs.update(overrides)
    action, row = await svc.save_forecast(db, **kwargs)
    await db.commit()
    await db.refresh(row)
    return action, row


async def test_negative_class_forecast_persists_and_serialises(
    db_session: AsyncSession,
) -> None:
    _, row = await _save(db_session, decision_bucket=NEGATIVE_CLASS_BUCKET)
    assert row.decision_bucket == NEGATIVE_CLASS_BUCKET
    assert svc.serialize_forecast(row)["decision_bucket"] == NEGATIVE_CLASS_BUCKET


async def test_bucket_defaults_to_null_so_existing_callers_are_unaffected(
    db_session: AsyncSession,
) -> None:
    """Back-compat: every pre-existing forecast_save call keeps working."""
    _, row = await _save(db_session)
    assert row.decision_bucket is None


async def test_unknown_bucket_is_rejected_before_the_write(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(svc.ForecastValidationError):
        await _save(db_session, decision_bucket="defered_no_action")
    await db_session.rollback()
    remaining = (await db_session.execute(select(TradeForecast))).scalars().all()
    assert remaining == []


async def test_unjoinable_report_link_is_rejected_before_the_write(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(svc.ForecastValidationError):
        await _save(db_session, report_item_uuid="not-a-uuid")
    await db_session.rollback()
    assert (await db_session.execute(select(TradeForecast))).scalars().all() == []


async def test_report_link_survives_the_round_trip_and_can_join(
    db_session: AsyncSession,
) -> None:
    item_uuid = str(uuid.uuid4())
    _, row = await _save(
        db_session,
        decision_bucket=NEGATIVE_CLASS_BUCKET,
        report_item_uuid=item_uuid.upper(),
    )
    # Stored canonically, so a string-equality join against the item table holds.
    assert row.report_item_uuid == item_uuid
    found = (
        (
            await db_session.execute(
                select(TradeForecast).where(TradeForecast.report_item_uuid == item_uuid)
            )
        )
        .scalars()
        .all()
    )
    assert len(found) == 1


async def test_cohort_is_queryable_by_bucket(db_session: AsyncSession) -> None:
    await _save(
        db_session, decision_bucket=NEGATIVE_CLASS_BUCKET, forecast_id=uuid.uuid4()
    )
    await _save(
        db_session, decision_bucket="new_buy_candidate", forecast_id=uuid.uuid4()
    )
    await _save(db_session, forecast_id=uuid.uuid4())  # unclassified

    cohort = await svc.list_forecasts(db_session, decision_bucket=NEGATIVE_CLASS_BUCKET)
    assert cohort["summary"]["count"] == 1
    assert cohort["entries"][0]["decision_bucket"] == NEGATIVE_CLASS_BUCKET

    everything = await svc.list_forecasts(db_session)
    breakdown = everything["summary"]["by_decision_bucket"]
    # "unclassified" is counted, not collapsed: it is the size of the blind spot.
    assert breakdown[NEGATIVE_CLASS_BUCKET] == 1
    assert breakdown["new_buy_candidate"] == 1
    assert breakdown["unclassified"] == 1


async def test_typo_in_the_cohort_query_errors_instead_of_returning_empty(
    db_session: AsyncSession,
) -> None:
    """An empty result would read as "nothing was rejected". It must raise."""
    await _save(db_session, decision_bucket=NEGATIVE_CLASS_BUCKET)
    with pytest.raises(svc.ForecastValidationError):
        await svc.list_forecasts(db_session, decision_bucket="deferred")


async def test_negative_class_forecast_is_gradeable_like_any_other(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anti-orphan guarantee ROB-1301's scoring depends on.

    A bucketed forecast must resolve and score through the *same* path as an
    unbucketed one — the bucket is a label on the cohort, never a side channel
    that diverts the row out of scoring.
    """
    from app.services.daily_candles.repository import DailyCandleRow

    async def _fake_window(*args, **kwargs):
        return [
            DailyCandleRow(
                time_utc=datetime(2026, 8, 25, tzinfo=UTC),
                symbol="005930",
                partition="KRX",
                open=125.0,
                high=131.0,
                low=124.0,
                close=130.5,
                adj_close=None,
                volume=1000.0,
                value=130000.0,
                source="kis",
            )
        ]

    monkeypatch.setattr(svc, "_read_window_candles", _fake_window)

    _, row = await _save(
        db_session,
        decision_bucket=NEGATIVE_CLASS_BUCKET,
        review_date="2026-08-25",
        probability=0.3,
    )
    result = await svc.resolve_forecast(
        db_session, forecast_id=row.forecast_id, persist=True, backfill_missing=False
    )
    await db_session.commit()
    await db_session.refresh(row)

    assert result["status"] == "resolved", result
    assert result["computed"]["brier_score"] is not None
    assert row.status == "closed"
    assert row.outcome is True  # high 131 >= target 130
    assert row.brier_score is not None
    # The label survives scoring, so the cohort stays identifiable afterwards.
    assert row.decision_bucket == NEGATIVE_CLASS_BUCKET


async def test_health_reads_the_live_surface(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    health = await load_negative_class_health(db_session, market="kr", now=now)
    assert health.status == "never_recorded"

    await _save(db_session, decision_bucket=NEGATIVE_CLASS_BUCKET)
    health = await load_negative_class_health(db_session, market="kr", now=now)
    assert health.status == "ok"
    assert health.last_source == "forecast"


async def test_health_is_scoped_per_market(db_session: AsyncSession) -> None:
    """A KR rejection must not make US look healthy."""
    now = datetime.now(UTC)
    await _save(db_session, decision_bucket=NEGATIVE_CLASS_BUCKET)
    assert (
        await load_negative_class_health(db_session, market="kr", now=now)
    ).status == "ok"
    assert (
        await load_negative_class_health(db_session, market="us", now=now)
    ).status == "never_recorded"


async def test_unbucketed_forecasts_do_not_count_as_negative_class(
    db_session: AsyncSession,
) -> None:
    """The stall must not be masked by ordinary forecast traffic.

    This is precisely how the real stall hid: ``trade_forecasts`` was busy the
    whole time, so any check that merely asked "are forecasts being written?"
    would have answered yes for 66 days.
    """
    await _save(db_session)
    health = await load_negative_class_health(
        db_session, market="kr", now=datetime.now(UTC)
    )
    assert health.status == "never_recorded"


async def test_stale_bucketed_forecast_reports_stalled(
    db_session: AsyncSession,
) -> None:
    _, row = await _save(db_session, decision_bucket=NEGATIVE_CLASS_BUCKET)
    far_future = datetime.now(UTC).replace(year=datetime.now(UTC).year + 1)
    health = await load_negative_class_health(db_session, market="kr", now=far_future)
    assert health.status == "stalled"
    assert health.stale_days >= 365
    assert date.today() is not None  # sanity: no clock freezing in this module
