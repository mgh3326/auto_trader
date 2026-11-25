"""hk 6343: research minute-bar ingest/targets gate and token contract.

Acceptance 1: gate off → 404 before DB access; token unset → 403; wrong token →
401. A session cookie is never a substitute for the machine token.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.routers.research_minutes_ingest import router as ingest_router

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_INGEST_PATH = "/trading/api/research-minutes/ingest"
_TARGETS_PATH = "/trading/api/research-minutes/targets?market=kr"
_HEADER = "X-Research-Minutes-Ingest-Token"
_TOKEN = "minutes-secret"

_BAR: dict[str, Any] = {
    "symbol": "005930",
    "venue": "KRX",
    "time_utc": "2026-11-24T00:00:00Z",
    "open": "100",
    "high": "110",
    "low": "90",
    "close": "105",
    "volume": "10",
    "received_at": "2026-11-24T00:01:00Z",
    "source": "KIS",
}
_INGEST_BODY: dict[str, Any] = {"market": "kr", "bars": [_BAR]}


def _db_must_not_be_used() -> Any:
    raise AssertionError("gate-off surface must not resolve a DB dependency")


def _build_app(db: Any = None) -> FastAPI:
    app = FastAPI()
    app.include_router(ingest_router)
    app.add_middleware(AuthMiddleware)
    app.dependency_overrides[get_db] = (
        _db_must_not_be_used if db is None else (lambda: db)
    )
    return app


async def _reply(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    db: Any = None,
):
    app = _build_app(db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        if cookies:
            client.cookies.update(cookies)
        return await client.request(method, path, json=body, headers=headers or {})


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_TOKEN", _TOKEN, raising=False
    )
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_TOKEN_HEADER", _HEADER, raising=False
    )


@pytest.mark.parametrize(
    "method,path", [("POST", _INGEST_PATH), ("GET", _TARGETS_PATH)]
)
async def test_gate_off_answers_404_before_any_db_access(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_ENABLED", False, raising=False
    )
    # A token is configured and supplied: the gate still wins, so a disabled
    # surface never reveals token state either.
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_TOKEN", _TOKEN, raising=False
    )
    reply = await _reply(
        method,
        path,
        body=_INGEST_BODY if method == "POST" else None,
        headers={_HEADER: _TOKEN},
    )
    assert reply.status_code == 404
    assert reply.json() == {"detail": "Not Found"}


async def test_unset_token_answers_403(
    armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RESEARCH_MINUTES_INGEST_TOKEN", "", raising=False)
    reply = await _reply("POST", _INGEST_PATH, body=_INGEST_BODY)
    assert reply.status_code == 403
    assert "not configured" in reply.json()["detail"].lower()


async def test_blank_header_name_answers_403(
    armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_TOKEN_HEADER", "   ", raising=False
    )
    reply = await _reply(
        "POST", _INGEST_PATH, body=_INGEST_BODY, headers={_HEADER: _TOKEN}
    )
    assert reply.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [{}, {_HEADER: "wrong"}, {_HEADER: _TOKEN + "x"}],
)
async def test_missing_or_wrong_token_answers_401(
    armed: None, headers: dict[str, str]
) -> None:
    reply = await _reply("POST", _INGEST_PATH, body=_INGEST_BODY, headers=headers)
    assert reply.status_code == 401


async def test_session_cookie_cannot_substitute_for_the_machine_token(
    armed: None,
) -> None:
    reply = await _reply(
        "POST", _INGEST_PATH, body=_INGEST_BODY, cookies={"session": "logged-in"}
    )
    assert reply.status_code == 401


async def test_targets_read_needs_the_same_token(armed: None) -> None:
    reply = await _reply("GET", _TARGETS_PATH)
    assert reply.status_code == 401


async def test_valid_token_reaches_the_handler(armed: None) -> None:
    """A correct token passes the gate; the body then gets ordinary validation."""

    class _Session:
        pass

    reply = await _reply(
        "POST",
        _INGEST_PATH,
        body={"market": "kr", "bars": []},
        headers={_HEADER: _TOKEN},
        db=_Session(),
    )
    assert reply.status_code == 422


async def test_crafted_host_cannot_skip_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``request.url.path`` is Host-derived; auth must use the routed path.

    ``Host: x/auth/?`` makes Starlette parse the URL path as ``/auth/`` (public)
    while routing still matches ``/trading/api/research-minutes/ingest``.
    """
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_ENABLED", False, raising=False
    )
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_TOKEN", _TOKEN, raising=False
    )
    reply = await _reply(
        "POST",
        _INGEST_PATH,
        body=_INGEST_BODY,
        headers={"host": "x/auth/?"},
    )
    assert reply.status_code == 404
    assert reply.json() == {"detail": "Not Found"}


async def test_crafted_host_cannot_skip_the_token(armed: None) -> None:
    reply = await _reply(
        "POST",
        _INGEST_PATH,
        body=_INGEST_BODY,
        headers={"host": "x/auth/?"},
    )
    assert reply.status_code == 401


async def test_non_ascii_token_is_401_not_500(armed: None) -> None:
    """Header values are latin-1 decoded; str ``compare_digest`` raises there."""
    reply = await _reply(
        "POST", _INGEST_PATH, body=_INGEST_BODY, headers={_HEADER: b"w\xe9"}
    )
    assert reply.status_code == 401


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("false", False),
        ("TRUE", False),
        ("1", False),
        ("yes", False),
        ("on", False),
        ("y", False),
        ("", False),
    ],
)
async def test_gate_opens_only_on_exactly_true(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    """pydantic bool coercion would accept 1/yes/on/TRUE; the contract does not."""
    monkeypatch.setenv("RESEARCH_MINUTES_INGEST_ENABLED", raw)
    from app.core.config import Settings

    assert Settings(_env_file=None).RESEARCH_MINUTES_INGEST_ENABLED is expected
