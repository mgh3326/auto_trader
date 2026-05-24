import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.db import get_db
from app.models.investment_dimension_reports import InvestmentDimensionReport
from app.models.investment_stages import InvestmentStageRun
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.routers.investment_dimension_reports import router as dim_reports_router

URL_TEMPLATE = "/trading/api/investment-reports/runs/{run_uuid}/dimension-reports"


def _build_app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(dim_reports_router)

    async def _db_override() -> AsyncIterator[object]:
        yield db_session

    async def _user_override() -> User:
        return User(id=1, email="test@example.com", role="user")  # type: ignore

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_authenticated_user] = _user_override
    return app


@pytest.mark.asyncio
async def test_get_dimension_reports(db_session) -> None:
    # Seed a stage run
    run_uuid = uuid.uuid4()
    run = InvestmentStageRun(
        run_uuid=run_uuid,
        snapshot_bundle_uuid=uuid.uuid4(),
        market="us",
        status="running",
        started_at=dt.datetime.now(tz=dt.UTC),
    )
    db_session.add(run)
    await db_session.commit()

    # Seed a dimension report
    report = InvestmentDimensionReport(
        run_uuid=run_uuid,
        snapshot_bundle_uuid=uuid.uuid4(),
        dimension="market",
        market="us",
        stance="bullish",
        confidence=80,
        report_text="시장 분석 보고서",
        content_hash="some-hash",
        idempotency_key=str(uuid.uuid4()),
    )
    db_session.add(report)
    await db_session.commit()

    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.get(URL_TEMPLATE.format(run_uuid=run_uuid))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runUuid"] == str(run_uuid)
    assert len(body["reports"]) == 1
    assert body["reports"][0]["dimension"] == "market"
    assert body["reports"][0]["stance"] == "bullish"
    assert body["reports"][0]["stanceLabel"] == "강세"
    assert body["reports"][0]["confidenceLabel"] == "80%"
    assert body["reports"][0]["reportText"] == "시장 분석 보고서"
