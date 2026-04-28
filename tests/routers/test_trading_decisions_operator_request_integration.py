from __future__ import annotations

import asyncio
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_tables() -> None:
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
                    INSERT INTO users
                        (username, email, role, tz, base_currency, is_active)
                    VALUES
                        (:username, :email, 'viewer', 'Asia/Seoul', 'KRW', true)
                    RETURNING id
                    """
                ),
                {
                    "username": f"rob15_router_{suffix}",
                    "email": f"rob15_router_{suffix}@example.com",
                },
            )
        ).scalar_one()
        await session.commit()
        return int(user_id)


async def _cleanup_user(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await session.commit()


def _make_client(user_id: int, monkeypatch):
    from app.core.db import get_db
    from app.routers import trading_decisions
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(trading_decisions.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(
        id=user_id
    )

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(
        trading_decisions.settings, "public_base_url", "https://trader.robinco.dev"
    )
    return TestClient(app)


@pytest.mark.integration
def test_no_advisory_persists_session_and_proposals_in_db(monkeypatch):
    from app.models.trading_decision import (
        TradingDecisionProposal,
        TradingDecisionSession,
    )

    asyncio.run(_ensure_tables())
    user_id = asyncio.run(_create_user())
    try:
        client = _make_client(user_id, monkeypatch)
        resp = client.post(
            "/trading/api/decisions/from-operator-request",
            json={
                "market_scope": "kr",
                "candidates": [
                    {
                        "symbol": "005930",
                        "instrument_type": "equity_kr",
                        "side": "buy",
                        "confidence": 70,
                        "proposal_kind": "enter",
                        "rationale": "deterministic op",
                    }
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()

        async def _load():
            async with SessionLocal() as session:
                persisted = (
                    await session.execute(
                        select(TradingDecisionSession).where(
                            TradingDecisionSession.user_id == user_id
                        )
                    )
                ).scalar_one()
                proposals = (
                    (
                        await session.execute(
                            select(TradingDecisionProposal).where(
                                TradingDecisionProposal.session_id == persisted.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return persisted, proposals

        persisted, proposals = asyncio.run(_load())
        assert persisted.market_scope == "kr"
        assert persisted.market_brief["advisory_only"] is True
        assert persisted.market_brief["execution_allowed"] is False
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.symbol == "005930"
        assert proposal.original_payload["advisory_only"] is True
        assert proposal.original_payload["execution_allowed"] is False
        assert "synthesis" not in proposal.original_payload
        assert proposal.original_payload["operator_request"]["applied_policies"] == [
            "no_advisory"
        ]
        assert body["session_url"] == (
            f"https://trader.robinco.dev/trading/decisions/{persisted.session_uuid}"
        )
    finally:
        asyncio.run(_cleanup_user(user_id))


@pytest.mark.integration
def test_advisory_path_persists_synthesis_block(monkeypatch):
    from app.models.trading_decision import (
        TradingDecisionProposal,
        TradingDecisionSession,
    )
    from app.schemas.tradingagents_research import (
        TradingAgentsConfigSnapshot,
        TradingAgentsLLM,
        TradingAgentsRunnerResult,
        TradingAgentsWarnings,
    )
    from app.services import operator_decision_session_service

    asyncio.run(_ensure_tables())
    user_id = asyncio.run(_create_user())
    try:
        fake_runner_result = TradingAgentsRunnerResult(
            status="ok",
            symbol="NVDA",
            as_of_date=date(2026, 4, 28),
            decision="Underweight",
            advisory_only=True,
            execution_allowed=False,
            analysts=["market"],
            llm=TradingAgentsLLM(
                provider="openai-compatible",
                model="gpt-5.5",
                base_url="http://127.0.0.1:8796/v1",
            ),
            config=TradingAgentsConfigSnapshot(
                max_debate_rounds=1,
                max_risk_discuss_rounds=1,
                max_recur_limit=30,
                output_language="English",
                checkpoint_enabled=False,
            ),
            warnings=TradingAgentsWarnings(),
            final_trade_decision="no execution",
            raw_state_keys=["k1"],
        )
        monkeypatch.setattr(
            operator_decision_session_service,
            "run_tradingagents_research",
            AsyncMock(return_value=fake_runner_result),
        )

        client = _make_client(user_id, monkeypatch)
        resp = client.post(
            "/trading/api/decisions/from-operator-request",
            json={
                "market_scope": "us",
                "candidates": [
                    {
                        "symbol": "NVDA",
                        "instrument_type": "equity_us",
                        "side": "buy",
                        "confidence": 70,
                        "proposal_kind": "enter",
                    }
                ],
                "include_tradingagents": True,
                "analysts": ["market"],
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["advisory_used"] is True

        async def _load():
            async with SessionLocal() as session:
                persisted = (
                    await session.execute(
                        select(TradingDecisionSession).where(
                            TradingDecisionSession.user_id == user_id
                        )
                    )
                ).scalar_one()
                proposals = (
                    (
                        await session.execute(
                            select(TradingDecisionProposal).where(
                                TradingDecisionProposal.session_id == persisted.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return persisted, proposals

        persisted, proposals = asyncio.run(_load())
        assert persisted.source_profile == "operator_request+tradingagents"
        assert persisted.market_brief["advisory_only"] is True
        assert persisted.market_brief["execution_allowed"] is False
        assert persisted.market_brief["synthesis_meta"]["proposal_count"] == 1
        assert len(proposals) == 1
        payload = proposals[0].original_payload
        assert payload["advisory_only"] is True
        assert payload["execution_allowed"] is False
        assert payload["synthesis"]["final_side"] == "none"
        assert payload["synthesis"]["final_proposal_kind"] == "pullback_watch"
    finally:
        asyncio.run(_cleanup_user(user_id))
