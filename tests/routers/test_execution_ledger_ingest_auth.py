"""execution-ledger ingest token gate (fillwire P0) — 403/401/valid contract.

Mirrors the research-reports / Hermes / news-relevance branch shape: an unset
token (or unset header name) is fail-closed 403, a wrong token is 401, and a
session cookie is never a substitute for the machine token.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.routers.execution_ledger_ingest import router as ingest_router

_INGEST_PATH = "/trading/api/execution-ledger/fills/ingest"
_TRIGGER_PATH = "/trading/api/execution-ledger/reconcile/trigger"
_HEADER = "X-Execution-Ledger-Ingest-Token"

_INGEST_BODY: dict[str, Any] = {
    "fills": [{"broker": "upbit"}],
    "source": "fillwire",
}
_TRIGGER_BODY: dict[str, Any] = {"market": "kr", "dry_run": True, "reason": "reconnect"}

_CASES = [
    (_INGEST_PATH, _INGEST_BODY),
    (_TRIGGER_PATH, _TRIGGER_BODY),
]


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ingest_router)
    app.add_middleware(AuthMiddleware)
    return app


async def _post(path: str, body: dict[str, Any], headers: dict[str, str] | None = None):
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        return await client.post(path, json=body, headers=headers or {})


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body"), _CASES)
async def test_unconfigured_token_returns_403(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    monkeypatch.setattr(settings, "EXECUTION_LEDGER_INGEST_TOKEN", "", raising=False)
    resp = await _post(path, body)
    assert resp.status_code == 403
    assert "not configured" in cast(str, resp.json()["detail"]).lower()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body"), _CASES)
async def test_unconfigured_header_name_returns_403(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN", "ledger-secret", raising=False
    )
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN_HEADER", "  ", raising=False
    )
    resp = await _post(path, body)
    assert resp.status_code == 403


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body"), _CASES)
async def test_missing_token_returns_401(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN", "ledger-secret", raising=False
    )
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN_HEADER", _HEADER, raising=False
    )
    resp = await _post(path, body)
    assert resp.status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body"), _CASES)
async def test_wrong_token_returns_401(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN", "ledger-secret", raising=False
    )
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN_HEADER", _HEADER, raising=False
    )
    resp = await _post(path, body, headers={_HEADER: "nope"})
    assert resp.status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body"), _CASES)
async def test_session_cookie_cannot_substitute_for_the_machine_token(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    """A logged-in browser session must never reach these endpoints."""
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN", "ledger-secret", raising=False
    )
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN_HEADER", _HEADER, raising=False
    )

    async def _never_called(_request):  # pragma: no cover - must not run
        raise AssertionError("token branch must precede session auth")

    monkeypatch.setattr(AuthMiddleware, "_load_user", staticmethod(_never_called))

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        client.cookies.set("session", "a-valid-looking-session")
        resp = await client.post(path, json=body)
    assert resp.status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_token_reaches_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correct token passes the gate; the body then gets ordinary validation."""
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN", "ledger-secret", raising=False
    )
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN_HEADER", _HEADER, raising=False
    )
    resp = await _post(
        _INGEST_PATH,
        {"fills": [], "source": "fillwire"},
        headers={_HEADER: "ledger-secret"},
    )
    # Past auth: an empty batch is an envelope validation error, not 401/403.
    assert resp.status_code == 422
