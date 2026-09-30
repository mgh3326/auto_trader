"""Task 890 PR A -- the /trader buttons reach the Telegram approval core.

The /trader inbox buttons post to the existing /invest web approval endpoints
(``POST /invest/api/approvals/{proposal_id}/{approve|deny|loss-cut-confirm}``,
``app/routers/invest_loss_cut_approvals.py``). Those run
``telegram_callback.handle_web_approval``, which calls the SAME handler
functions the Telegram webhook (``handle_callback_update`` ->
``handle_normalized_callback``) calls:

=========================  ==============================================
click                      shared handler
=========================  ==============================================
approve (place/replace)    ``telegram_callback._handle_approve``
reject                     ``telegram_callback._handle_deny``
approve on a loss_cut      ``telegram_callback._handle_loss_cut_first_click``
loss-cut 2nd click         ``telegram_callback._handle_approve``
                           (``loss_cut_confirmation=True``)
=========================  ==============================================

Each test drives the real Telegram entry point and the real /invest HTTP route
(with the real CSRF middleware) against the same published proposal, spies the
shared handler, binds both calls to the handler signature with defaults
applied, and requires every argument to be equal except the channel identity
keys below. The spy never executes the core, so no broker path is reachable.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
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
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    CallbackEnvelope,
)
from tests.services.order_proposals.test_telegram_callback import (
    _allow_chat,
    _FakeNotifier,
    _make_update,
    _proposal_callback_data,
    _publish_fixture_card,
    _seed_loss_cut_proposal,
    _seed_proposal,
    _session_factory,
)

WEB_USER_ID = 9

#: The only arguments allowed to differ between the Telegram and web calls.
#: They identify the clicking channel/principal, the Telegram message to edit
#: (absent on the web), the per-request clock, and the per-request
#: session/service objects -- never what is approved or how it is executed.
CHANNEL_IDENTITY_KEYS = frozenset(
    {
        "session",
        "service",
        "now",
        "now_fn",
        "notifier",
        "chat_id",
        "message_id",
        "callback_query_id",
        "telegram_user_id",
        "actor_channel",
        "actor_subject",
        "web_confirmation_token",
    }
)


class _Spy:
    def __init__(self, name: str, real: Callable[..., Any]) -> None:
        self.name = name
        self.signature = inspect.signature(real)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        bound = self.signature.bind(**kwargs)
        bound.apply_defaults()
        self.calls.append(dict(bound.arguments))
        return {
            "handled": True,
            "reason": "spy",
            "proposal_id": str(kwargs["proposal_id"]),
        }


@pytest.fixture
def spies(monkeypatch) -> dict[str, _Spy]:
    installed: dict[str, _Spy] = {}
    for name in (
        "_handle_approve",
        "_handle_deny",
        "_handle_loss_cut_first_click",
        "_handle_auto_veto",
        "_handle_batch_approve",
    ):
        spy = _Spy(name, getattr(callback_module, name))
        monkeypatch.setattr(callback_module, name, spy)
        installed[name] = spy
    _allow_chat(monkeypatch)
    monkeypatch.setattr(settings, "INVEST_APPROVALS_ENABLED", True)
    monkeypatch.setattr(settings, "INVEST_LOSS_CUT_APPROVAL_ENABLED", True)
    return installed


def _web_app(db_session, monkeypatch, factory) -> FastAPI:
    """The real /invest approval router behind the real CSRF middleware.

    ``handle_web_approval`` stays the real function; the test only binds the
    DB session factory so the core reads the seeded proposal.
    """
    real = callback_module.handle_web_approval

    async def bound_handle_web_approval(*args: Any, **kwargs: Any):
        return await real(*args, service_factory=factory, **kwargs)

    monkeypatch.setattr(
        web_router_module, "handle_web_approval", bound_handle_web_approval
    )
    app = FastAPI()

    @app.get("/csrf-seed")
    async def csrf_seed() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(web_router_module.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(
        id=WEB_USER_ID, role=UserRole.trader
    )
    app.dependency_overrides[get_db] = lambda: db_session
    app.add_middleware(TemplateFormCSRFMiddleware, secret="same-path-test-secret")
    return app


async def _click_web(app: FastAPI, proposal_id: uuid.UUID, action: str, body=None):
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


async def _click_telegram(group, *, action: str, factory) -> dict[str, Any]:
    return await callback_module.handle_callback_update(
        _make_update(data=_proposal_callback_data(group, action=action)),
        now=datetime.now(UTC),
        service_factory=factory,
        notifier=_FakeNotifier(),
    )


def _assert_same_call(telegram: dict[str, Any], web: dict[str, Any]) -> None:
    assert set(telegram) == set(web)
    differing = {key for key in telegram if telegram[key] != web[key]}
    assert differing <= CHANNEL_IDENTITY_KEYS, differing - CHANNEL_IDENTITY_KEYS
    # What is approved and how it executes are identical by value.
    assert isinstance(telegram["callback"], CallbackEnvelope)
    assert telegram["callback"] == web["callback"]
    assert telegram["proposal_id"] == web["proposal_id"]
    # The same real service class serves both channels.
    assert type(telegram["service"]) is OrderProposalsService
    assert type(web["service"]) is OrderProposalsService
    # Channel identity: the web principal is the authenticated user and no
    # Telegram message is touched.
    assert telegram["actor_channel"] == "telegram"
    assert web["actor_channel"] == "web"
    assert web["actor_subject"] == f"user:{WEB_USER_ID}"
    assert web["notifier"] is None
    assert web["chat_id"] is None
    assert web["message_id"] is None


def _latest_group(db_session):
    async def load(proposal_id):
        group, _ = await OrderProposalsService(db_session).get_proposal(proposal_id)
        return group

    return load


@pytest.mark.asyncio
@pytest.mark.integration
async def test_approve_click_reaches_handle_approve_with_the_telegram_arguments(
    db_session, monkeypatch, spies
) -> None:
    group = await _seed_proposal(db_session, nonce="same-path-op", symbol="SP1")
    factory = _session_factory(db_session)

    telegram_result = await _click_telegram(group, action="op", factory=factory)
    response = await _click_web(
        _web_app(db_session, monkeypatch, factory), group.proposal_id, "approve"
    )

    assert telegram_result["reason"] == "spy"
    assert response.status_code == 200, response.text
    assert response.json()["reason"] == "spy"
    calls = spies["_handle_approve"].calls
    assert len(calls) == 2
    telegram, web = calls
    _assert_same_call(telegram, web)
    assert telegram["callback"].action == "op"
    assert telegram["revalidate_fn"] is revalidation_module.revalidate_and_submit
    assert web["revalidate_fn"] is revalidation_module.revalidate_and_submit
    assert telegram["loss_cut_confirmation"] is False
    assert web["loss_cut_confirmation"] is False
    assert telegram["service_factory"] is factory
    assert web["service_factory"] is factory
    for other in ("_handle_deny", "_handle_loss_cut_first_click"):
        assert spies[other].calls == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reject_click_reaches_handle_deny_with_the_telegram_arguments(
    db_session, monkeypatch, spies
) -> None:
    group = await _seed_proposal(db_session, nonce="same-path-dn", symbol="SP2")
    factory = _session_factory(db_session)

    await _click_telegram(group, action="dn", factory=factory)
    response = await _click_web(
        _web_app(db_session, monkeypatch, factory), group.proposal_id, "deny"
    )

    assert response.status_code == 200, response.text
    calls = spies["_handle_deny"].calls
    assert len(calls) == 2
    telegram, web = calls
    _assert_same_call(telegram, web)
    assert telegram["callback"].action == "dn"
    assert spies["_handle_approve"].calls == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_loss_cut_approve_click_is_only_the_first_step_on_both_channels(
    db_session, monkeypatch, spies
) -> None:
    group = await _seed_loss_cut_proposal(db_session, monkeypatch, nonce="same-path-lc")
    factory = _session_factory(db_session)

    await _click_telegram(group, action="op", factory=factory)
    response = await _click_web(
        _web_app(db_session, monkeypatch, factory), group.proposal_id, "approve"
    )

    assert response.status_code == 200, response.text
    # A loss-cut approve never reaches the submitting handler on either path.
    assert spies["_handle_approve"].calls == []
    calls = spies["_handle_loss_cut_first_click"].calls
    assert len(calls) == 2
    telegram, web = calls
    _assert_same_call(telegram, web)
    assert telegram["callback"].action == "op"
    assert (
        telegram["loss_cut_preview_fn"]
        is web["loss_cut_preview_fn"]
        is revalidation_module.preview_loss_cut_confirmation
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_loss_cut_second_click_reaches_handle_approve_as_confirmation(
    db_session, monkeypatch, spies
) -> None:
    group = await _seed_loss_cut_proposal(db_session, monkeypatch, nonce="same-path-l2")
    service = OrderProposalsService(db_session)
    await _publish_fixture_card(
        service,
        group,
        nonce="same-path-l2",
        card_kind=ApprovalCardKind.LOSS_CUT_CONFIRMATION,
    )
    await db_session.commit()
    group = await _latest_group(db_session)(group.proposal_id)
    factory = _session_factory(db_session)

    await _click_telegram(group, action="lc", factory=factory)
    response = await _click_web(
        _web_app(db_session, monkeypatch, factory),
        group.proposal_id,
        "loss-cut-confirm",
        body={"confirmation_token": "t" * 32},
    )

    assert response.status_code == 200, response.text
    calls = spies["_handle_approve"].calls
    assert len(calls) == 2
    telegram, web = calls
    _assert_same_call(telegram, web)
    assert telegram["callback"].action == "lc"
    assert telegram["loss_cut_confirmation"] is True
    assert web["loss_cut_confirmation"] is True
    # The browser's opaque token is carried only as channel identity; the
    # server-side nonce inside ``callback`` is identical on both channels.
    assert web["web_confirmation_token"] == "t" * 32
    assert telegram["web_confirmation_token"] is None
