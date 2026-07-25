"""ROB-693 — invalidation_triggers: Hermes-authored advisory narrative field
("what would invalidate this thesis"), persisted verbatim (no synthesis) into
evidence_snapshot.invalidation_triggers at ingestion write-time. Migration-0:
no new column, no alembic revision, no response-schema change — round-trips
through the existing InvestmentReportItemResponse.evidence_snapshot JSONB.

Distinct from WatchInvalidation (app/schemas/investment_reports.py, scanner-
executable price_below/condition_text trigger attached to
WatchRecommendationPayload.invalidation) — this field is advisory narrative
only and does not drive the watch scanner.
"""

from __future__ import annotations

import pytest

from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository

pytestmark = pytest.mark.unit


async def _ingest_single_item_report(
    session, item: IngestReportItem, *, kst_date: str = "2026-07-04"
):
    repo = InvestmentReportsRepository(session)
    svc = InvestmentReportIngestionService(session, repository=repo)
    report = await svc.ingest(
        IngestReportRequest(
            report_type="advisory_lite_v1",
            market="kr",
            account_scope="kis_live",
            created_by_profile="CLAUDE_ADVISOR",
            title="t",
            summary="s",
            kst_date=kst_date,
            status="draft",
            items=[item],
        )
    )
    await session.flush()
    items = await repo.list_items_for_report(report.id)
    return items[0].evidence_snapshot or {}


@pytest.mark.asyncio
async def test_invalidation_triggers_round_trip_preserves_values_and_order(
    session,
) -> None:
    snap = await _ingest_single_item_report(
        session,
        IngestReportItem(
            client_item_key="k1",
            item_kind="action",
            symbol="005930",
            side="buy",
            intent="buy_review",
            rationale="반도체 업황 개선 기대",
            invalidation_triggers=[
                "분기 가이던스 하향 조정",
                "RSI 30 하회 후 5일 지속",
                "외국인 3일 연속 순매도 전환",
            ],
        ),
    )
    assert snap["invalidation_triggers"] == [
        "분기 가이던스 하향 조정",
        "RSI 30 하회 후 5일 지속",
        "외국인 3일 연속 순매도 전환",
    ]


@pytest.mark.asyncio
async def test_invalidation_triggers_empty_list_leaves_snapshot_key_absent(
    session,
) -> None:
    """No invalidation_triggers supplied -> key not added (legacy JSONB shape
    unchanged, exactly like structured_evidence/entry_plan/trade_setup)."""
    snap = await _ingest_single_item_report(
        session,
        IngestReportItem(
            client_item_key="k1",
            item_kind="action",
            intent="buy_review",
            rationale="r",
        ),
    )
    assert "invalidation_triggers" not in snap


@pytest.mark.asyncio
async def test_invalidation_triggers_coexists_with_other_evidence_snapshot_keys(
    session,
) -> None:
    """Additive: invalidation_triggers merges alongside other reserved keys
    (e.g. a caller-supplied raw evidence_snapshot dict) without clobbering
    them."""
    snap = await _ingest_single_item_report(
        session,
        IngestReportItem(
            client_item_key="k1",
            item_kind="action",
            intent="buy_review",
            rationale="r",
            evidence_snapshot={"action_verdict": "buy"},
            invalidation_triggers=["실적 쇼크"],
        ),
    )
    assert snap["action_verdict"] == "buy"
    assert snap["invalidation_triggers"] == ["실적 쇼크"]


@pytest.mark.asyncio
async def test_invalidation_triggers_applies_to_watch_and_risk_items_too(
    session,
) -> None:
    """The field is not action-only — watch/risk items can carry it too (it
    is per-item advisory narrative, independent of item_kind)."""
    snap = await _ingest_single_item_report(
        session,
        IngestReportItem(
            client_item_key="k1",
            item_kind="risk",
            intent="risk_review",
            rationale="포지션 리스크 점검",
            invalidation_triggers=["변동성 지수 급등"],
        ),
    )
    assert snap["invalidation_triggers"] == ["변동성 지수 급등"]


def test_hermes_composition_result_items_accept_invalidation_triggers() -> None:
    """Hermes ingest wiring — HermesCompositionResult.items is
    list[IngestReportItem] (app/schemas/hermes_composition.py), so the field
    auto-plumbs with no edits to hermes_composition.py/hermes_ingest.py. This
    test only proves the additive contract at the schema boundary Hermes
    actually calls through; the DB round-trip above already covers
    persistence."""
    import uuid

    from app.schemas.hermes_composition import HermesCompositionResult

    result = HermesCompositionResult(
        snapshot_bundle_uuid=uuid.uuid4(),
        hermes_run_id="run-1",
        title="t",
        summary="s",
        items=[
            IngestReportItem(
                client_item_key="k1",
                item_kind="action",
                operation="review",
                intent="buy_review",
                rationale="r",
                apply_policy="requires_user_approval",
                invalidation_triggers=["실적 가이던스 하향"],
            )
        ],
    )
    assert result.items[0].invalidation_triggers == ["실적 가이던스 하향"]
