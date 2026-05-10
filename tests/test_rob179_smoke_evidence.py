"""ROB-179 smoke evidence capture. Saves JSON to .smoke-out/rob179-feed-research-evidence.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _make_app():
    from fastapi import FastAPI

    from app.core.db import get_db
    from app.routers.dependencies import get_authenticated_user
    from app.routers.invest_api import get_invest_home_service
    from app.routers.invest_api import router as invest_router
    from app.schemas.invest_home import InvestHomeResponse, InvestHomeResponseMeta
    from app.services.invest_home_service import (
        build_grouped_holdings,
        build_home_summary,
    )

    class _Stub:
        async def get_home(self, *, user_id):
            return InvestHomeResponse(
                homeSummary=build_home_summary([]),
                accounts=[],
                holdings=[],
                groupedHoldings=build_grouped_holdings([]),
                meta=InvestHomeResponseMeta(warnings=[]),
            )

    app = FastAPI()
    app.include_router(invest_router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_invest_home_service] = lambda: _Stub()

    async def _db():
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    return app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rob179_smoke_capture(db_session):
    from fastapi.testclient import TestClient

    from app.models.research_reports import ResearchReport

    source = f"rob179-smoke-{uuid4().hex[:8]}"
    base_dt = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)

    for i in range(7):
        row = ResearchReport(
            dedup_key=f"smoke-{i}-{uuid4()}",
            report_type="equity_research",
            source=source,
            title=f"ROB-179 smoke report {i + 1}",
            analyst="Smoke Tester",
            category="기업분석" if i % 2 == 0 else "산업분석",
            summary_text=f"Summary {i + 1}",
            detail_excerpt=f"Excerpt {i + 1} — no body fields here",
            detail_url=f"https://example.com/r{i + 1}",
            pdf_url=f"https://example.com/r{i + 1}.pdf",
            symbol_candidates=[
                {
                    "symbol": "005930" if i % 2 == 0 else "AAPL",
                    "market": "kr" if i % 2 == 0 else "us",
                    "source": "t",
                }
            ],
            attribution_publisher="Korea Investment",
            attribution_copyright_notice="© Korea Investment",
            attribution_full_text_exported=False,
            attribution_pdf_body_exported=False,
            published_at=base_dt - timedelta(hours=i),
        )
        db_session.add(row)
    await db_session.commit()

    forbidden_fields = frozenset(
        {
            "pdf_body",
            "pdf_text",
            "extracted_text",
            "full_text",
            "article_content",
            "article_body",
            "raw_payload",
            "raw_payload_json",
            "dedup_key",
            "source_report_id",
            "ingestion_run_id",
            "pdf_sha256",
            "pdf_size_bytes",
            "pdf_page_count",
            "pdf_filename",
            "pdf_text_length",
            "attribution_full_text_exported",
            "attribution_pdf_body_exported",
            "raw_text_policy",
        }
    )

    def _scan(obj):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in forbidden_fields:
                    found.append(k)
                found.extend(_scan(v))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(_scan(item))
        return found

    samples = {}
    invariants_ok = True

    with TestClient(_make_app()) as c:
        for tab in ["top", "latest", "kr", "us", "mine", "watchlist"]:
            r = c.get(f"/invest/api/feed/research?source={source}&tab={tab}&limit=3")
            assert r.status_code == 200
            body = r.json()
            bad = _scan(body)
            if bad:
                invariants_ok = False
            samples[tab] = {
                "status": r.status_code,
                "tab": body["tab"],
                "item_count": len(body["items"]),
                "has_next_cursor": body["nextCursor"] is not None,
                "forbidden_fields_found": bad,
                "excerpt_lengths": [
                    len(item.get("excerpt") or "") for item in body["items"]
                ],
            }

        # Cursor round-trip
        r1 = c.get(f"/invest/api/feed/research?source={source}&tab=latest&limit=3")
        b1 = r1.json()
        cursor_ok = False
        if b1.get("nextCursor"):
            r2 = c.get(
                f"/invest/api/feed/research?source={source}&tab=latest&limit=3&cursor={b1['nextCursor']}"
            )
            b2 = r2.json()
            ids1 = {i["id"] for i in b1["items"]}
            ids2 = {i["id"] for i in b2["items"]}
            cursor_ok = ids1.isdisjoint(ids2)
            if not cursor_ok:
                invariants_ok = False
        samples["cursor_round_trip"] = {
            "page1_count": len(b1["items"]),
            "next_cursor_present": b1.get("nextCursor") is not None,
            "cursor_disjoint": cursor_ok,
        }

    assert invariants_ok, f"Smoke invariants failed: {samples}"

    evidence = {
        "smoke": "rob-179-invest-research-api",
        "captured_at": datetime.now(UTC).isoformat(),
        "source_used": source,
        "invariants_ok": invariants_ok,
        "samples": samples,
    }
    out_path = (
        Path(__file__).parent.parent
        / ".smoke-out"
        / "rob179-feed-research-evidence.json"
    )
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(evidence, indent=2))
