"""ROB-459 P1 — 타입드 per-item evidence 스키마/영속화."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.schemas.investment_reports import IngestReportItem, ItemEvidencePayload

pytestmark = pytest.mark.unit


def test_evidence_payload_requires_source():
    with pytest.raises(Exception):
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
    with pytest.raises(Exception):
        ItemEvidencePayload(source="s", bogus_field="x")
