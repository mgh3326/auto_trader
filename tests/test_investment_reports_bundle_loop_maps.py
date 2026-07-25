"""ROB-715 — _serialise_bundle folds forecast/retrospective maps onto response."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.routers.investment_reports import _serialise_bundle
from app.schemas.investment_reports import (
    ForecastLinkResponse,
    RetrospectiveLinkResponse,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)


def _request(*, kst_date: str) -> dict:
    return {
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": f"t-{kst_date}",
        "summary": "s",
        "kst_date": kst_date,
        "items": [
            {
                "client_item_key": "action-1",
                "item_kind": "action",
                "symbol": "005930",
                "side": "buy",
                "intent": "buy_review",
                "rationale": "r",
            }
        ],
    }


async def _build_minimal_bundle_dict(session: AsyncSession) -> dict:
    """Reuse the ingestion + query service to get a real bundle dict."""
    from app.schemas.investment_reports import IngestReportRequest

    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(IngestReportRequest(**_request(kst_date="2026-05-20")))
    query = InvestmentReportQueryService(session)
    bundle = await query.get_bundle(report.report_uuid)
    assert bundle is not None
    # Strip the new maps so we can test the "absent" path too.
    bundle.pop("forecasts_by_item_uuid", None)
    bundle.pop("retrospectives_by_item_uuid", None)
    return bundle


@pytest.mark.asyncio
async def test_serialise_bundle_carries_loop_maps(
    session: AsyncSession,
) -> None:
    bundle = await _build_minimal_bundle_dict(session)
    item = bundle["items"][0]
    key = str(item.item_uuid)

    bundle["forecasts_by_item_uuid"] = {
        key: [ForecastLinkResponse(forecast_id="f1", status="open", probability=0.6)]
    }
    bundle["retrospectives_by_item_uuid"] = {
        key: [RetrospectiveLinkResponse(retrospective_id=1, outcome="filled")]
    }

    out = _serialise_bundle(bundle)

    assert out.forecasts_by_item_uuid[key][0].status == "open"
    assert out.retrospectives_by_item_uuid[key][0].outcome == "filled"


@pytest.mark.asyncio
async def test_serialise_bundle_defaults_empty_when_maps_absent(
    session: AsyncSession,
) -> None:
    bundle = await _build_minimal_bundle_dict(session)
    # Legacy bundle dict without the new keys → empty dicts, no crash.
    out = _serialise_bundle(bundle)
    assert out.forecasts_by_item_uuid == {}
    assert out.retrospectives_by_item_uuid == {}


@pytest.mark.asyncio
async def test_serialise_bundle_sets_structured_evidence_summary(
    session: AsyncSession,
) -> None:
    bundle = await _build_minimal_bundle_dict(session)
    item = bundle["items"][0]
    # Inject structured_evidence into the ORM item before serialisation.
    item.evidence_snapshot = {"structured_evidence": {"valuation": "cheap"}}

    out = _serialise_bundle(bundle)

    assert out.items[0].structured_evidence_summary == "1 evidence fields: valuation"


def _uuid_str() -> str:
    return str(uuid.uuid4())
