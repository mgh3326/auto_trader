"""ROB-459 P1 — 타입드 per-item evidence 스키마/영속화."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.investment_reports import IngestReportItem, ItemEvidencePayload

pytestmark = pytest.mark.unit


def test_evidence_payload_requires_source():
    with pytest.raises(ValidationError):
        ItemEvidencePayload(metric="buy_ratings", value=10)  # source 누락


def test_item_accepts_structured_evidence():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="action",
        intent="buy_review",
        rationale="컨센 10buy",
        evidence=[
            {"source": "consensus", "metric": "buy_ratings", "value": 10},
            {
                "source": "foreign_flow",
                "metric": "net",
                "value": "1.2e9",
                "as_of": "2026-06-09",
                "freshness": "fresh",
            },
        ],
        freshness="fresh",
    )
    assert len(item.evidence) == 2
    assert item.evidence[0].source == "consensus"
    assert item.freshness == "fresh"


def test_item_evidence_defaults_empty():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
    )
    assert item.evidence == []
    assert item.freshness is None


def test_evidence_value_decimal_and_str_round_trip_json():
    payload = ItemEvidencePayload(source="s", value=Decimal("10.5"))
    dumped = payload.model_dump(mode="json")
    assert dumped["value"] == "10.5"  # Decimal → JSON string


def test_evidence_payload_forbids_unknown_keys():
    with pytest.raises(ValidationError):
        ItemEvidencePayload(source="s", bogus_field="x")


@pytest.mark.asyncio
async def test_structured_evidence_round_trips_through_ingestion(session) -> None:
    """create→저장→read에서 structured_evidence/item_freshness가 노출된다."""
    from app.schemas.investment_reports import IngestReportRequest
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.repository import InvestmentReportsRepository

    repo = InvestmentReportsRepository(session)
    svc = InvestmentReportIngestionService(session, repository=repo)
    report = await svc.ingest(
        IngestReportRequest(
            report_type="advisory_lite_v1",
            market="kr",
            created_by_profile="CLAUDE_ADVISOR",
            title="t",
            summary="s",
            kst_date="2026-06-09",
            status="draft",
            items=[
                IngestReportItem(
                    client_item_key="k1",
                    item_kind="action",
                    intent="buy_review",
                    rationale="컨센 10buy / 외국인 순매수",
                    evidence=[
                        {"source": "consensus", "metric": "buy_ratings", "value": 10},
                    ],
                    freshness="fresh",
                )
            ],
        )
    )
    await session.flush()
    items = await repo.list_items_for_report(report.id)
    assert len(items) == 1
    snap = items[0].evidence_snapshot
    assert snap["structured_evidence"][0]["source"] == "consensus"
    assert snap["structured_evidence"][0]["value"] in (10, "10")
    assert snap["item_freshness"] == "fresh"


@pytest.mark.asyncio
async def test_no_evidence_leaves_snapshot_keys_absent(session) -> None:
    """evidence 미지정 시 reserved key를 추가하지 않는다(기존 동작 무변화)."""
    from app.schemas.investment_reports import IngestReportRequest
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.repository import InvestmentReportsRepository

    repo = InvestmentReportsRepository(session)
    svc = InvestmentReportIngestionService(session, repository=repo)
    report = await svc.ingest(
        IngestReportRequest(
            report_type="advisory_lite_v1",
            market="kr",
            created_by_profile="CLAUDE_ADVISOR",
            title="t",
            summary="s",
            kst_date="2026-06-09",
            status="draft",
            items=[
                IngestReportItem(
                    client_item_key="k1",
                    item_kind="action",
                    intent="buy_review",
                    rationale="r",
                )
            ],
        )
    )
    await session.flush()
    items = await repo.list_items_for_report(report.id)
    snap = items[0].evidence_snapshot or {}
    assert "structured_evidence" not in snap
    assert "item_freshness" not in snap


def test_item_forbids_unknown_top_level_keys():
    with pytest.raises(ValidationError) as exc:
        IngestReportItem(
            client_item_key="k1",
            item_kind="action",
            intent="buy_review",
            rationale="r",
            entry_price=Decimal("100.0"),
        )
    assert "entry_price" in str(exc.value)


def test_item_accepts_typed_trade_plan_fields():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        rationale="1차/2차 분할 진입",
        entry_plan=[
            {"label": "1차", "price": Decimal("70000"), "quantity": Decimal("1")},
            {"label": "2차", "price": Decimal("68000"), "quantity": Decimal("1")},
        ],
        stop_loss={"price": Decimal("65000"), "condition": "종가 이탈"},
        target_price={"price": Decimal("78000"), "condition": "저항 돌파"},
        linked_order_ids=[
            {
                "broker": "kis",
                "account_scope": "kis_live",
                "odno": "0026500500",
                "ledger_id": 123,
            }
        ],
    )

    assert item.entry_plan[0].label == "1차"
    assert item.entry_plan[0].price == Decimal("70000")
    assert item.stop_loss is not None
    assert item.stop_loss.price == Decimal("65000")
    assert item.target_price is not None
    assert item.target_price.price == Decimal("78000")
    assert item.linked_order_ids[0].odno == "0026500500"


def test_linked_order_ref_requires_identifier():
    with pytest.raises(ValidationError) as exc:
        IngestReportItem(
            client_item_key="k1",
            item_kind="action",
            intent="buy_review",
            rationale="r",
            linked_order_ids=[{"broker": "kis"}],
        )
    assert "linked_order_ids" in str(exc.value)
    assert "one of order_no, odno, ledger_id, report_item_uuid" in str(exc.value)


def test_typed_trade_plan_rejects_evidence_snapshot_reserved_key_conflict():
    with pytest.raises(ValidationError) as exc:
        IngestReportItem(
            client_item_key="k1",
            item_kind="action",
            intent="buy_review",
            rationale="r",
            evidence_snapshot={"target_price": {"price": "76000"}},
            target_price={"price": Decimal("78000")},
        )
    assert "target_price" in str(exc.value)
    assert "reserved evidence_snapshot keys" in str(exc.value)


@pytest.mark.asyncio
async def test_trade_plan_fields_round_trip_through_evidence_snapshot(session) -> None:
    from app.schemas.investment_reports import IngestReportRequest
    from app.services.investment_reports.ingestion import (
        InvestmentReportIngestionService,
    )
    from app.services.investment_reports.repository import InvestmentReportsRepository

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
            kst_date="2026-06-10",
            status="draft",
            items=[
                IngestReportItem(
                    client_item_key="k1",
                    item_kind="action",
                    symbol="005930",
                    side="buy",
                    intent="buy_review",
                    rationale="분할 진입",
                    entry_plan=[
                        {"label": "1차", "price": Decimal("70000")},
                        {"label": "2차", "price": Decimal("68000")},
                    ],
                    stop_loss={"price": Decimal("65000")},
                    target_price={"price": Decimal("78000")},
                    linked_order_ids=[{"odno": "0026500500", "ledger_id": 123}],
                )
            ],
        )
    )
    await session.flush()

    items = await repo.list_items_for_report(report.id)
    snap = items[0].evidence_snapshot
    assert snap["entry_plan"][0]["label"] == "1차"
    assert snap["entry_plan"][0]["price"] == "70000"
    assert snap["stop_loss"]["price"] == "65000"
    assert snap["target_price"]["price"] == "78000"
    assert snap["linked_order_ids"][0]["odno"] == "0026500500"
    assert snap["linked_order_ids"][0]["ledger_id"] == 123
