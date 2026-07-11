"""ROB-816 PR 2 — AuthMiddleware secret-token gate for the Telegram
order-proposals approval webhook (``/trading/api/telegram/*``).

Same prefix-token shape as the Hermes / news-relevance branches
(``test_investment_hermes_http_auth.py`` / ``test_news_relevance_auth.py``):

* token unset -> 403 "not configured"
* header name unset -> 403 "not configured" (header, not token)
* wrong/missing supplied token -> 401 "invalid"
* correct token -> middleware passes through (``None``); the downstream
  ``ORDER_PROPOSALS_TELEGRAM_ENABLED`` gate-off 503 proves the request
  actually reached the FastAPI route handler rather than being short
  circuited earlier by an unrelated branch.
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.routers.telegram_callback import router as telegram_router

_PATH = "/trading/api/telegram/callback"
_BODY: dict = {"update_id": 1}


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(telegram_router)
    app.add_middleware(AuthMiddleware)
    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconfigured_token_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_TOKEN", "", raising=False)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(_PATH, json=_BODY)
    assert resp.status_code == 403
    assert "not configured" in cast(str, resp.json()["detail"]).lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconfigured_header_name_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_TOKEN", "secret", raising=False
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER", "   ", raising=False
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            _PATH, json=_BODY, headers={"X-Telegram-Bot-Api-Secret-Token": "secret"}
        )
    assert resp.status_code == 403
    assert "not configured" in cast(str, resp.json()["detail"]).lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_or_wrong_token_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_TOKEN", "secret", raising=False
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER",
        "X-Telegram-Bot-Api-Secret-Token",
        raising=False,
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        # No header at all.
        resp = await client.post(_PATH, json=_BODY)
        assert resp.status_code == 401, resp.text
        assert "invalid" in cast(str, resp.json()["detail"]).lower()

        # Wrong header value.
        resp = await client.post(
            _PATH,
            json=_BODY,
            headers={"X-Telegram-Bot-Api-Secret-Token": "nope"},
        )
        assert resp.status_code == 401, resp.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correct_token_passes_through_to_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correct token -> middleware returns ``None`` (pass-through).

    Downstream ``ORDER_PROPOSALS_TELEGRAM_ENABLED`` is left at its default
    (off), so the request reaching the route handler is proven by the
    structured 503 gate-off body rather than a 401/403 auth failure.
    """
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_TOKEN", "secret", raising=False
    )
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_TOKEN_HEADER",
        "X-Telegram-Bot-Api-Secret-Token",
        raising=False,
    )
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", False, raising=False
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            _PATH, json=_BODY, headers={"X-Telegram-Bot-Api-Secret-Token": "secret"}
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "order_proposals_telegram_disabled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sibling_prefix_not_affected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: the prefix match is anchored on ``/telegram/`` — a sibling
    ``/trading/api/`` request should not pick up this token branch."""
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_TOKEN", "", raising=False)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            "/trading/api/some-other-family/callback", json={"x": 1}
        )
    # 404 (route not registered) confirms we did NOT route through the
    # Telegram token branch (which would have been 403 "not configured").
    assert resp.status_code != 403
