"""ROB-207 bulk ingest bridge endpoint tests: token-auth + idempotency."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.integration
def test_bulk_ingest_idempotent_on_run_uuid(monkeypatch):
    from app.core.config import settings
    from app.middleware.auth import AuthMiddleware
    from app.routers import research_reports as router_module

    monkeypatch.setattr(settings, "RESEARCH_REPORTS_INGEST_TOKEN", "t", raising=False)
    monkeypatch.setattr(
        settings,
        "RESEARCH_REPORTS_INGEST_TOKEN_HEADER",
        "X-Research-Reports-Ingest-Token",
        raising=False,
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(router_module.router)

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
    headers = {"X-Research-Reports-Ingest-Token": "t"}
    with TestClient(app) as client:
        r1 = client.post(
            "/trading/api/research-reports/ingest/bulk", json=payload, headers=headers
        )
        r2 = client.post(
            "/trading/api/research-reports/ingest/bulk", json=payload, headers=headers
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    b1 = r1.json()
    b2 = r2.json()
    assert b1["inserted_count"] == 1
    assert b2["inserted_count"] == 0
    assert b2["skipped_count"] == 1


@pytest.mark.integration
def test_bulk_ingest_rejects_full_text_exported(monkeypatch):
    from app.core.config import settings
    from app.middleware.auth import AuthMiddleware
    from app.routers import research_reports as router_module

    monkeypatch.setattr(settings, "RESEARCH_REPORTS_INGEST_TOKEN", "t", raising=False)
    monkeypatch.setattr(
        settings,
        "RESEARCH_REPORTS_INGEST_TOKEN_HEADER",
        "X-Research-Reports-Ingest-Token",
        raising=False,
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(router_module.router)

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
                "attribution": {"full_text_exported": True},
            }
        ],
    }
    with TestClient(app) as client:
        r = client.post(
            "/trading/api/research-reports/ingest/bulk",
            json=payload,
            headers={"X-Research-Reports-Ingest-Token": "t"},
        )
    assert r.status_code in (400, 422)
