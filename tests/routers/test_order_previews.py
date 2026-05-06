"""ROB-118 — Order previews router tests."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import api as app
from app.middleware.auth import AuthMiddleware
from app.routers.dependencies import get_authenticated_user
from app.routers.order_previews import (
    get_broker_submit_callable,
    get_order_preview_session_service,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_preview_returns_passed_status() -> None:
    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()
    from app.schemas.order_preview_session import PreviewLegOut, PreviewSessionOut

    fake_service.create_preview.return_value = PreviewSessionOut(
        preview_uuid="uuid-1",
        source_kind="portfolio_action",
        source_ref=None,
        research_session_id=None,
        symbol="KRW-ADA",
        market="crypto",
        venue="crypto_live",
        side="sell",
        status="preview_passed",
        legs=[
            PreviewLegOut(
                leg_index=0,
                quantity=Decimal("33.33"),
                price=Decimal("650"),
                order_type="limit",
                estimated_value=Decimal("21666.5"),
                estimated_fee=Decimal("10.83"),
                expected_pnl=None,
                dry_run_status="passed",
                dry_run_error=None,
            )
        ],
        executions=[],
        approved_at=None,
        submitted_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_order_preview_session_service] = lambda: fake_service
    try:
        with patch.object(AuthMiddleware, "_maybe_authenticate", return_value=None):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                res = await ac.post(
                    "/trading/api/order-previews",
                    json={
                        "source_kind": "portfolio_action",
                        "symbol": "KRW-ADA",
                        "market": "crypto",
                        "venue": "crypto_live",
                        "side": "sell",
                        "legs": [
                            {"leg_index": 0, "quantity": "33.33", "price": "650"},
                        ],
                    },
                )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "preview_passed"
            assert len(body["legs"]) == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_blocked_returns_409() -> None:
    from app.services.order_preview_session_service import PreviewNotApprovedError

    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()
    fake_service.submit_preview.side_effect = PreviewNotApprovedError("not passed")
    fake_broker = AsyncMock()

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_order_preview_session_service] = lambda: fake_service
    app.dependency_overrides[get_broker_submit_callable] = lambda: fake_broker
    try:
        with patch.object(AuthMiddleware, "_maybe_authenticate", return_value=None):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                res = await ac.post(
                    "/trading/api/order-previews/uuid-1/submit",
                    json={"approval_token": "x" * 24},
                )
            assert res.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_default_broker_path_is_fail_closed() -> None:
    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_order_preview_session_service] = lambda: fake_service
    try:
        with patch.object(AuthMiddleware, "_maybe_authenticate", return_value=None):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                res = await ac.post(
                    "/trading/api/order-previews/uuid-1/submit",
                    json={"approval_token": "x" * 24},
                )
        assert res.status_code == 409
        assert "broker submission disabled" in res.json()["detail"]
        fake_service.submit_preview.assert_not_called()
    finally:
        app.dependency_overrides.clear()
