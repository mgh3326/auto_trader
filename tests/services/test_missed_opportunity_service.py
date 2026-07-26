"""ROB-1017 — mandatory D+5 forecasts for zero-entry volatile sessions."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast, TradeRetrospective
from app.services.daily_candles.repository import DailyCandleRow
from app.services.trade_journal import forecast_service
from app.services.trade_journal import missed_opportunity_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeForecast))
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()


def _candidate(symbol: str = "005930", *, probability: float = 0.7) -> dict:
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "reference_price": 100.0,
        "target_return_pct": 2.0,
        "probability": probability,
        "rejection_reason": "deep limit never reached",
        "evidence_ids": ["artifact:rob1017"],
    }


def _kwargs(**overrides):
    values = {
        "created_by": "codex",
        "market": "kr",
        "session_date": "2026-07-21",
        "account_mode": "toss_live",
        "index_symbol": "KOSPI",
        "index_change_pct": 2.01,
        "new_buy_count": 0,
        "candidates": [_candidate()],
        "session_label": "rob1017-zero-entry",
        "top_n": 1,
    }
    values.update(overrides)
    return values


def test_storage_service_imports_no_broker_order_or_scheduler_modules():
    tree = ast.parse(Path(svc.__file__).read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    forbidden_prefixes = (
        "app.core.taskiq_broker",
        "app.jobs",
        "app.mcp_server.tooling.orders",
        "app.services.brokers",
        "app.tasks",
    )
    assert not {
        module for module in imported_modules if module.startswith(forbidden_prefixes)
    }


@pytest.mark.asyncio
async def test_gate_is_strict_and_writes_nothing_when_not_required(
    db_session: AsyncSession,
):
    at_boundary = await svc.save_missed_opportunities(
        db_session, **_kwargs(index_change_pct=2.0, candidates=[])
    )
    with_a_buy = await svc.save_missed_opportunities(
        db_session, **_kwargs(index_change_pct=-3.0, new_buy_count=1, candidates=[])
    )
    await db_session.commit()

    assert at_boundary["required"] is False
    assert with_a_buy["required"] is False
    assert (
        await db_session.scalar(
            select(TradeForecast).with_only_columns(TradeForecast.id).limit(1)
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(TradeRetrospective).with_only_columns(TradeRetrospective.id).limit(1)
        )
        is None
    )


@pytest.mark.asyncio
async def test_required_session_publishes_linked_top_n_d5_rows(
    db_session: AsyncSession,
):
    result = await svc.save_missed_opportunities(db_session, **_kwargs())
    await db_session.commit()

    forecasts = (await db_session.execute(select(TradeForecast))).scalars().all()
    retros = (await db_session.execute(select(TradeRetrospective))).scalars().all()
    assert result["required"] is True
    assert result["review_date"] == "2026-07-28"  # fifth XKRX session after D0
    assert result["forecast_count"] == 1
    assert len(forecasts) == len(retros) == 1
    forecast = forecasts[0]
    retro = retros[0]
    assert forecast.review_date == date(2026, 7, 28)
    assert forecast.horizon == "D+5 trading sessions"
    assert forecast.forecast_target["kind"] == "return_at_horizon"
    assert forecast.forecast_target["rank"] == 1
    assert retro.trigger_type == "missed_opportunity"
    assert retro.outcome == "unfilled"
    assert retro.correlation_id == forecast.correlation_id
    assert retro.evidence_snapshot["forecast_id"] == str(forecast.forecast_id)


@pytest.mark.asyncio
async def test_exact_retry_is_idempotent(db_session: AsyncSession):
    first = await svc.save_missed_opportunities(db_session, **_kwargs())
    await db_session.commit()
    second = await svc.save_missed_opportunities(db_session, **_kwargs())
    await db_session.commit()

    assert first["entries"][0]["forecast_action"] == "created"
    assert second["entries"][0]["forecast_action"] == "updated"
    assert len((await db_session.execute(select(TradeForecast))).scalars().all()) == 1
    assert (
        len((await db_session.execute(select(TradeRetrospective))).scalars().all()) == 1
    )


@pytest.mark.asyncio
async def test_invalid_required_batch_is_atomic(db_session: AsyncSession):
    with pytest.raises(svc.MissedOpportunityValidationError, match="exactly top_n"):
        await svc.save_missed_opportunities(
            db_session,
            **_kwargs(top_n=2, candidates=[_candidate()]),
        )
    await db_session.rollback()

    assert (await db_session.execute(select(TradeForecast))).scalars().all() == []
    assert (await db_session.execute(select(TradeRetrospective))).scalars().all() == []


@pytest.mark.asyncio
async def test_resolving_d5_return_scores_linked_missed_retrospective(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    saved = await svc.save_missed_opportunities(db_session, **_kwargs())
    await db_session.commit()
    forecast_id = saved["entries"][0]["forecast_id"]

    async def _candles(*_args, **_kwargs):
        return [
            DailyCandleRow(
                time_utc=datetime(2026, 7, 28, tzinfo=UTC),
                symbol="005930",
                partition="KRX",
                open=108.0,
                high=112.0,
                low=107.0,
                close=110.0,
                adj_close=None,
                volume=1_000.0,
                value=110_000.0,
                source="test",
            )
        ]

    monkeypatch.setattr(forecast_service, "_read_window_candles", _candles)
    resolved = await forecast_service.resolve_forecast(
        db_session,
        forecast_id=forecast_id,
        persist=True,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    await db_session.commit()

    retro = (await db_session.execute(select(TradeRetrospective))).scalar_one()
    assert resolved["computed"]["observed_value"] == pytest.approx(10.0)
    assert resolved["retrospective_synced"] is True
    assert float(retro.pnl_pct) == pytest.approx(10.0)
    assert retro.evidence_snapshot["resolution"]["horizon_close"] == 110.0
