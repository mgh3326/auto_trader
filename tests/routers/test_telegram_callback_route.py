"""ROB-816 PR 2 — HTTP transport for the Telegram order-proposals webhook.

Covers:

* Gate-off behaviour (``ORDER_PROPOSALS_TELEGRAM_ENABLED``): the endpoint
  short-circuits ``503`` with the structured
  ``order_proposals_telegram_disabled`` body — same shape as the Hermes
  HTTP transport's gate-off.
* Happy path: gate on -> ``handle_callback_update`` is invoked with the
  raw request body and a timezone-aware ``now``, and the endpoint always
  answers ``200 {"ok": True}`` regardless of the handler's internal
  result (Telegram's webhook contract only inspects the HTTP status).

Token-auth handling via ``AuthMiddleware`` is exercised separately in
``tests/middleware/test_auth_telegram_branch.py`` — this module focuses on
the route/business-logic behaviour, with ``AuthMiddleware`` included only
in the happy-path test to prove the secret-token header actually lets the
request through end-to-end.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.routers.telegram_callback import router as telegram_router

_PATH = "/trading/api/telegram/callback"


def _build_app(*, with_auth_middleware: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(telegram_router)
    if with_auth_middleware:
        app.add_middleware(AuthMiddleware)
    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_off_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", False, raising=False
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(_PATH, json={"update_id": 1})
    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error"] == "order_proposals_telegram_disabled"
    assert "hint" in body["detail"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_on_valid_token_invokes_handler_and_returns_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_TOKEN", "secret", raising=False
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER",
        "X-Telegram-Bot-Api-Secret-Token",
        raising=False,
    )

    update_payload = {
        "update_id": 123,
        "callback_query": {
            "id": "cbq-1",
            "data": "ap:abcdef01:nonce123",
            "from": {"id": 777},
            "message": {"chat": {"id": 42}, "message_id": 555},
        },
    }

    fake_handler = AsyncMock(return_value={"handled": True, "reason": "approved"})
    app = _build_app(with_auth_middleware=True)

    with patch("app.routers.telegram_callback.handle_callback_update", fake_handler):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                _PATH,
                json=update_payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    fake_handler.assert_awaited_once()
    call_args, call_kwargs = fake_handler.call_args
    assert call_args[0] == update_payload
    now_arg = call_kwargs["now"]
    assert isinstance(now_arg, datetime)
    assert now_arg.tzinfo is not None  # must be timezone-aware, per Task 14


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_on_valid_token_returns_ok_even_when_handler_reports_not_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route always answers 200 {"ok": True} to Telegram regardless of
    the handler's internal result dict -- Telegram's webhook contract only
    inspects the HTTP status, and ``handle_callback_update`` never raises
    (Task 14's fail-closed guarantee)."""
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_TOKEN", "secret", raising=False
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER",
        "X-Telegram-Bot-Api-Secret-Token",
        raising=False,
    )

    fake_handler = AsyncMock(
        return_value={"handled": False, "reason": "chat_not_allowed"}
    )
    app = _build_app(with_auth_middleware=True)

    with patch("app.routers.telegram_callback.handle_callback_update", fake_handler):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                _PATH,
                json={"update_id": 999},
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    fake_handler.assert_awaited_once()
