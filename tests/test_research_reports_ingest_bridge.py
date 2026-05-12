"""ROB-207 bulk ingest bridge endpoint tests: token-auth + idempotency."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

INGEST_TOKEN_HEADER = "X-Research-Reports-Ingest-Token"
INGEST_HEADERS = {INGEST_TOKEN_HEADER: "t"}
INGEST_PATH = "/trading/api/research-reports/ingest/bulk"


def _build_token_authed_app(monkeypatch) -> FastAPI:
    from app.core.config import settings
    from app.middleware.auth import AuthMiddleware
    from app.routers import research_reports as router_module

    monkeypatch.setattr(settings, "RESEARCH_REPORTS_INGEST_TOKEN", "t", raising=False)
    monkeypatch.setattr(
        settings,
        "RESEARCH_REPORTS_INGEST_TOKEN_HEADER",
        INGEST_TOKEN_HEADER,
        raising=False,
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(router_module.router)
    return app


@pytest.mark.integration
def test_bulk_ingest_idempotent_on_run_uuid(monkeypatch):
    app = _build_token_authed_app(monkeypatch)

    payload = {
        "research_report_ingestion_run": {
            "run_uuid": f"run-{uuid4()}",
            "payload_version": "research-reports.v1",
            "source": "naver_research",
            "report_count": 1,
        },
        "reports": [
            {
                "dedup_key": f"k-bridge-{uuid4()}",
                "report_type": "equity_research",
                "source": "naver_research",
                "title": "Bridge smoke",
                "attribution": {
                    "publisher": "naver_research",
                    "copyright_notice": "© Naver",
                    "full_text_exported": False,
                    "pdf_body_exported": False,
                },
            }
        ],
    }
    with TestClient(app) as client:
        r1 = client.post(INGEST_PATH, json=payload, headers=INGEST_HEADERS)
        r2 = client.post(INGEST_PATH, json=payload, headers=INGEST_HEADERS)
    assert r1.status_code == 200
    assert r2.status_code == 200
    b1 = r1.json()
    b2 = r2.json()
    assert b1["inserted_count"] == 1
    assert b2["inserted_count"] == 0
    assert b2["skipped_count"] == 1


@pytest.mark.integration
def test_bulk_ingest_rejects_full_text_exported(monkeypatch):
    app = _build_token_authed_app(monkeypatch)

    payload = {
        "research_report_ingestion_run": {
            "run_uuid": f"run-{uuid4()}",
            "payload_version": "research-reports.v1",
            "source": "naver_research",
            "report_count": 1,
        },
        "reports": [
            {
                "dedup_key": f"k-bad-{uuid4()}",
                "report_type": "equity_research",
                "source": "naver_research",
                "title": "Guardrail test",
                "attribution": {
                    "publisher": "naver_research",
                    "copyright_notice": "© Naver",
                    "full_text_exported": True,
                    "pdf_body_exported": False,
                },
            }
        ],
    }
    with TestClient(app) as client:
        r = client.post(INGEST_PATH, json=payload, headers=INGEST_HEADERS)
    assert r.status_code in (400, 422)
    assert "full_text_exported" in r.text
