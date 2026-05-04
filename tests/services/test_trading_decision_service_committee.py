import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.trading_decision import WorkflowStatus
from app.services.trading_decision_service import create_decision_session
from sqlalchemy import text

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
                    "username": f"td_svc_test_{suffix}",
                    "email": f"td_svc_{suffix}@example.com",
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
async def test_create_decision_session_with_committee_fields() -> None:
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            ds = await create_decision_session(
                session,
                user_id=user_id,
                source_profile="committee_mock_paper",
                generated_at=datetime.now(UTC),
                workflow_status=WorkflowStatus.created,
                account_mode="kis_mock",
                automation={"enabled": True}
            )
            await session.commit()
            
            assert ds.workflow_status == WorkflowStatus.created
            assert ds.account_mode == "kis_mock"
            assert ds.automation == {"enabled": True}
    finally:
        await _cleanup_user(user_id)
