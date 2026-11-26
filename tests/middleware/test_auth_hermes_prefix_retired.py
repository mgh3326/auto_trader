"""hk 6933 — the retired Hermes HTTP ingest family has no AuthMiddleware branch.

Before this change ``/trading/api/investment-reports/hermes/`` was a token
branch (403 unset / 401 wrong token). The route and its settings are gone, so
the old prefix must fall through to the app's ordinary auth/route handling:
a supplied legacy token header must change nothing.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, settings
from app.middleware.auth import AuthMiddleware

_LEGACY_PATH = "/trading/api/investment-reports/hermes/context"
_LEGACY_HEADER = "X-Hermes-Ingest-Token"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    return app


@pytest.mark.unit
def test_hermes_token_branch_and_settings_are_gone() -> None:
    assert not any(name.startswith("HERMES_INGEST") for name in vars(AuthMiddleware))
    assert not any(name.startswith("HERMES_INGEST") for name in Settings.model_fields)
    assert not any(name.startswith("HERMES_INGEST") for name in vars(settings))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_prefix_ignores_supplied_token_header() -> None:
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        without_header = await client.post(_LEGACY_PATH, json={})
        with_header = await client.post(
            _LEGACY_PATH, json={}, headers={_LEGACY_HEADER: "anything-supplied"}
        )
    assert with_header.status_code == without_header.status_code
    assert with_header.json() == without_header.json()
    assert "Hermes ingest token" not in with_header.text
