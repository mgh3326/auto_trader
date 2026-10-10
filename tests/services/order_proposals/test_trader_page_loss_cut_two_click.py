"""Task 890 PR A -- a single /trader click can never approve a loss_cut.

Drives the real /invest web approval route the /trader loss-cut buttons post to
(CSRF middleware, real ``handle_web_approval``, real shared core, real DB). Only
the broker leg (``revalidate_fn``) and the broker preview are fakes that record
whether submission was reached.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.csrf import TemplateFormCSRFMiddleware
from app.models.trading import UserRole
from app.routers import invest_loss_cut_approvals as web_router_module
from app.routers.dependencies import get_authenticated_user
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import revalidation as revalidation_module
from app.services.order_proposals import telegram_callback as callback_module
from app.services.order_proposals.revalidation import RungOutcome
from tests.services.order_proposals.test_telegram_callback import (
    _fake_loss_cut_preview,
    _seed_loss_cut_proposal,
    _session_factory,
)
from tests.services.order_proposals.window_fakes import allow_known_session

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def submits(monkeypatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(
        callback_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(
        revalidation_module, "evaluate_approval_window", allow_known_session
    )
    monkeypatch.setattr(settings, "INVEST_APPROVALS_ENABLED", True)
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    return []


def _app(db_session, monkeypatch, submits: list[dict[str, Any]]) -> FastAPI:
    real = callback_module.handle_web_approval

    async def fake_revalidate(**kwargs: Any):
        submits.append(kwargs)
        return [RungOutcome(0, "submitted_resting", {})]

    async def bound(*args: Any, **kwargs: Any):
        return await real(
            *args,
            service_factory=_session_factory(db_session),
            revalidate_fn=fake_revalidate,
            loss_cut_preview_fn=_fake_loss_cut_preview,
            **kwargs,
        )

    monkeypatch.setattr(web_router_module, "handle_web_approval", bound)
    app = FastAPI()

    @app.get("/csrf-seed")
    async def csrf_seed() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(web_router_module.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(
        id=9, role=UserRole.trader
    )
    app.dependency_overrides[get_db] = lambda: db_session
    app.add_middleware(TemplateFormCSRFMiddleware, secret="two-click-test-secret")
    return app


async def _post(app, proposal_id: uuid.UUID, action: str, body=None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/csrf-seed")
        return await client.post(
            f"/invest/api/approvals/{proposal_id}/{action}",
            json=body or {},
            headers={
                "X-CSRFToken": client.cookies["csrftoken"],
                "Idempotency-Key": f"click-{uuid.uuid4()}",
            },
        )


async def _assert_not_approved(db_session, proposal_id: uuid.UUID) -> None:
    db_session.expire_all()
    group, rungs = await OrderProposalsService(db_session).get_proposal(proposal_id)
    assert group.approved_at is None
    assert [rung.state for rung in rungs] == ["pending_approval"]
    assert all(rung.broker_order_id is None for rung in rungs)


async def test_single_approve_click_on_loss_cut_does_not_submit(
    db_session, monkeypatch, submits
) -> None:
    group = await _seed_loss_cut_proposal(db_session, monkeypatch, nonce="tc-single")
    app = _app(db_session, monkeypatch, submits)

    first = await _post(app, group.proposal_id, "approve")

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["reason"] == "loss_cut_confirmation_required"
    assert isinstance(body["confirmation_token"], str)
    assert submits == []
    await _assert_not_approved(db_session, group.proposal_id)

    # A double click / replayed first click is not a second step either.
    again = await _post(app, group.proposal_id, "approve")
    assert again.status_code in (200, 409), again.text
    if again.status_code == 200:
        assert again.json()["handled"] is False
    assert submits == []
    await _assert_not_approved(db_session, group.proposal_id)


async def test_confirmation_without_the_first_click_token_does_not_submit(
    db_session, monkeypatch, submits
) -> None:
    group = await _seed_loss_cut_proposal(db_session, monkeypatch, nonce="tc-notoken")
    app = _app(db_session, monkeypatch, submits)

    # No first click at all: a forged confirmation must fail closed.
    forged = await _post(
        app,
        group.proposal_id,
        "loss-cut-confirm",
        body={"confirmation_token": "f" * 43},
    )
    assert forged.status_code in (200, 409), forged.text
    if forged.status_code == 200:
        assert forged.json()["handled"] is False
    assert submits == []
    await _assert_not_approved(db_session, group.proposal_id)


async def test_confirmation_with_a_wrong_token_does_not_submit(
    db_session, monkeypatch, submits
) -> None:
    group = await _seed_loss_cut_proposal(db_session, monkeypatch, nonce="tc-wrongtk")
    app = _app(db_session, monkeypatch, submits)

    first = await _post(app, group.proposal_id, "approve")
    assert first.json()["reason"] == "loss_cut_confirmation_required"

    wrong = await _post(
        app,
        group.proposal_id,
        "loss-cut-confirm",
        body={"confirmation_token": "w" * 43},
    )
    assert wrong.status_code in (200, 409), wrong.text
    if wrong.status_code == 200:
        assert wrong.json()["handled"] is False
    assert submits == []
    await _assert_not_approved(db_session, group.proposal_id)


async def test_two_token_bound_clicks_submit_exactly_once(
    db_session, monkeypatch, submits
) -> None:
    group = await _seed_loss_cut_proposal(db_session, monkeypatch, nonce="tc-twoclk")
    app = _app(db_session, monkeypatch, submits)

    first = await _post(app, group.proposal_id, "approve")
    token = first.json()["confirmation_token"]
    assert submits == []

    second = await _post(
        app,
        group.proposal_id,
        "loss-cut-confirm",
        body={"confirmation_token": token},
    )

    assert second.status_code == 200, second.text
    assert second.json()["reason"] == "approved"
    assert len(submits) == 1
    assert submits[0]["proposal_id"] == group.proposal_id

    # Replaying the second click cannot submit again.
    replay = await _post(
        app,
        group.proposal_id,
        "loss-cut-confirm",
        body={"confirmation_token": token},
    )
    assert replay.status_code in (200, 409), replay.text
    assert len(submits) == 1
