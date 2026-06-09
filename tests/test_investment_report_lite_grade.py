"""ROB-472 — lite report quality grade 순수 헬퍼."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import investment_reports_handlers as h
from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
from app.services.investment_reports.lite_grade import (
    build_lite_report_quality_summary,
)

pytestmark = pytest.mark.unit


def _item(**over):
    base = {
        "client_item_key": "k1",
        "item_kind": "action",
        "intent": "buy_review",
        "rationale": "r",
    }
    base.update(over)
    return IngestReportItem(**base)


def test_no_actionable_items_grades_no_action():
    items = [_item(item_kind="risk", intent="risk_review")]
    out = build_lite_report_quality_summary(items)
    assert out["grade"] == "no_action"
    assert out["basis"] == "item_evidence_lite"


def test_actionable_but_no_evidence_grades_no_action():
    items = [_item()]  # action item, evidence=[] (default)
    out = build_lite_report_quality_summary(items)
    assert out["grade"] == "no_action"
    assert "evidence" in out["reason"]


def test_evidence_backed_action_grades_informational_only():
    items = [
        _item(
            evidence=[{"source": "consensus", "freshness": "fresh"}],
            freshness="fresh",
        )
    ]
    out = build_lite_report_quality_summary(items)
    assert out["grade"] == "informational_only"
    assert out["evidence_item_count"] == 1
    assert out["actionable_item_count"] == 1


def test_never_returns_high_confidence_even_with_rich_evidence():
    items = [
        _item(
            client_item_key=f"k{i}",
            evidence=[
                {"source": "consensus", "freshness": "fresh"},
                {"source": "foreign_flow", "freshness": "fresh"},
            ],
            freshness="fresh",
        )
        for i in range(5)
    ]
    out = build_lite_report_quality_summary(items)
    assert out["grade"] != "high_confidence"
    assert out["grade"] == "informational_only"


def test_freshness_breakdown_counts_item_and_evidence():
    items = [
        _item(
            evidence=[
                {"source": "a", "freshness": "fresh"},
                {"source": "b", "freshness": "stale"},
            ],
            freshness="soft_stale",
        )
    ]
    out = build_lite_report_quality_summary(items)
    # item.freshness(soft_stale) + evidence(fresh, stale)
    assert out["freshness_breakdown"] == {
        "fresh": 1,
        "soft_stale": 1,
        "stale": 1,
        "unknown": 0,
    }
    assert out["evidence_source_count"] == 2


def test_empty_items_grades_no_action():
    out = build_lite_report_quality_summary([])
    assert out["grade"] == "no_action"
    assert out["total_item_count"] == 0


def _request(profile="CLAUDE_ADVISOR", **over):
    base = {
        "report_type": "advisory_lite_v1",
        "market": "kr",
        "created_by_profile": profile,
        "title": "t",
        "summary": "s",
        "kst_date": "2026-06-09",
        "status": "draft",
        "items": [
            _item(
                evidence=[{"source": "consensus", "freshness": "fresh"}],
                freshness="fresh",
            )
        ],
    }
    base.update(over)
    return IngestReportRequest(**base)


def test_attach_lite_quality_advisory_profile_populates():
    out = h._maybe_attach_lite_quality(_request(profile="CLAUDE_ADVISOR"))
    rqs = out.snapshot_report_diagnostics["report_quality_summary"]
    assert rqs["grade"] == "informational_only"
    assert rqs["basis"] == "item_evidence_lite"


def test_attach_lite_quality_non_advisory_profile_skips():
    out = h._maybe_attach_lite_quality(_request(profile="t"))
    assert out.snapshot_report_diagnostics is None


def test_attach_lite_quality_does_not_clobber_caller_diagnostics():
    caller = {"report_quality_summary": {"grade": "no_action", "basis": "caller"}}
    out = h._maybe_attach_lite_quality(
        _request(profile="CLAUDE_ADVISOR", snapshot_report_diagnostics=caller)
    )
    assert out.snapshot_report_diagnostics == caller


@pytest.mark.asyncio
async def test_lite_diagnostics_persists_and_round_trips(session) -> None:
    """글루가 붙인 lite grade가 저장되고 ORM으로 round-trip된다."""
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.repository import (
        InvestmentReportsRepository,
    )

    request = h._maybe_attach_lite_quality(_request(profile="CLAUDE_ADVISOR"))
    repo = InvestmentReportsRepository(session)
    svc = InvestmentReportIngestionService(session, repository=repo)
    report = await svc.ingest(request)
    await session.flush()

    diag = report.snapshot_report_diagnostics
    assert diag is not None
    assert diag["report_quality_summary"]["grade"] == "informational_only"
    assert diag["report_quality_summary"]["basis"] == "item_evidence_lite"
