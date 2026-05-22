"""ROB-287 Phase A — AuthMiddleware token-gate behaviour for the four
``/trading/api/investment-reports/hermes/*`` endpoints.

The middleware path is identical to the ROB-207 ``research-reports``
bulk-ingest token gate, but the prefix-match catches all four
endpoints in the family. Each guard fires before the FastAPI route
handler, so we don't need a body for the negative cases.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.routers.investment_hermes_http import router as hermes_router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(hermes_router)
    app.add_middleware(AuthMiddleware)

    async def _db_override() -> AsyncIterator[object]:
        fake_db = MagicMock()
        fake_db.commit = AsyncMock()
        fake_db.rollback = AsyncMock()
        yield fake_db

    from app.core.db import get_db

    app.dependency_overrides[get_db] = _db_override
    return app


_PATH_SAMPLES: list[str] = [
    "/trading/api/investment-reports/hermes/prepare-bundle",
    "/trading/api/investment-reports/hermes/context",
    "/trading/api/investment-reports/hermes/stage-artifacts",
    "/trading/api/investment-reports/hermes/composition",
]


@pytest.mark.parametrize("path", _PATH_SAMPLES)
@pytest.mark.asyncio
async def test_unconfigured_token_returns_403(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token unset → 403 'not configured' regardless of body or upstream gate."""
    monkeypatch.setattr(settings, "HERMES_INGEST_TOKEN", "", raising=False)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(path, json={"market": "kr"})
    assert resp.status_code == 403
    assert "not configured" in cast(str, resp.json()["detail"]).lower()


@pytest.mark.parametrize("path", _PATH_SAMPLES)
@pytest.mark.asyncio
async def test_missing_or_wrong_token_returns_401(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token configured + supplied token absent/wrong → 401 'invalid'."""
    monkeypatch.setattr(settings, "HERMES_INGEST_TOKEN", "shared-secret", raising=False)
    monkeypatch.setattr(
        settings,
        "HERMES_INGEST_TOKEN_HEADER",
        "X-Hermes-Ingest-Token",
        raising=False,
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        # No header at all.
        resp = await client.post(path, json={"market": "kr"})
        assert resp.status_code == 401, resp.text
        assert "invalid" in cast(str, resp.json()["detail"]).lower()

        # Wrong header value.
        resp = await client.post(
            path,
            json={"market": "kr"},
            headers={"X-Hermes-Ingest-Token": "not-the-secret"},
        )
        assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_correct_token_lets_request_through_to_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token correct + gate-off downstream → 503 'snapshot_backed_report_generator_disabled'.

    Confirms the middleware lets the request through to the FastAPI
    handler when the token matches. The downstream 503 is itself an
    important invariant — token-auth must NOT silently bypass the
    operational-flag gate.
    """
    monkeypatch.setattr(settings, "HERMES_INGEST_TOKEN", "shared-secret", raising=False)
    monkeypatch.setattr(
        settings,
        "HERMES_INGEST_TOKEN_HEADER",
        "X-Hermes-Ingest-Token",
        raising=False,
    )
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False, raising=False
    )

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            "/trading/api/investment-reports/hermes/context",
            json={"snapshot_bundle_uuid": str(uuid.uuid4())},
            headers={"X-Hermes-Ingest-Token": "shared-secret"},
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "snapshot_backed_report_generator_disabled"


@pytest.mark.asyncio
async def test_non_hermes_path_under_same_family_not_affected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: the prefix match is anchored on ``/hermes/`` — a sibling
    ``/trading/api/investment-reports/`` request should not pick up the
    Hermes token branch (it falls through to the regular session-auth path
    that the existing investment_reports router already handles)."""
    monkeypatch.setattr(settings, "HERMES_INGEST_TOKEN", "", raising=False)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        # 404 (route not registered) is fine — we just want to confirm
        # AuthMiddleware doesn't fire the Hermes branch on this prefix.
        resp = await client.post(
            "/trading/api/investment-reports/something-else",
            json={"x": 1},
        )
    # Either 404 from FastAPI or 401 from the generic session path — both
    # confirm we did NOT route through the Hermes token branch (which
    # would have been 403 'not configured').
    assert resp.status_code != 403
