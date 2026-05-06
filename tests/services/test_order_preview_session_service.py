from datetime import UTC

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


@pytest_asyncio.fixture
async def db_session():
    from app.models.order_preview_session import (
        OrderExecutionRequest,
        OrderPreviewLeg,
        OrderPreviewSession,
    )
    from app.models.trading import User

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # User table must come first for foreign keys
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(OrderPreviewSession.__table__.create)
        await conn.run_sync(OrderPreviewLeg.__table__.create)
        await conn.run_sync(OrderExecutionRequest.__table__.create)

    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        # Create a dummy user
        from datetime import datetime

        u = User(
            id=1,
            email="test@example.com",
            username="testuser",
            created_at=datetime.now(UTC),
        )
        session.add(u)
        await session.commit()
        yield session


@pytest.mark.unit
def test_order_preview_session_model_columns_exist():
    from app.models.order_preview_session import (
        OrderExecutionRequest,
        OrderPreviewLeg,
        OrderPreviewSession,
    )

    assert "preview_uuid" in OrderPreviewSession.__table__.columns
    assert "status" in OrderPreviewSession.__table__.columns
    assert "leg_index" in OrderPreviewLeg.__table__.columns
    assert "broker_order_id" in OrderExecutionRequest.__table__.columns


@pytest.mark.unit
def test_create_preview_request_validates_required_fields():
    from pydantic import ValidationError

    from app.schemas.order_preview_session import CreatePreviewRequest, PreviewLegInput

    valid = CreatePreviewRequest(
        source_kind="portfolio_action",
        source_ref="action-uuid-1",
        symbol="KRW-ADA",
        market="crypto",
        venue="crypto_live",
        side="sell",
        legs=[
            PreviewLegInput(leg_index=0, quantity="33.33", price="650.0"),
            PreviewLegInput(leg_index=1, quantity="33.33", price="660.0"),
            PreviewLegInput(leg_index=2, quantity="33.34", price="670.0"),
        ],
    )
    assert len(valid.legs) == 3

    with pytest.raises(ValidationError):
        CreatePreviewRequest(
            source_kind="portfolio_action",
            symbol="KRW-ADA",
            market="crypto",
            venue="crypto_live",
            side="sell",
            legs=[],  # empty rejected
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_preview_persists_three_legs_for_ada_sell(db_session) -> None:
    from unittest.mock import AsyncMock

    from app.schemas.order_preview_session import CreatePreviewRequest, PreviewLegInput
    from app.services.order_preview_session_service import OrderPreviewSessionService

    fake_dry_run = AsyncMock()
    fake_dry_run.run.return_value = {
        "ok": True,
        "legs": [
            {"leg_index": 0, "estimated_value": "21666.5", "estimated_fee": "10.83"},
            {"leg_index": 1, "estimated_value": "22000.0", "estimated_fee": "11.0"},
            {"leg_index": 2, "estimated_value": "22338.5", "estimated_fee": "11.16"},
        ],
    }
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)

    req = CreatePreviewRequest(
        source_kind="portfolio_action",
        source_ref="action-uuid-1",
        symbol="KRW-ADA",
        market="crypto",
        venue="crypto_live",
        side="sell",
        legs=[
            PreviewLegInput(leg_index=0, quantity="33.33", price="650.0"),
            PreviewLegInput(leg_index=1, quantity="33.33", price="660.0"),
            PreviewLegInput(leg_index=2, quantity="33.34", price="670.0"),
        ],
    )

    out = await service.create_preview(user_id=1, request=req)

    assert out.status == "preview_passed"
    assert len(out.legs) == 3
    assert {leg.leg_index for leg in out.legs} == {0, 1, 2}
    assert all(leg.dry_run_status == "passed" for leg in out.legs)
    fake_dry_run.run.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_schema_mismatch_marks_preview_failed(db_session) -> None:
    from unittest.mock import AsyncMock

    from app.schemas.order_preview_session import CreatePreviewRequest, PreviewLegInput
    from app.services.order_preview_session_service import (
        OrderPreviewSessionService,
        PreviewSchemaMismatchError,
    )

    fake_dry_run = AsyncMock()
    fake_dry_run.run.side_effect = PreviewSchemaMismatchError("legs missing field")
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)

    req = CreatePreviewRequest(
        source_kind="portfolio_action",
        symbol="KRW-ADA",
        market="crypto",
        venue="crypto_live",
        side="sell",
        legs=[PreviewLegInput(leg_index=0, quantity="1", price="650")],
    )
    out = await service.create_preview(user_id=1, request=req)
    assert out.status == "preview_failed"
    assert out.dry_run_error and out.dry_run_error["kind"] == "schema_mismatch"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refresh_recomputes_dry_run(db_session) -> None:
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from app.schemas.order_preview_session import CreatePreviewRequest, PreviewLegInput
    from app.services.order_preview_session_service import OrderPreviewSessionService

    fake_dry_run = AsyncMock()
    fake_dry_run.run.return_value = {
        "ok": True,
        "legs": [{"leg_index": 0, "estimated_value": "100", "estimated_fee": "0.1"}],
    }
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)
    out = await service.create_preview(
        user_id=1,
        request=CreatePreviewRequest(
            source_kind="portfolio_action",
            symbol="KRW-ADA",
            market="crypto",
            venue="crypto_live",
            side="sell",
            legs=[PreviewLegInput(leg_index=0, quantity="1", price="650")],
        ),
    )

    fake_dry_run.run.return_value = {
        "ok": True,
        "legs": [{"leg_index": 0, "estimated_value": "200", "estimated_fee": "0.2"}],
    }
    refreshed = await service.refresh_preview(user_id=1, preview_uuid=out.preview_uuid)
    assert refreshed.legs[0].estimated_value == pytest.approx(Decimal("200"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_blocked_when_status_not_preview_passed(db_session) -> None:
    from unittest.mock import AsyncMock

    from app.schemas.order_preview_session import (
        CreatePreviewRequest,
        PreviewLegInput,
        SubmitPreviewRequest,
    )
    from app.services.order_preview_session_service import (
        OrderPreviewSessionService,
        PreviewNotApprovedError,
    )

    fake_dry_run = AsyncMock()
    fake_dry_run.run.return_value = {"ok": False, "error": "out_of_session"}
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)
    out = await service.create_preview(
        user_id=1,
        request=CreatePreviewRequest(
            source_kind="portfolio_action",
            symbol="KRW-ADA",
            market="crypto",
            venue="crypto_live",
            side="sell",
            legs=[PreviewLegInput(leg_index=0, quantity="1", price="650")],
        ),
    )
    assert out.status == "preview_failed"

    broker = AsyncMock()
    with pytest.raises(PreviewNotApprovedError):
        await service.submit_preview(
            user_id=1,
            preview_uuid=out.preview_uuid,
            request=SubmitPreviewRequest(approval_token="anything-long-enough"),
            broker_submit=broker,
        )
    broker.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_records_broker_order_ids(db_session) -> None:
    from unittest.mock import AsyncMock

    from app.schemas.order_preview_session import (
        CreatePreviewRequest,
        PreviewLegInput,
        SubmitPreviewRequest,
    )
    from app.services.order_preview_session_service import OrderPreviewSessionService

    fake_dry_run = AsyncMock()
    fake_dry_run.run.return_value = {
        "ok": True,
        "legs": [
            {"leg_index": 0, "estimated_value": "100"},
            {"leg_index": 1, "estimated_value": "100"},
            {"leg_index": 2, "estimated_value": "100"},
        ],
    }
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)
    out = await service.create_preview(
        user_id=1,
        request=CreatePreviewRequest(
            source_kind="portfolio_action",
            symbol="KRW-ADA",
            market="crypto",
            venue="crypto_live",
            side="sell",
            legs=[
                PreviewLegInput(leg_index=0, quantity="1", price="650"),
                PreviewLegInput(leg_index=1, quantity="1", price="660"),
                PreviewLegInput(leg_index=2, quantity="1", price="670"),
            ],
        ),
    )
    assert out.status == "preview_passed"

    counter = {"n": 0}

    async def fake_broker(*, leg, session):
        counter["n"] += 1
        return {"order_id": f"BK-{leg.leg_index}"}

    # approval_token loaded from DB (test fetches via service.get internals)
    from sqlalchemy import select

    from app.models.order_preview_session import OrderPreviewSession

    row = (
        await db_session.execute(
            select(OrderPreviewSession).where(
                OrderPreviewSession.preview_uuid == out.preview_uuid
            )
        )
    ).scalar_one()
    token = row.approval_token

    submitted = await service.submit_preview(
        user_id=1,
        preview_uuid=out.preview_uuid,
        request=SubmitPreviewRequest(approval_token=token),
        broker_submit=fake_broker,
    )
    assert submitted.status == "submitted"
    assert counter["n"] == 3
    assert {e.broker_order_id for e in submitted.executions} == {
        "BK-0",
        "BK-1",
        "BK-2",
    }
