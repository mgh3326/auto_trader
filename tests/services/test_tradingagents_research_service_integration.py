from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ProposalKind,
    TradingDecisionAction,
    TradingDecisionCounterfactual,
    TradingDecisionOutcome,
    TradingDecisionProposal,
    TradingDecisionSession,
    UserResponse,
)
from app.services import tradingagents_research_service as svc

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tradingagents"
SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
def reset_trading_decision_service_module():
    """Undo direct service monkeypatches from legacy router tests in same process."""
    from app.services import trading_decision_service

    svc.trading_decision_service = importlib.reload(trading_decision_service)


async def _ensure_trading_decision_tables() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(
                text("SELECT to_regclass('trading_decision_sessions')")
            )
            if row.scalar_one_or_none() is None:
                pytest.skip("trading_decision tables are not migrated")
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
                    "username": f"ta_research_test_{suffix}",
                    "email": f"ta_research_{suffix}@example.com",
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


def _payload() -> dict:
    return json.loads((FIXTURE_DIR / "runner_ok_nvda.json").read_text("utf-8"))


@pytest.fixture
def stub_runner(monkeypatch: pytest.MonkeyPatch):
    async def _run_tradingagents_research(**_kwargs):
        from app.schemas.tradingagents_research import TradingAgentsRunnerResult

        return TradingAgentsRunnerResult.model_validate(_payload())

    monkeypatch.setattr(svc, "run_tradingagents_research", _run_tradingagents_research)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_creates_session_and_single_proposal(stub_runner) -> None:
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds, proposal = await svc.ingest_tradingagents_research(
                session,
                user_id=user_id,
                symbol="NVDA",
                instrument_type=InstrumentType.equity_us,
            )
            await session.commit()

            assert ds.source_profile == "tradingagents"
            assert ds.strategy_name == pytest.approx(
                "tradingagents:gpt-5.5:market,news"
            )
            assert ds.market_scope == "us"
            assert proposal.proposal_kind == ProposalKind.other
            assert proposal.side == "none"
            assert proposal.session_id == ds.id

            count = await session.scalar(
                select(func.count(TradingDecisionProposal.id)).where(
                    TradingDecisionProposal.session_id == ds.id
                )
            )
            assert count == 1
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_persists_advisory_invariants_in_market_brief_and_payload(
    stub_runner,
) -> None:
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds, proposal = await svc.ingest_tradingagents_research(
                session,
                user_id=user_id,
                symbol="NVDA",
                instrument_type=InstrumentType.equity_us,
            )
            await session.commit()

            assert ds.market_brief["advisory_only"] is True
            assert ds.market_brief["execution_allowed"] is False
            assert proposal.original_payload["advisory_only"] is True
            assert proposal.original_payload["execution_allowed"] is False
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_preserves_warnings_structured_output(stub_runner) -> None:
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds, proposal = await svc.ingest_tradingagents_research(
                session,
                user_id=user_id,
                symbol="NVDA",
                instrument_type=InstrumentType.equity_us,
            )
            await session.commit()

            expected = ["earnings sensitivity noted", "macro liquidity risk noted"]
            assert ds.market_brief["warnings"]["structured_output"] == expected
            assert (
                proposal.original_payload["warnings"]["structured_output"] == expected
            )
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_does_not_create_action_or_counterfactual_or_outcome(
    stub_runner,
) -> None:
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            _, proposal = await svc.ingest_tradingagents_research(
                session,
                user_id=user_id,
                symbol="NVDA",
                instrument_type=InstrumentType.equity_us,
            )
            await session.commit()

            action_count = await session.scalar(
                select(func.count(TradingDecisionAction.id)).where(
                    TradingDecisionAction.proposal_id == proposal.id
                )
            )
            counterfactual_count = await session.scalar(
                select(func.count(TradingDecisionCounterfactual.id)).where(
                    TradingDecisionCounterfactual.proposal_id == proposal.id
                )
            )
            outcome_count = await session.scalar(
                select(func.count(TradingDecisionOutcome.id)).where(
                    TradingDecisionOutcome.proposal_id == proposal.id
                )
            )
            assert action_count == 0
            assert counterfactual_count == 0
            assert outcome_count == 0
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_runner_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ensure_trading_decision_tables()
    user_id = await _create_user()

    async def _raise(**_kwargs):
        raise svc.TradingAgentsRunnerError("runner failed")

    monkeypatch.setattr(svc, "run_tradingagents_research", _raise)
    try:
        async with SessionLocal() as session:
            with pytest.raises(svc.TradingAgentsRunnerError):
                await svc.ingest_tradingagents_research(
                    session,
                    user_id=user_id,
                    symbol="NVDA",
                    instrument_type=InstrumentType.equity_us,
                )
            await session.rollback()

            count = await session.scalar(
                select(func.count(TradingDecisionSession.id)).where(
                    TradingDecisionSession.user_id == user_id,
                    TradingDecisionSession.source_profile == "tradingagents",
                )
            )
            assert count == 0
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_does_not_touch_user_response_fields(stub_runner) -> None:
    await _ensure_trading_decision_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            _, proposal = await svc.ingest_tradingagents_research(
                session,
                user_id=user_id,
                symbol="NVDA",
                instrument_type=InstrumentType.equity_us,
            )
            await session.commit()

            assert proposal.user_response == UserResponse.pending
            assert proposal.responded_at is None
            assert proposal.user_quantity is None
            assert proposal.user_quantity_pct is None
            assert proposal.user_amount is None
            assert proposal.user_price is None
            assert proposal.user_trigger_price is None
            assert proposal.user_threshold_pct is None
            assert proposal.user_note is None
    finally:
        await _cleanup_user(user_id)
