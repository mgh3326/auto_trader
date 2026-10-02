"""Task 890 PR A -- /trader approval inbox routes: auth, CSRF, surface.

Core contract (builder-owned):

* every new route rejects an unauthenticated request exactly as /invest does;
* the inbox reads use the /invest approval hub's role gate (403 for viewer);
* the new routes are reads only -- the only state-changing endpoints the page
  calls are the existing /invest approval endpoints and the existing
  protected-positions PUT, and each of those is CSRF-protected by the real
  application middleware configuration.
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware import Middleware

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.middleware.csrf import TemplateFormCSRFMiddleware
from app.models.trading import UserRole
from app.routers import (
    invest_loss_cut_approvals,
    invest_protected_positions,
    trader_page,
)
from app.routers.dependencies import get_authenticated_user
from app.schemas.trader_approvals import (
    TraderApprovalDetailResponse,
    TraderApprovalInboxResponse,
)

T0 = dt.datetime(2026, 9, 30, 1, 0, tzinfo=dt.UTC)
PROPOSAL_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")

#: Every route this PR adds.
NEW_ROUTES = (
    "/trading/api/trader/approvals",
    f"/trading/api/trader/approvals/{PROPOSAL_ID}",
)

#: Every state-changing endpoint the /trader page calls (all pre-existing).
PAGE_WRITE_TARGETS = (
    ("POST", f"/invest/api/approvals/{PROPOSAL_ID}/approve"),
    ("POST", f"/invest/api/approvals/{PROPOSAL_ID}/deny"),
    ("POST", f"/invest/api/approvals/{PROPOSAL_ID}/loss-cut-confirm"),
    ("PUT", "/invest/api/settings/protected-positions/kis_live/kr/005930"),
)


class _StubInbox:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_inbox(self) -> TraderApprovalInboxResponse:
        self.calls.append("list")
        return TraderApprovalInboxResponse(
            as_of=T0,
            actions_enabled=True,
            loss_cut_actions_enabled=False,
            count=0,
            items=[],
        )

    async def get_item(self, proposal_id: uuid.UUID):
        self.calls.append(f"get:{proposal_id}")
        return None


def _app(*, user: object | None, inbox: _StubInbox) -> FastAPI:
    app = FastAPI()
    app.include_router(trader_page.router)
    app.include_router(invest_loss_cut_approvals.router)
    app.include_router(invest_protected_positions.router)
    if user is not None:
        app.dependency_overrides[get_authenticated_user] = lambda: user
    app.dependency_overrides[trader_page.get_trader_approval_inbox_service] = lambda: (
        inbox
    )

    async def _fake_db():
        yield None

    app.dependency_overrides[get_db] = _fake_db
    return app


@pytest.mark.unit
def test_unauthenticated_new_routes_get_the_same_rejection_as_invest() -> None:
    inbox = _StubInbox()
    client = TestClient(
        AuthMiddleware(_app(user=None, inbox=inbox)), raise_server_exceptions=False
    )

    reference = client.get("/invest/api/approvals")
    assert reference.status_code == 401
    for path in NEW_ROUTES:
        response = client.get(path)
        assert response.status_code == reference.status_code, path
        assert response.json() == reference.json(), path
    assert inbox.calls == []


@pytest.mark.unit
def test_unauthenticated_page_write_targets_are_rejected_before_routing() -> None:
    inbox = _StubInbox()
    client = TestClient(
        AuthMiddleware(_app(user=None, inbox=inbox)), raise_server_exceptions=False
    )
    reference = client.get("/invest/api/approvals")
    for method, path in PAGE_WRITE_TARGETS:
        response = client.request(method, path, json={})
        assert response.status_code == 401, (method, path)
        assert response.json() == reference.json(), (method, path)


@pytest.mark.unit
def test_viewer_role_gets_the_invest_hub_rejection(monkeypatch) -> None:
    monkeypatch.setattr(settings, "INVEST_APPROVALS_ENABLED", True)
    inbox = _StubInbox()
    viewer = SimpleNamespace(id=5, role=UserRole.viewer)
    client = TestClient(_app(user=viewer, inbox=inbox), raise_server_exceptions=False)

    reference = client.get("/invest/api/approvals")
    assert reference.status_code == 403
    for path in NEW_ROUTES:
        response = client.get(path)
        assert response.status_code == 403, path
        assert response.json() == reference.json(), path
    assert inbox.calls == []


@pytest.mark.unit
def test_trader_role_reads_the_inbox_and_unknown_detail_is_404() -> None:
    inbox = _StubInbox()
    trader = SimpleNamespace(id=6, role=UserRole.trader)
    client = TestClient(_app(user=trader, inbox=inbox), raise_server_exceptions=False)

    listed = client.get("/trading/api/trader/approvals")
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json()["actions_enabled"] is True

    missing = client.get(f"/trading/api/trader/approvals/{PROPOSAL_ID}")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "proposal_not_found"}
    assert inbox.calls == ["list", f"get:{PROPOSAL_ID}"]


@pytest.mark.unit
def test_detail_route_returns_the_projection() -> None:
    from app.schemas.trader_approvals import TraderApprovalItem

    class _One(_StubInbox):
        async def get_item(self, proposal_id):
            self.calls.append("get")
            return TraderApprovalDetailResponse(
                as_of=T0,
                actions_enabled=False,
                loss_cut_actions_enabled=False,
                item=TraderApprovalItem(
                    proposal_id=str(proposal_id),
                    symbol="005930",
                    market="equity_kr",
                    account_mode="kis_live",
                    broker_account_id=None,
                    side="buy",
                    order_type="limit",
                    action="place",
                    exit_intent=None,
                    requires_two_step=False,
                    card_kind="manual",
                    lifecycle_state="submitted",
                    rungs=[],
                    total_quantity=None,
                    total_notional=None,
                    distance_pct=None,
                    distance_price_asof=None,
                    tier=None,
                    caveats=[],
                    valid_until=T0,
                    expires_in_seconds=0,
                    approved_at=T0,
                    approved_by_channel="web",
                    commit_lease_active=False,
                    actionable=False,
                    block_reason="nonce_used",
                ),
            )

    trader = SimpleNamespace(id=6, role=UserRole.trader)
    client = TestClient(_app(user=trader, inbox=_One()))
    response = client.get(f"/trading/api/trader/approvals/{PROPOSAL_ID}")
    assert response.status_code == 200
    assert response.json()["item"]["actionable"] is False
    assert response.json()["item"]["approved_by_channel"] == "web"


@pytest.mark.unit
def test_new_trader_routes_are_reads_only() -> None:
    for route in trader_page.router.routes:
        methods = getattr(route, "methods", set()) or set()
        assert methods <= {"GET", "HEAD"}, getattr(route, "path", "?")
    paths = {getattr(route, "path", "") for route in trader_page.router.routes}
    assert "/trading/api/trader/approvals" in paths
    assert "/trading/api/trader/approvals/{proposal_id}" in paths


def _real_csrf_middleware() -> Middleware:
    from app.main import api

    matches = [m for m in api.user_middleware if m.cls is TemplateFormCSRFMiddleware]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.unit
def test_page_write_targets_are_not_csrf_exempt_in_the_real_app() -> None:
    middleware = _real_csrf_middleware()
    exempt = middleware.kwargs["exempt_urls"]
    for _method, path in PAGE_WRITE_TARGETS:
        hits = [pattern.pattern for pattern in exempt if pattern.match(path)]
        assert hits == [], (path, hits)


@pytest.mark.unit
@pytest.mark.parametrize(("method", "path"), PAGE_WRITE_TARGETS)
def test_page_write_targets_refuse_a_missing_csrf_token(
    monkeypatch, method, path
) -> None:
    monkeypatch.setattr(settings, "INVEST_APPROVALS_ENABLED", True)
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    reached: list[str] = []

    async def fake_web_approval(*args, **kwargs):
        reached.append("approval")
        return {"handled": True, "reason": "stub"}

    async def fake_save(*args, **kwargs):
        reached.append("protected_save")
        raise invest_protected_positions.ProtectionStateUnavailable("stub")

    monkeypatch.setattr(
        invest_loss_cut_approvals, "handle_web_approval", fake_web_approval
    )
    monkeypatch.setattr(
        invest_protected_positions.ProtectedQuantityService, "save", fake_save
    )
    app = _app(user=SimpleNamespace(id=1, role=UserRole.admin), inbox=_StubInbox())
    from app.auth.admin_router import require_admin

    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        id=1, role=UserRole.admin
    )
    real = _real_csrf_middleware()
    app.add_middleware(
        TemplateFormCSRFMiddleware,
        secret="csrf-test-secret",
        exempt_urls=real.kwargs["exempt_urls"],
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.get("/trading/api/trader/approvals")  # seeds the csrftoken cookie
    token = client.cookies["csrftoken"]

    body = (
        {
            "protected_quantity": "1",
            "reason": "r",
            "expected_revision": None,
            "idempotency_key": "k",
            "confirm_protection_change": True,
        }
        if method == "PUT"
        else {"confirmation_token": "t" * 32}
    )
    refused = client.request(
        method, path, json=body, headers={"Idempotency-Key": "click"}
    )
    assert refused.status_code == 403, (method, path, refused.text)
    assert reached == []

    # Positive control: the same request with the CSRF header reaches the
    # endpoint, so the 403 above is the CSRF gate and nothing else.
    accepted = client.request(
        method,
        path,
        json=body,
        headers={"Idempotency-Key": "click", "X-CSRFToken": token},
    )
    assert accepted.status_code != 403, (method, path, accepted.text)
    assert len(reached) == 1
