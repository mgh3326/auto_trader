from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.db import get_db
from app.middleware.csrf import TemplateFormCSRFMiddleware
from app.models.trading import UserRole
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_loss_cut_approvals import (
    _require_invest_approvals_enabled,
    _require_web_loss_cut_confirmation_enabled,
    router,
)


def _app(*, user: object) -> FastAPI:
    app = FastAPI()

    @app.get("/csrf-seed")
    async def csrf_seed() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    app.dependency_overrides[get_authenticated_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[_require_invest_approvals_enabled] = lambda: None
    app.dependency_overrides[_require_web_loss_cut_confirmation_enabled] = lambda: None
    app.add_middleware(TemplateFormCSRFMiddleware, secret="router-test-secret")
    return app


@pytest.mark.asyncio
async def test_web_approval_rejects_viewer() -> None:
    app = _app(user=SimpleNamespace(id=7, role=UserRole.viewer))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/invest/api/approvals")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_web_approval_requires_csrf_before_core(monkeypatch) -> None:
    from app.routers import invest_loss_cut_approvals as module

    core_calls: list[dict[str, object]] = []

    async def core(*args, **kwargs):
        core_calls.append({"args": args, "kwargs": kwargs})
        return {"handled": True, "reason": "approved"}

    monkeypatch.setattr(module, "handle_web_approval", core)
    app = _app(user=SimpleNamespace(id=8, role=UserRole.trader))
    proposal_id = uuid.uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/invest/api/approvals/{proposal_id}/approve",
            headers={"Idempotency-Key": "click-1"},
        )

    assert response.status_code == 403
    assert core_calls == []


@pytest.mark.asyncio
async def test_web_approval_uses_authenticated_principal_and_processing_mapping(
    monkeypatch,
) -> None:
    from app.routers import invest_loss_cut_approvals as module

    seen: list[dict[str, object]] = []

    async def core(*args, **kwargs):
        seen.append({"args": args, "kwargs": kwargs})
        return {"handled": False, "reason": "lease_held"}

    monkeypatch.setattr(module, "handle_web_approval", core)
    app = _app(user=SimpleNamespace(id=9, role=UserRole.trader))
    proposal_id = uuid.uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/csrf-seed")
        response = await client.post(
            f"/invest/api/approvals/{proposal_id}/approve",
            headers={
                "X-CSRFToken": client.cookies["csrftoken"],
                "Idempotency-Key": "click-2",
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": {"error": "processing"}}
    assert seen[0]["args"] == (proposal_id,)
    assert seen[0]["kwargs"]["actor_subject"] == "user:9"


@pytest.mark.asyncio
async def test_web_approval_maps_the_core_nonce_replay_vocabulary(monkeypatch) -> None:
    """`nonce_replay` is the concrete service reason, not an invented alias."""
    from app.routers import invest_loss_cut_approvals as module

    async def core(*args, **kwargs):
        return {"handled": False, "reason": "nonce_replay"}

    monkeypatch.setattr(module, "handle_web_approval", core)
    app = _app(user=SimpleNamespace(id=9, role=UserRole.trader))
    proposal_id = uuid.uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/csrf-seed")
        response = await client.post(
            f"/invest/api/approvals/{proposal_id}/approve",
            headers={
                "X-CSRFToken": client.cookies["csrftoken"],
                "Idempotency-Key": "click-nonce-replay",
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": {"error": "processing"}}


@pytest.mark.asyncio
async def test_web_approval_endpoints_default_disabled(monkeypatch) -> None:
    from app.core.config import settings
    from app.routers import invest_loss_cut_approvals as module

    monkeypatch.setattr(settings, "INVEST_APPROVALS_ENABLED", False)

    async def must_not_reach_cards(*args, **kwargs):
        pytest.fail("disabled approval endpoint reached its card reader")

    monkeypatch.setattr(module.WebApprovalService, "list_cards", must_not_reach_cards)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(
        id=9, role=UserRole.trader
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/invest/api/approvals")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_web_loss_cut_confirm_respects_legacy_kill_switch(monkeypatch) -> None:
    from app.core.config import settings
    from app.routers import invest_loss_cut_approvals as module

    monkeypatch.setattr(settings, "INVEST_APPROVALS_ENABLED", True)
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", False)

    async def must_not_reach_core(*args, **kwargs):
        pytest.fail("loss-cut confirmation bypassed its legacy kill switch")

    monkeypatch.setattr(module, "handle_web_approval", must_not_reach_core)
    app = FastAPI()

    @app.get("/csrf-seed")
    async def csrf_seed() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(
        id=9, role=UserRole.trader
    )
    app.add_middleware(TemplateFormCSRFMiddleware, secret="router-test-secret")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/csrf-seed")
        response = await client.post(
            f"/invest/api/approvals/{uuid.uuid4()}/loss-cut-confirm",
            json={"confirmation_token": "x" * 32},
            headers={
                "X-CSRFToken": client.cookies["csrftoken"],
                "Idempotency-Key": "confirm-gated",
            },
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_web_approval_requires_idempotency_key(monkeypatch) -> None:
    from app.routers import invest_loss_cut_approvals as module

    monkeypatch.setattr(module, "handle_web_approval", pytest.fail)
    app = _app(user=SimpleNamespace(id=10, role=UserRole.trader))
    proposal_id = uuid.uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/csrf-seed")
        response = await client.post(
            f"/invest/api/approvals/{proposal_id}/deny",
            headers={"X-CSRFToken": client.cookies["csrftoken"]},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "idempotency_key_required"}
