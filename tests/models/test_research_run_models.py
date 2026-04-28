"""Integration tests for research_run ORM models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunPendingReconciliation,
)
from app.models.trading import InstrumentType

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_research_run_tables() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(text("SELECT to_regclass('research_runs')"))
            if row.scalar_one_or_none() is None:
                pytest.skip("research_run tables are not migrated")
    except Exception:
        pytest.skip("database is not available for integration persistence checks")


async def _create_user() -> int:
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        user_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (username, email, role, tz, base_currency, is_active)
                    VALUES (:username, :email, 'viewer', 'Asia/Seoul', 'KRW', true)
                    RETURNING id
                    """
                ),
                {
                    "username": f"rob24_test_{suffix}",
                    "email": f"rob24_{suffix}@example.com",
                },
            )
        ).scalar_one()
        await session.commit()
        return user_id


async def _cleanup_user(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id}
        )
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_research_run_round_trip_with_candidate_and_reconciliation() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="kr",
                stage="nxt_aftermarket",
                source_profile="hermes",
                strategy_name="nxt_test",
                source_freshness={
                    "quote_as_of": "2026-04-28T05:00:00+00:00",
                    "kr_universe_synced_at": "2026-04-28T04:30:00+00:00",
                },
                source_warnings=["missing_orderbook"],
                advisory_links=[],
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()

            candidate = ResearchRunCandidate(
                research_run_id=run.id,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="buy",
                candidate_kind="screener_hit",
                proposed_price=Decimal("70000"),
                proposed_qty=Decimal("10"),
                confidence=72,
                rationale="dummy",
                currency="KRW",
                source_freshness=None,
                warnings=[],
                payload={"source": "test"},
            )
            session.add(candidate)
            await session.flush()

            recon = ResearchRunPendingReconciliation(
                research_run_id=run.id,
                candidate_id=candidate.id,
                order_id="ORDER-1",
                symbol="005930",
                market="kr",
                side="buy",
                classification="maintain",
                nxt_classification="buy_pending_actionable",
                nxt_actionable=True,
                gap_pct=Decimal("0.10"),
                reasons=["gap_within_near_fill_pct"],
                warnings=[],
                decision_support={"current_price": "70070.0", "gap_pct": "0.1"},
                summary="NXT 매수 대기 — 적정 (지속 모니터링)",
            )
            session.add(recon)
            await session.commit()

            assert run.run_uuid is not None
            assert candidate.candidate_uuid is not None
            assert recon.id is not None
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_research_run_stage_check_rejects_unknown_value() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="kr",
                stage="not_a_stage",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_research_run_market_scope_check_rejects_unknown_value() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="forex",
                stage="preopen",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recon_classification_check_rejects_unknown_value() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="kr",
                stage="intraday",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            recon = ResearchRunPendingReconciliation(
                research_run_id=run.id,
                order_id="O1",
                symbol="005930",
                market="kr",
                side="buy",
                classification="bogus",
                decision_support={},
            )
            session.add(recon)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cascade_delete_run_removes_children() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        run_id: int
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="kr",
                stage="preopen",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            run_id = run.id
            session.add(
                ResearchRunCandidate(
                    research_run_id=run.id,
                    symbol="005930",
                    instrument_type=InstrumentType.equity_kr,
                    candidate_kind="screener_hit",
                )
            )
            session.add(
                ResearchRunPendingReconciliation(
                    research_run_id=run.id,
                    order_id="O1",
                    symbol="005930",
                    market="kr",
                    side="buy",
                    classification="maintain",
                    decision_support={},
                )
            )
            await session.commit()

        async with SessionLocal() as session:
            await session.execute(
                text("DELETE FROM research_runs WHERE id = :id"), {"id": run_id}
            )
            await session.commit()

            cand_count = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM research_run_candidates WHERE research_run_id = :id"
                    ),
                    {"id": run_id},
                )
            ).scalar_one()
            recon_count = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM research_run_pending_reconciliations WHERE research_run_id = :id"
                    ),
                    {"id": run_id},
                )
            ).scalar_one()
            assert cand_count == 0
            assert recon_count == 0
    finally:
        await _cleanup_user(user_id)
