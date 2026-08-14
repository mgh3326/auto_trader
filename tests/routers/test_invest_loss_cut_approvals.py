from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.middleware.csrf import TemplateFormCSRFMiddleware
from app.models.trading import UserRole
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_loss_cut_approvals import (
    get_loss_cut_approval_service,
    router,
)
from app.schemas.loss_cut_approval import (
    LossCutBeginResponse,
    LossCutEvidenceField,
    LossCutEvidenceResponse,
)


def _evidence(proposal_id: uuid.UUID) -> LossCutEvidenceResponse:
    return LossCutEvidenceResponse(
        mode="proposal",
        symbol="AAPL",
        proposal_id=str(proposal_id),
        generated_at="2026-08-14T06:00:00+00:00",
        can_begin=True,
        positions=[],
        loss=LossCutEvidenceField(status="filled", label="손실률", value={}),
        reason=LossCutEvidenceField(status="filled", label="사유 판정", value={}),
        r931=LossCutEvidenceField(
            status="unavailable", label="R-931", reason="not recorded"
        ),
        consensus=LossCutEvidenceField(
            status="missing", label="컨센서스", reason="no snapshot"
        ),
        watch=LossCutEvidenceField(
            status="missing", label="워치 맥락", reason="not-registered"
        ),
        fingerprint={"proposal_id": str(proposal_id)},
    )


class _FakeService:
    def __init__(self, proposal_id: uuid.UUID) -> None:
        self.proposal_id = proposal_id
        self.begin_calls: list[dict] = []

    async def get_proposal_evidence(self, *, proposal_id: uuid.UUID):
        assert proposal_id == self.proposal_id
        return _evidence(proposal_id)

    async def begin(self, **kwargs):
        self.begin_calls.append(kwargs)
        return LossCutBeginResponse(
            proposal_id=str(self.proposal_id),
            ceremony_id="c" * 48,
            expires_at="2026-08-14T06:01:30+00:00",
            evidence=_evidence(self.proposal_id),
            fingerprint={"proposal_id": str(self.proposal_id)},
        )


def _build_app(*, user_dependency, service: _FakeService, include_auth=False):
    app = FastAPI()

    @app.get("/csrf-seed")
    async def csrf_seed():
        return {"ok": True}

    app.include_router(router)
    db = AsyncMock()
    app.dependency_overrides[get_authenticated_user] = user_dependency
    app.dependency_overrides[get_loss_cut_approval_service] = lambda: service
    app.dependency_overrides[get_db] = lambda: db
    if include_auth:
        app.add_middleware(AuthMiddleware)
    app.add_middleware(TemplateFormCSRFMiddleware, secret="router-test-secret")
    return app, db


async def _client(app: FastAPI):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_loss_cut_get_requires_authenticated_session(monkeypatch):
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    proposal_id = uuid.uuid4()

    async def unauthenticated():
        raise HTTPException(status_code=401, detail="Not authenticated")

    app, _db = _build_app(
        user_dependency=unauthenticated,
        service=_FakeService(proposal_id),
    )
    async with await _client(app) as client:
        response = await client.get(f"/invest/api/loss-cut-approvals/{proposal_id}")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_loss_cut_get_rejects_viewer_role(monkeypatch):
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    proposal_id = uuid.uuid4()
    app, _db = _build_app(
        user_dependency=lambda: SimpleNamespace(id=7, role=UserRole.viewer),
        service=_FakeService(proposal_id),
    )
    async with await _client(app) as client:
        response = await client.get(f"/invest/api/loss-cut-approvals/{proposal_id}")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_loss_cut_get_is_no_store_for_trader(monkeypatch):
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    proposal_id = uuid.uuid4()
    app, _db = _build_app(
        user_dependency=lambda: SimpleNamespace(id=8, role=UserRole.trader),
        service=_FakeService(proposal_id),
    )
    async with await _client(app) as client:
        response = await client.get(f"/invest/api/loss-cut-approvals/{proposal_id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_loss_cut_begin_requires_csrf_before_handler(monkeypatch):
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    proposal_id = uuid.uuid4()
    service = _FakeService(proposal_id)
    app, db = _build_app(
        user_dependency=lambda: SimpleNamespace(id=9, role=UserRole.trader),
        service=service,
    )
    async with await _client(app) as client:
        response = await client.post(
            f"/invest/api/loss-cut-approvals/{proposal_id}/begin",
            json={},
        )

    assert response.status_code == 403
    assert service.begin_calls == []
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_csrf_cannot_bypass_session_auth_middleware(monkeypatch):
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    monkeypatch.setattr(AuthMiddleware, "_load_user", AsyncMock(return_value=None))
    proposal_id = uuid.uuid4()
    service = _FakeService(proposal_id)
    app, db = _build_app(
        user_dependency=lambda: SimpleNamespace(id=99, role=UserRole.trader),
        service=service,
        include_auth=True,
    )
    async with await _client(app) as client:
        seed = await client.get("/csrf-seed", follow_redirects=False)
        assert seed.status_code == 303
        csrf = client.cookies["csrftoken"]
        response = await client.post(
            f"/invest/api/loss-cut-approvals/{proposal_id}/begin",
            json={},
            headers={"X-CSRFToken": csrf},
        )

    assert response.status_code == 401
    assert service.begin_calls == []
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_cannot_inject_nonce_or_scope_into_begin(monkeypatch):
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    proposal_id = uuid.uuid4()
    service = _FakeService(proposal_id)
    app, db = _build_app(
        user_dependency=lambda: SimpleNamespace(id=10, role=UserRole.trader),
        service=service,
    )
    async with await _client(app) as client:
        await client.get("/csrf-seed")
        csrf = client.cookies["csrftoken"]
        response = await client.post(
            f"/invest/api/loss-cut-approvals/{proposal_id}/begin",
            json={"approval_nonce": "forged", "quantity": "999"},
            headers={"X-CSRFToken": csrf},
        )
        confirm_response = await client.post(
            f"/invest/api/loss-cut-approvals/{proposal_id}/confirm",
            json={"ceremony_id": "c" * 48, "approval_nonce": "forged"},
            headers={"X-CSRFToken": csrf},
        )

    assert response.status_code == 422
    assert confirm_response.status_code == 422
    assert service.begin_calls == []
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_csrf_begin_uses_only_authenticated_actor(monkeypatch):
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    proposal_id = uuid.uuid4()
    service = _FakeService(proposal_id)
    app, db = _build_app(
        user_dependency=lambda: SimpleNamespace(id=11, role=UserRole.trader),
        service=service,
    )
    async with await _client(app) as client:
        await client.get("/csrf-seed")
        csrf = client.cookies["csrftoken"]
        response = await client.post(
            f"/invest/api/loss-cut-approvals/{proposal_id}/begin",
            json={},
            headers={"X-CSRFToken": csrf},
        )

    assert response.status_code == 200
    assert service.begin_calls == [
        {
            "proposal_id": proposal_id,
            "actor_user_id": 11,
            "actor_role": UserRole.trader,
        }
    ]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_loss_cut_route_has_no_unwalled_alias(monkeypatch):
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    proposal_id = uuid.uuid4()
    app, _db = _build_app(
        user_dependency=lambda: SimpleNamespace(id=12, role=UserRole.trader),
        service=_FakeService(proposal_id),
    )
    async with await _client(app) as client:
        responses = [
            await client.get(f"/invest/loss-cut-approvals/{proposal_id}"),
            await client.get(f"/api/loss-cut-approvals/{proposal_id}"),
            await client.get(f"/loss-cut-approvals/{proposal_id}"),
        ]

    assert [response.status_code for response in responses] == [404, 404, 404]


def test_loss_cut_flags_are_default_off():
    fields = type(settings).model_fields
    assert fields["INVEST_LOSS_CUT_EVIDENCE_ENABLED"].default is False
    assert fields["INVEST_LOSS_CUT_APPROVAL_ENABLED"].default is False
