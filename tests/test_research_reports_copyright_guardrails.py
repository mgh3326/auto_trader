"""Copyright guardrails (ROB-140).

These tests are intentionally redundant with the schema/query tests, so that any
future change that introduces a body / full-text column or response field will
trip a clearly-named guard.
"""

from __future__ import annotations

import inspect

import pytest


def test_research_report_model_has_no_full_body_columns():
    from app.models.research_reports import ResearchReport

    columns = {c.name for c in ResearchReport.__table__.columns}
    forbidden = {
        "pdf_body",
        "pdf_text",
        "extracted_text",
        "full_text",
        "article_content",
        "article_body",
        "raw_payload_json",
        "raw_payload",
    }
    overlap = columns & forbidden
    assert not overlap, (
        f"ResearchReport must not store full bodies; remove columns: {overlap}"
    )


def test_citation_schema_has_no_full_body_fields():
    from app.schemas.research_reports import ResearchReportCitation

    fields = set(ResearchReportCitation.model_fields.keys())
    forbidden = {
        "pdf_body",
        "pdf_text",
        "extracted_text",
        "full_text",
        "article_content",
        "article_body",
        "raw_payload",
    }
    overlap = fields & forbidden
    assert not overlap, (
        f"Citation must not expose full bodies; remove fields: {overlap}"
    )


def test_payload_schema_rejects_full_text_exported_true():
    from app.schemas.research_reports import ResearchReportPayloadV1

    base = {
        "dedup_key": "x",
        "report_type": "equity_research",
        "source": "naver_research",
        "attribution": {
            "publisher": "naver_research",
            "full_text_exported": True,
            "pdf_body_exported": False,
        },
    }
    with pytest.raises(Exception):
        ResearchReportPayloadV1.model_validate(base)


def test_payload_schema_rejects_pdf_body_exported_true():
    from app.schemas.research_reports import ResearchReportPayloadV1

    base = {
        "dedup_key": "x",
        "report_type": "equity_research",
        "source": "naver_research",
        "attribution": {
            "publisher": "naver_research",
            "full_text_exported": False,
            "pdf_body_exported": True,
        },
    }
    with pytest.raises(Exception):
        ResearchReportPayloadV1.model_validate(base)


def test_query_service_module_does_not_reference_body_fields():
    """Cheap text grep: query_service source code must not access body-style names."""
    from app.services.research_reports import query_service

    source = inspect.getsource(query_service)
    forbidden_substrings = (
        "pdf_body",
        "pdf_text",
        "extracted_text",
        "full_text",
        "article_content",
    )
    for needle in forbidden_substrings:
        assert needle not in source, (
            f"{needle!r} reference found in query_service source — "
            "full body fields must not be touched by the read layer."
        )
