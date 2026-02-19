from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.research_backtest import BacktestRunSummary
from app.services.research_gate_service import GateResult
from app.services.research_ingestion_service import ingest_summary_payload


@pytest.mark.asyncio
async def test_ingest_summary_payload_returns_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()

    parsed = BacktestRunSummary(
        run_id="run-20260219-01",
        strategy_name="NFI",
        timeframe="5m",
        runner="mac",
        total_trades=31,
        profit_factor=Decimal("1.3"),
        max_drawdown=Decimal("0.08"),
    )
    monkeypatch.setattr(
        "app.services.research_ingestion_service.parse_backtest_summary",
        lambda payload, strategy_name=None, runner=None: parsed,
    )

    run_row = SimpleNamespace(id=100, run_id=parsed.run_id)
    upsert_backtest_run = AsyncMock(return_value=run_row)
    upsert_promotion_candidate = AsyncMock()
    replace_backtest_pairs = AsyncMock()
    create_sync_job = AsyncMock(return_value=SimpleNamespace(id=1, status="running"))
    update_sync_job_status = AsyncMock()
    monkeypatch.setattr(
        "app.services.research_ingestion_service.upsert_backtest_run",
        upsert_backtest_run,
    )
    monkeypatch.setattr(
        "app.services.research_ingestion_service.upsert_promotion_candidate",
        upsert_promotion_candidate,
    )
    monkeypatch.setattr(
        "app.services.research_ingestion_service.replace_backtest_pairs",
        replace_backtest_pairs,
    )
    monkeypatch.setattr(
        "app.services.research_ingestion_service.create_sync_job",
        create_sync_job,
    )
    monkeypatch.setattr(
        "app.services.research_ingestion_service.update_sync_job_status",
        update_sync_job_status,
    )
    monkeypatch.setattr(
        "app.services.research_ingestion_service.evaluate_candidate",
        lambda **kwargs: GateResult(
            status="PASS",
            reason_code="OK",
            thresholds={"minimum_trade_count": 20.0},
            metrics={"total_trades": 31.0},
        ),
    )

    run_id = await ingest_summary_payload(
        session,
        {"run_id": "run-20260219-01", "total_trades": 31, "profit_factor": 1.3},
        gate_config={"minimum_trade_count": 20},
        source_file="reports/summary.json",
    )

    assert run_id == "run-20260219-01"
    upsert_backtest_run.assert_awaited_once()
    upsert_promotion_candidate.assert_awaited_once()
    create_sync_job.assert_awaited_once()
    update_sync_job_status.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_summary_payload_marks_sync_job_failed_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()

    create_sync_job = AsyncMock(return_value=SimpleNamespace(id=10))
    update_sync_job_status = AsyncMock()
    monkeypatch.setattr(
        "app.services.research_ingestion_service.create_sync_job",
        create_sync_job,
    )
    monkeypatch.setattr(
        "app.services.research_ingestion_service.update_sync_job_status",
        update_sync_job_status,
    )

    def raise_parse(payload, strategy_name=None, runner=None):
        raise ValueError("bad payload")

    monkeypatch.setattr(
        "app.services.research_ingestion_service.parse_backtest_summary",
        raise_parse,
    )

    with pytest.raises(ValueError, match="bad payload"):
        await ingest_summary_payload(
            session,
            {"run_id": "broken"},
            received_at=datetime.now(UTC),
        )

    update_sync_job_status.assert_awaited_once()
    session.rollback.assert_awaited_once()
