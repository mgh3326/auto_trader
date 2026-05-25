"""ROB-287 Phase A — HTTP transport for the four Hermes contract tools.

Covers:

* Gate-off behaviour (settings flag): every endpoint short-circuits
  ``503`` with the structured ``snapshot_backed_report_generator_disabled``
  body.
* Service routing on the happy path: each endpoint calls its matching
  service class and returns the same envelope shape the MCP tool would.
* Service-level error mapping: ``HermesContextExportError`` /
  ``HermesCompositionIngestError`` / ``HermesStageArtifactsIngestError``
  surface as the right HTTP status + structured detail (404 for missing
  bundle, 409 for envelope mismatch / content conflict, etc.).
* Token-auth handling via ``AuthMiddleware`` is exercised in a separate
  test file (``test_investment_hermes_http_auth.py``) — keeping this
  module focused on body validation + service routing so the middleware
  cross-cutting concern doesn't pollute every test case.
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


# ---------------------------------------------------------------------------
# Gate-off behaviour for all four endpoints
# ---------------------------------------------------------------------------


_GATE_OFF_CASES: list[tuple[str, dict]] = [
    (
        "/trading/api/investment-reports/hermes/prepare-bundle",
        {"market": "kr"},
    ),
    (
        "/trading/api/investment-reports/hermes/context",
        {"snapshot_bundle_uuid": str(uuid.uuid4())},
    ),
    (
        "/trading/api/investment-reports/hermes/stage-artifacts",
        {
            "run_envelope": {
                "run_uuid": str(uuid.uuid4()),
                "snapshot_bundle_uuid": str(uuid.uuid4()),
                "market": "kr",
            },
            "artifacts": [
                {"stage_type": "market", "verdict": "neutral", "confidence": 50}
            ],
        },
    ),
    (
        "/trading/api/investment-reports/hermes/composition",
        {
            "composition": {
                "snapshot_bundle_uuid": str(uuid.uuid4()),
                "hermes_run_id": "x",
                "title": "t",
                "summary": "s",
                "items": [],
            },
            "kst_date": "2026-05-21",
            "market": "crypto",
        },
    ),
]


@pytest.mark.parametrize(("url", "body"), _GATE_OFF_CASES)
@pytest.mark.asyncio
async def test_gate_off_returns_503(
    url: str, body: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False, raising=False
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(url, json=body)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["error"] == "snapshot_backed_report_generator_disabled"
    assert "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED" in detail["hint"]


# ---------------------------------------------------------------------------
# prepare-bundle — service routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_bundle_routes_through_ensure_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    bundle_uuid = uuid.uuid4()
    response = SimpleNamespace(bundle_uuid=bundle_uuid, status="complete")
    response.model_dump = lambda mode="json": {
        "bundle_uuid": str(bundle_uuid),
        "status": "complete",
        "coverage_summary": {},
        "freshness_summary": {"overall": "fresh"},
        "missing_sources": [],
        "warnings": [],
        "created": False,
    }
    ensure_svc = AsyncMock()
    ensure_svc.ensure = AsyncMock(return_value=response)

    app = _build_app()
    with (
        patch(
            "app.routers.investment_hermes_http.production_collector_registry",
            return_value=object(),
        ),
        patch(
            "app.routers.investment_hermes_http.SnapshotBundleEnsureService",
            return_value=ensure_svc,
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/prepare-bundle",
                json={"market": "kr", "account_scope": "kis_live"},
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["bundle_uuid"] == str(bundle_uuid)
    called = ensure_svc.ensure.call_args.args[0]
    assert called.purpose == "report_generation"
    assert called.market == "kr"
    assert called.account_scope == "kis_live"
    assert called.requested_by == "hermes"


@pytest.mark.asyncio
async def test_prepare_bundle_injects_production_registry_and_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    bundle_uuid = uuid.uuid4()
    response = SimpleNamespace(bundle_uuid=bundle_uuid, status="partial")
    response.model_dump = lambda mode="json": {
        "bundle_uuid": str(bundle_uuid),
        "status": "partial",
        "coverage_summary": {},
        "freshness_summary": {},
        "missing_sources": [],
        "warnings": [],
        "created": True,
    }
    ensure_svc = AsyncMock()
    ensure_svc.ensure = AsyncMock(return_value=response)
    sentinel_registry = object()

    app = _build_app()
    with (
        patch(
            "app.routers.investment_hermes_http.production_collector_registry",
            return_value=sentinel_registry,
        ) as mock_registry,
        patch(
            "app.routers.investment_hermes_http.SnapshotBundleEnsureService",
            return_value=ensure_svc,
        ) as mock_cls,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/prepare-bundle",
                json={
                    "market": "kr",
                    "account_scope": "kis_live",
                    "symbols": ["005930"],
                    "user_id": 7,
                },
            )

    assert resp.status_code == 200, resp.text
    mock_registry.assert_called_once()
    assert mock_cls.call_args.kwargs["collectors"] is sentinel_registry
    called = ensure_svc.ensure.call_args.args[0]
    assert called.user_id == 7
    assert called.market == "kr"
    assert called.account_scope == "kis_live"


# ---------------------------------------------------------------------------
# context — service routing + missing bundle mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_returns_payload_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.schemas.hermes_composition import HermesContextPayload

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    bundle_uuid = uuid.uuid4()
    payload = HermesContextPayload(
        snapshot_bundle_uuid=bundle_uuid,
        bundle_status="complete",
        market="crypto",
        account_scope="upbit_live",
        policy_version="intraday_action_report_v1",
    )

    exporter = AsyncMock()
    exporter.export = AsyncMock(return_value=payload)

    app = _build_app()
    with patch(
        "app.routers.investment_hermes_http.HermesContextExporter",
        return_value=exporter,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/context",
                json={"snapshot_bundle_uuid": str(bundle_uuid)},
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["context_version"] == "hermes-context.v1"
    assert body["snapshot_bundle_uuid"] == str(bundle_uuid)
    assert body["constraints"]["advisory_only"] is True


@pytest.mark.asyncio
async def test_context_missing_bundle_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.investment_stages.hermes_context import (
        HermesContextExportError,
    )

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )

    bundle_uuid = uuid.uuid4()
    exporter = AsyncMock()
    exporter.export = AsyncMock(side_effect=HermesContextExportError("not found"))

    app = _build_app()
    with patch(
        "app.routers.investment_hermes_http.HermesContextExporter",
        return_value=exporter,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/context",
                json={"snapshot_bundle_uuid": str(bundle_uuid)},
            )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "snapshot_bundle_not_found"
    assert detail["snapshot_bundle_uuid"] == str(bundle_uuid)


# ---------------------------------------------------------------------------
# stage-artifacts — service routing + error mapping
# ---------------------------------------------------------------------------


def _stage_artifact_body() -> dict:
    return {
        "run_envelope": {
            "run_uuid": str(uuid.uuid4()),
            "snapshot_bundle_uuid": str(uuid.uuid4()),
            "market": "kr",
            "market_session": "regular",
            "account_scope": "kis_live",
        },
        "artifacts": [
            {
                "stage_type": "market",
                "verdict": "bull",
                "confidence": 60,
                "summary": "ok",
            }
        ],
    }


@pytest.mark.asyncio
async def test_stage_artifacts_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )

    run_uuid = uuid.uuid4()
    bundle_uuid = uuid.uuid4()
    artifact_uuid = uuid.uuid4()

    svc = AsyncMock()
    svc.ingest_stage_artifacts = AsyncMock(
        return_value=SimpleNamespace(
            run=SimpleNamespace(
                run_uuid=run_uuid,
                status="running",
                snapshot_bundle_uuid=bundle_uuid,
            ),
            results=[
                SimpleNamespace(
                    stage_type="market",
                    artifact=SimpleNamespace(artifact_uuid=artifact_uuid),
                    idempotent_existing=False,
                )
            ],
        )
    )

    body = _stage_artifact_body()

    app = _build_app()
    with patch(
        "app.routers.investment_hermes_http.HermesStageArtifactsIngestService",
        return_value=svc,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/stage-artifacts",
                json=body,
            )

    assert resp.status_code == 200, resp.text
    rb = resp.json()
    assert rb["success"] is True
    assert rb["run_uuid"] == str(run_uuid)
    assert rb["run_status"] == "running"
    assert rb["artifacts"][0]["stage_type"] == "market"
    assert rb["artifacts"][0]["artifact_uuid"] == str(artifact_uuid)
    assert rb["artifacts"][0]["idempotent_existing"] is False


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("snapshot_bundle_not_found", 404),
        ("run_envelope_mismatch", 409),
        ("artifact_content_conflict", 409),
        ("append_only_race", 409),
        ("totally_unknown_code", 400),
    ],
)
@pytest.mark.asyncio
async def test_stage_artifacts_error_mapping(
    error_code: str, expected_status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.investment_stages.hermes_ingest import (
        HermesStageArtifactsIngestError,
    )

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )

    svc = AsyncMock()
    svc.ingest_stage_artifacts = AsyncMock(
        side_effect=HermesStageArtifactsIngestError("synthesized", code=error_code)
    )

    app = _build_app()
    with patch(
        "app.routers.investment_hermes_http.HermesStageArtifactsIngestService",
        return_value=svc,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/stage-artifacts",
                json=_stage_artifact_body(),
            )
    assert resp.status_code == expected_status, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == error_code


@pytest.mark.asyncio
async def test_stage_artifacts_rejects_empty_artifacts_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TS6 enforcement at the HTTP boundary — Pydantic 422 before reaching
    the service."""
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    body = _stage_artifact_body()
    body["artifacts"] = []
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            "/trading/api/investment-reports/hermes/stage-artifacts", json=body
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# composition — service routing + missing bundle mapping
# ---------------------------------------------------------------------------


def _composition_body() -> dict:
    return {
        "composition": {
            "snapshot_bundle_uuid": str(uuid.uuid4()),
            "hermes_run_id": "hermes-1",
            "title": "Hermes Advisory",
            "summary": "Synth",
            "items": [
                {
                    "client_item_key": "auto-buy-BTC",
                    "item_kind": "action",
                    "operation": "review",
                    "symbol": "BTC",
                    "side": "buy",
                    "intent": "buy_review",
                    "rationale": "r",
                    "apply_policy": "requires_user_approval",
                }
            ],
        },
        "kst_date": "2026-05-21",
        "market": "crypto",
        "account_scope": "upbit_live",
    }


@pytest.mark.asyncio
async def test_composition_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )

    report_uuid = uuid.uuid4()
    svc = AsyncMock()
    svc.ingest_composition = AsyncMock(
        return_value=SimpleNamespace(
            report_uuid=report_uuid, idempotency_key="idem-1", status="draft"
        )
    )

    app = _build_app()
    with patch(
        "app.routers.investment_hermes_http.HermesCompositionIngestService",
        return_value=svc,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/composition",
                json=_composition_body(),
            )

    assert resp.status_code == 200, resp.text
    rb = resp.json()
    assert rb["success"] is True
    assert rb["report_uuid"] == str(report_uuid)
    assert rb["idempotency_key"] == "idem-1"
    assert rb["status"] == "draft"
    assert rb["items_count"] == 1


@pytest.mark.asyncio
async def test_composition_missing_bundle_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.investment_stages.hermes_ingest import (
        HermesCompositionIngestError,
    )

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )

    svc = AsyncMock()
    svc.ingest_composition = AsyncMock(
        side_effect=HermesCompositionIngestError("missing")
    )

    app = _build_app()
    with patch(
        "app.routers.investment_hermes_http.HermesCompositionIngestService",
        return_value=svc,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/hermes/composition",
                json=_composition_body(),
            )

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "snapshot_bundle_not_found"


@pytest.mark.asyncio
async def test_composition_rejects_invalid_items_at_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema-level rejection of items that violate advisory-only
    invariants — operation=create, apply_policy != requires_user_approval."""
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    body = _composition_body()
    body["composition"]["items"][0]["operation"] = "create"

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            "/trading/api/investment-reports/hermes/composition", json=body
        )
    # FastAPI surfaces Pydantic ``ValidationError`` as 422 before our
    # handler runs.
    assert resp.status_code == 422
