# tests/schemas/test_hermes_news_citations.py
"""ROB-423 PR2 — ReportNewsCitation additive field."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.investment_report_composition import (
    ReportCompositionResult,
    ReportNewsCitation,
)
from app.schemas.investment_reports import IngestReportItem


def _base_composition(**extra):
    return {
        "snapshot_bundle_uuid": str(uuid.uuid4()),
        "hermes_run_id": "run-1",
        "title": "t",
        "summary": "s",
        **extra,
    }


@pytest.mark.unit
def test_composition_defaults_news_citations_empty() -> None:
    comp = ReportCompositionResult(**_base_composition())
    assert comp.news_citations == []  # legacy payload back-compat


@pytest.mark.unit
def test_news_citation_requires_ref_and_judgment() -> None:
    cit = ReportNewsCitation(
        canonical_url="https://x/1",
        symbol="AAPL",
        relevance="direct",
        role="catalyst",
        decision_impact="strengthen_buy",
        selection_reason="beat",
        client_item_key="ci-1",
    )
    comp = ReportCompositionResult(
        **_base_composition(news_citations=[cit.model_dump()])
    )
    assert comp.news_citations[0].symbol == "AAPL"
    assert comp.news_citations[0].role == "catalyst"


@pytest.mark.unit
def test_news_citation_rejects_bad_role() -> None:
    with pytest.raises(ValidationError):
        ReportNewsCitation(
            canonical_url="https://x/1",
            symbol="AAPL",
            relevance="direct",
            role="bogus",
            decision_impact="strengthen_buy",
        )


@pytest.mark.unit
def test_news_citation_requires_some_ref() -> None:
    with pytest.raises(ValidationError):
        ReportNewsCitation(
            symbol="AAPL",
            relevance="direct",
            role="catalyst",
            decision_impact="strengthen_buy",
        )  # neither external_article_id nor canonical_url


def _item(**overrides) -> IngestReportItem:
    return IngestReportItem(
        **{
            "client_item_key": "k1",
            "item_kind": "action",
            "operation": "review",
            "intent": "buy_review",
            "rationale": "r",
            "apply_policy": "requires_user_approval",
            **overrides,
        }
    )


@pytest.mark.unit
def test_composition_rejects_non_review_operation() -> None:
    items = [_item(operation="create")]
    with pytest.raises(ValidationError, match="advisory-only"):
        ReportCompositionResult(**_base_composition(items=items))


@pytest.mark.unit
def test_composition_rejects_other_apply_policy() -> None:
    with pytest.raises(ValidationError, match="requires_user_approval"):
        ReportCompositionResult(
            **_base_composition(items=[_item(apply_policy="auto_apply")])
        )
