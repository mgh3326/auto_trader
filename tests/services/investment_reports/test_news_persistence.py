# tests/services/investment_reports/test_news_persistence.py
"""ROB-423 PR2 — pure news persistence matching helper."""

from __future__ import annotations

import uuid

import pytest

from app.schemas.hermes_composition import HermesNewsCitation
from app.services.investment_reports.news_persistence import build_news_persistence


def _news_payload():
    return {
        "articles": [
            {
                "title": "Apple beats", "url": "https://x/aapl-1", "source": "Reuters",
                "summary": "strong", "published_at": "2026-05-05T12:00:00",
                "symbol": "AAPL", "provider": "finnhub",
                "external_article_id": "hash-aapl-1", "sentiment": "positive",
            },
            {
                "title": "MSFT cloud", "url": "https://x/msft-1", "source": "Bloomberg",
                "summary": None, "published_at": None,
                "symbol": "MSFT", "provider": "finnhub",
                "external_article_id": "hash-msft-1", "sentiment": None,
            },
        ],
        "fetch_records": [
            {"symbol": "AAPL", "provider": "finnhub", "requested_limit": 20,
             "returned_count": 1, "status": "ok", "error_code": None},
            {"symbol": "MSFT", "provider": "finnhub", "requested_limit": 20,
             "returned_count": 1, "status": "ok", "error_code": None},
        ],
        "market": "us",
    }


@pytest.mark.unit
def test_matches_by_external_id_and_copies_snapshot() -> None:
    item_uuid = uuid.uuid4()
    cites = [
        HermesNewsCitation(
            external_article_id="hash-aapl-1", symbol="AAPL", relevance="direct",
            role="catalyst", decision_impact="strengthen_buy",
            selection_reason="earnings beat", client_item_key="ci-1",
        )
    ]
    plan = build_news_persistence(
        news_payloads=[_news_payload()],
        citations=cites,
        item_uuid_by_client_key={"ci-1": item_uuid},
        instrument_type="equity_us",
    )

    assert len(plan.citations) == 1
    c = plan.citations[0]
    assert c["title"] == "Apple beats"
    assert c["canonical_url"] == "https://x/aapl-1"
    assert c["external_article_id"] == "hash-aapl-1"
    assert c["role"] == "catalyst"
    assert c["decision_impact"] == "strengthen_buy"
    assert c["report_item_uuid"] == item_uuid
    assert c["provider"] == "finnhub"
    # fetch_runs: AAPL used_count=1, MSFT used_count=0
    runs = {r["symbol"]: r for r in plan.fetch_runs}
    assert runs["AAPL"]["used_count"] == 1
    assert runs["AAPL"]["returned_count"] == 1
    assert runs["MSFT"]["used_count"] == 0
    assert plan.unmatched == []


@pytest.mark.unit
def test_unmatched_ref_is_dropped_and_recorded() -> None:
    cites = [
        HermesNewsCitation(
            external_article_id="does-not-exist", symbol="AAPL", relevance="direct",
            role="catalyst", decision_impact="strengthen_buy",
        )
    ]
    plan = build_news_persistence(
        news_payloads=[_news_payload()], citations=cites,
        item_uuid_by_client_key={}, instrument_type="equity_us",
    )
    assert plan.citations == []
    assert plan.unmatched == ["does-not-exist"]


@pytest.mark.unit
def test_matches_by_canonical_url_fallback() -> None:
    cites = [
        HermesNewsCitation(
            canonical_url="https://x/msft-1", symbol="MSFT", relevance="related",
            role="confirmation", decision_impact="hold_watch",
        )
    ]
    plan = build_news_persistence(
        news_payloads=[_news_payload()], citations=cites,
        item_uuid_by_client_key={}, instrument_type="equity_us",
    )
    assert len(plan.citations) == 1
    assert plan.citations[0]["symbol"] == "MSFT"
    assert plan.citations[0]["report_item_uuid"] is None  # no client_item_key
    assert plan.citations[0]["summary_snapshot"] is None


@pytest.mark.unit
def test_summary_truncated_to_1000() -> None:
    payload = _news_payload()
    payload["articles"][0]["summary"] = "x" * 2000
    cites = [
        HermesNewsCitation(
            external_article_id="hash-aapl-1", symbol="AAPL", relevance="direct",
            role="catalyst", decision_impact="strengthen_buy",
        )
    ]
    plan = build_news_persistence(
        news_payloads=[payload], citations=cites,
        item_uuid_by_client_key={}, instrument_type="equity_us",
    )
    assert len(plan.citations[0]["summary_snapshot"]) == 1000
