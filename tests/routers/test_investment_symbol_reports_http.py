"""ROB-301 T4 — HTTP transport for the symbol-reports Hermes ingest route.

Routing-level (service mocked): gate-off 503, happy-path envelope shape, and
service-error -> HTTP status mapping. Token auth is prefix-based and covered by
``test_investment_symbol_reports_http_auth``-style middleware tests; the real DB
ingest is covered by the T3 service tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.routers.investment_hermes_http import router as hermes_router
from app.services.investment_stages.symbol_report_ingest import SymbolReportIngestError

URL = "/trading/api/investment-reports/hermes/symbol-reports"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(hermes_router)

    async def _db_override() -> AsyncIterator[object]:
        fake_db = MagicMock()
        fake_db.commit = AsyncMock()
        fake_db.rollback = AsyncMock()
        yield fake_db

    from app.core.db import get_db

    app.dependency_overrides[get_db] = _db_override
    return app


def _body() -> dict:
    return {
        "run_envelope": {
            "run_uuid": str(uuid.uuid4()),
            "snapshot_bundle_uuid": str(uuid.uuid4()),
            "market": "kr",
            "market_session": "regular",
            "account_scope": "kis_live",
        },
        "symbol_reports": [
            {
                "symbol": "005930.KS",
                "decision_bucket": "new_buy_candidate",
                "side": "buy",
            }
        ],
    }


@pytest.mark.asyncio
async def test_gate_off_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False, raising=False
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(URL, json=_body())
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "snapshot_backed_report_generator_disabled"


@pytest.mark.asyncio
async def test_happy_path_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    run_uuid = uuid.uuid4()
    bundle_uuid = uuid.uuid4()
    report_uuid = uuid.uuid4()

    svc = AsyncMock()
    svc.ingest_from_hermes = AsyncMock(
        return_value=SimpleNamespace(
            run=SimpleNamespace(
                run_uuid=run_uuid, status="running", snapshot_bundle_uuid=bundle_uuid
            ),
            results=[
                SimpleNamespace(
                    symbol="005930.KS",
                    report=SimpleNamespace(
                        symbol_report_uuid=report_uuid,
                        decision_bucket="new_buy_candidate",
                        verdict="buy",
                        artifact_version=1,
                    ),
                    idempotent_existing=False,
                )
            ],
        )
    )
    app = _build_app()
    with patch(
        "app.routers.investment_hermes_http.SymbolIntermediateReportIngestService",
        return_value=svc,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(URL, json=_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["run_uuid"] == str(run_uuid)
    assert body["symbol_reports"][0]["symbol"] == "005930.KS"
    assert body["symbol_reports"][0]["verdict"] == "buy"
    assert body["symbol_reports"][0]["symbol_report_uuid"] == str(report_uuid)
    assert body["symbol_reports"][0]["idempotent_existing"] is False


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("stage_run_not_found", 404),
        ("snapshot_bundle_not_found", 404),
        ("run_envelope_mismatch", 409),
        ("symbol_report_race", 409),
        ("open_action_missing_side", 400),  # falls through to default
    ],
)
@pytest.mark.asyncio
async def test_error_mapping(
    code: str, expected_status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    svc = AsyncMock()
    svc.ingest_from_hermes = AsyncMock(
        side_effect=SymbolReportIngestError("boom", code=code)
    )
    app = _build_app()
    with patch(
        "app.routers.investment_hermes_http.SymbolIntermediateReportIngestService",
        return_value=svc,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(URL, json=_body())
    assert resp.status_code == expected_status
    assert resp.json()["detail"]["error"] == code
