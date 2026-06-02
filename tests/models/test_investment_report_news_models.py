# tests/models/test_investment_report_news_models.py
"""ROB-423 PR2 — news fetch-run + citation ORM registration."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_news_models_importable_and_in_review_schema() -> None:
    from app.models import (
        InvestmentReportNewsCitation,
        InvestmentReportNewsFetchRun,
    )

    assert (
        InvestmentReportNewsFetchRun.__tablename__
        == "investment_report_news_fetch_runs"
    )
    assert (
        InvestmentReportNewsCitation.__tablename__ == "investment_report_news_citations"
    )
    assert InvestmentReportNewsFetchRun.__table__.schema == "review"
    assert InvestmentReportNewsCitation.__table__.schema == "review"
    # citation references fetch_run (nullable FK) and carries judgment fields
    cols = InvestmentReportNewsCitation.__table__.columns.keys()
    for c in ("report_uuid", "role", "decision_impact", "relevance", "canonical_url"):
        assert c in cols
