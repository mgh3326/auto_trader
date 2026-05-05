import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.trading_decision import TradingDecisionSession, WorkflowStatus
from app.services.trading_decisions.committee_service import CommitteeSessionService

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


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
                    "username": f"committee_svc_test_{suffix}",
                    "email": f"committee_svc_{suffix}@example.com",
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
async def test_committee_session_updates() -> None:
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = TradingDecisionSession(
                user_id=user_id,
                source_profile="committee_mock_paper",
                generated_at=datetime.now(UTC),
                workflow_status=WorkflowStatus.created,
            )
            session.add(ds)
            await session.commit()
            session_uuid = ds.session_uuid

        async with SessionLocal() as session:
            # 1. Update workflow status
            updated = await CommitteeSessionService.update_workflow_status(
                session,
                session_uuid=session_uuid,
                user_id=user_id,
                status=WorkflowStatus.evidence_ready,
            )
            assert updated is not None
            assert updated.workflow_status == WorkflowStatus.evidence_ready
            await session.commit()

        async with SessionLocal() as session:
            # 2. Patch artifacts
            patch = {"evidence": {"technical_analysis": {"summary": "Bullish"}}}
            updated2 = await CommitteeSessionService.update_committee_artifacts(
                session,
                session_uuid=session_uuid,
                user_id=user_id,
                artifacts_patch=patch,
            )
            assert updated2 is not None
            assert updated2.artifacts == patch
            await session.commit()

        async with SessionLocal() as session:
            # 3. Partial merge of artifacts
            patch2 = {"risk_review": {"verdict": "approved"}}
            updated3 = await CommitteeSessionService.update_committee_artifacts(
                session,
                session_uuid=session_uuid,
                user_id=user_id,
                artifacts_patch=patch2,
            )
            assert updated3 is not None
            assert (
                updated3.artifacts["evidence"]["technical_analysis"]["summary"]
                == "Bullish"
            )
            assert updated3.artifacts["risk_review"]["verdict"] == "approved"
    finally:
        await _cleanup_user(user_id)
