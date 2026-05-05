from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_tables() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'trading_decision_sessions'
                      AND column_name IN (
                        'workflow_status', 'account_mode', 'artifacts'
                      )
                    HAVING count(*) = 3
                    """
                )
            )
            if row.scalar_one_or_none() is None:
                pytest.skip("trading_decision committee columns are not migrated")
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
                    "username": f"committee_router_test_{suffix}",
                    "email": f"committee_router_{suffix}@example.com",
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
    return TestClient(app)


@pytest.mark.integration
def test_create_and_get_committee_session_router(monkeypatch):
    asyncio.run(_ensure_tables())
    user_id = asyncio.run(_create_user())
    try:
        client = _make_client(user_id, monkeypatch)

        # 1. Create a session with committee fields
        generated_at = datetime.now(UTC).isoformat()
        resp = client.post(
            "/trading/api/decisions",
            json={
                "source_profile": "committee_mock_paper",
                "strategy_name": "Committee Test",
                "market_scope": "crypto",
                "generated_at": generated_at,
                "workflow_status": "created",
                "account_mode": "kis_mock",
                "automation": {
                    "enabled": True,
                    "auto_approve_risk": False,
                    "auto_execute": False,
                },
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["workflow_status"] == "created"
        assert body["account_mode"] == "kis_mock"
        assert body["automation"]["enabled"] is True

        session_uuid = body["session_uuid"]

        # 2. Get the session and verify fields
        resp2 = client.get(f"/trading/api/decisions/{session_uuid}")
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["workflow_status"] == "created"
        assert body2["account_mode"] == "kis_mock"
        assert body2["automation"]["enabled"] is True

        # 3. PATCH workflow status
        resp3 = client.patch(
            f"/trading/api/decisions/{session_uuid}/workflow",
            params={"status_update": "evidence_ready"},
        )
        assert resp3.status_code == 200
        assert resp3.json()["workflow_status"] == "evidence_ready"

        # 4. PATCH artifacts
        resp4 = client.patch(
            f"/trading/api/decisions/{session_uuid}/artifacts",
            json={"evidence": {"technical_analysis": {"summary": "Bullish"}}},
        )
        assert resp4.status_code == 200
        body4 = resp4.json()
        assert (
            body4["artifacts"]["evidence"]["technical_analysis"]["summary"] == "Bullish"
        )

    finally:
        asyncio.run(_cleanup_user(user_id))
