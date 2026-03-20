from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import (
    ResearchBacktestPair,
    ResearchBacktestRun,
    ResearchPromotionCandidate,
    ResearchSyncJob,
)
from app.schemas.research_backtest import BacktestRunSummary
from app.services.research_backtest_parser import parse_backtest_summary
from app.services.research_gate_service import GateResult, evaluate_candidate


def _to_decimal(value: float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _as_float(value: float | Decimal | None, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


async def create_sync_job(
    session: AsyncSession,
    *,
    idempotency_key: str | None,
    source_file: str | None,
) -> ResearchSyncJob:
    if idempotency_key:
        existing = await session.execute(
            select(ResearchSyncJob).where(
                ResearchSyncJob.idempotency_key == idempotency_key
            )
        )
        existing_row = existing.scalar_one_or_none()
        if existing_row is not None:
            existing_row.status = "running"
            existing_row.source_file = source_file or existing_row.source_file
            await session.flush()
            return existing_row

    row = ResearchSyncJob(
        idempotency_key=idempotency_key,
        source_file=source_file,
        status="running",
    )
    session.add(row)
    await session.flush()
    return row


async def update_sync_job_status(
    session: AsyncSession,
    sync_job: ResearchSyncJob,
    *,
    status: str,
    backtest_run_id: int | None = None,
    error_payload: dict[str, Any] | None = None,
) -> None:
    sync_job.status = status
    sync_job.backtest_run_id = backtest_run_id
    sync_job.error_payload = error_payload
    await session.flush()


async def upsert_backtest_run(
    session: AsyncSession,
    summary: BacktestRunSummary,
) -> ResearchBacktestRun:
    result = await session.execute(
        select(ResearchBacktestRun).where(ResearchBacktestRun.run_id == summary.run_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = ResearchBacktestRun(run_id=summary.run_id)
        session.add(row)

    row.strategy_name = summary.strategy_name
    row.strategy_version = summary.strategy_version
    row.exchange = summary.exchange
    row.market = summary.market
    row.timeframe = summary.timeframe
    row.timerange = summary.timerange
    row.runner = summary.runner
    row.started_at = summary.started_at
    row.ended_at = summary.ended_at
    row.total_trades = summary.total_trades
    row.profit_factor = _to_decimal(summary.profit_factor) or Decimal("0")
    row.max_drawdown = _to_decimal(summary.max_drawdown) or Decimal("0")
    row.win_rate = _to_decimal(summary.win_rate)
    row.expectancy = _to_decimal(summary.expectancy)
    row.total_return = _to_decimal(summary.total_return)
    row.artifact_path = summary.artifact_path
    row.artifact_hash = summary.artifact_hash
    row.raw_payload = summary.raw_payload
    await session.flush()
    return row


async def replace_backtest_pairs(
    session: AsyncSession,
    backtest_run: ResearchBacktestRun,
    summary: BacktestRunSummary,
) -> None:
    existing_pairs = await session.execute(
        select(ResearchBacktestPair).where(
            ResearchBacktestPair.backtest_run_id == backtest_run.id
        )
    )
    for pair_row in existing_pairs.scalars().all():
        await session.delete(pair_row)

    for pair in summary.pairs:
        session.add(
            ResearchBacktestPair(
                backtest_run_id=backtest_run.id,
                pair=pair.pair,
                total_trades=pair.total_trades,
                profit_factor=_to_decimal(pair.profit_factor),
                max_drawdown=_to_decimal(pair.max_drawdown),
                total_return=_to_decimal(pair.total_return),
            )
        )
    await session.flush()


async def upsert_promotion_candidate(
    session: AsyncSession,
    *,
    backtest_run: ResearchBacktestRun,
    gate_result: GateResult,
) -> ResearchPromotionCandidate:
    existing = await session.execute(
        select(ResearchPromotionCandidate).where(
            ResearchPromotionCandidate.backtest_run_id == backtest_run.id
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = ResearchPromotionCandidate(backtest_run_id=backtest_run.id)
        session.add(row)

    row.status = gate_result.status
    row.reason_code = gate_result.reason_code
    row.thresholds = gate_result.thresholds
    row.metrics = gate_result.metrics
    await session.flush()
    return row


async def ingest_summary_payload(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    gate_config: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    source_file: str | None = None,
    strategy_name: str | None = None,
    runner: str | None = None,
    received_at: datetime | None = None,
) -> str:
    _ = received_at
    sync_job = await create_sync_job(
        session,
        idempotency_key=idempotency_key,
        source_file=source_file,
    )

    try:
        parsed = parse_backtest_summary(
            payload,
            strategy_name=strategy_name,
            runner=runner,
        )
        run_row = await upsert_backtest_run(session, parsed)
        await replace_backtest_pairs(session, run_row, parsed)

        effective_gate_config = gate_config or {}
        gate_result = evaluate_candidate(
            total_trades=parsed.total_trades,
            profit_factor=_as_float(parsed.profit_factor),
            max_drawdown=_as_float(parsed.max_drawdown),
            expectancy=_as_float(parsed.expectancy)
            if parsed.expectancy is not None
            else None,
            total_return=_as_float(parsed.total_return)
            if parsed.total_return is not None
            else None,
            config=effective_gate_config,
        )
        await upsert_promotion_candidate(
            session,
            backtest_run=run_row,
            gate_result=gate_result,
        )
        await update_sync_job_status(
            session,
            sync_job,
            status="completed",
            backtest_run_id=run_row.id,
        )
        await session.commit()
        return parsed.run_id
    except Exception as exc:
        await session.rollback()
        await update_sync_job_status(
            session,
            sync_job,
            status="failed",
            error_payload={"error": str(exc)},
        )
        await session.commit()
        raise
