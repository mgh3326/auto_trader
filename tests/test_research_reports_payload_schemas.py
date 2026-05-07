"""Pydantic schemas for research-reports.v1 payload (ROB-140)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _sample_report() -> dict:
    return {
        "dedup_key": "naver-research-2026-05-07-AAPL-1",
        "report_type": "equity_research",
        "source": "naver_research",
        "source_report_id": "abc123",
        "title": "Apple Q2 Outlook",
        "category": "기업분석",
        "analyst": "김철수",
        "published_at_text": "2026-05-07 09:00",
        "summary_text": "단기 모멘텀이 약화되고 있으나 장기 펀더멘털은 견조함",
        "detail": {
            "url": "https://finance.naver.com/research/company_read.naver?nid=abc123",
            "title": "Apple Q2 Outlook",
            "subtitle": "단기 보수적, 장기 긍정적",
            "excerpt": "투자의견 매수, 목표가 220달러",
        },
        "pdf": {
            "url": "https://example.com/report.pdf",
            "filename": "report.pdf",
            "sha256": "f" * 64,
            "size_bytes": 1024,
            "page_count": 12,
            "text_length": 8000,
        },
        "symbol_candidates": [
            {"symbol": "AAPL", "market": "us", "source": "ticker_match"},
        ],
        "raw_text_policy": "metadata_only",
        "attribution": {
            "publisher": "naver_research",
            "copyright_notice": "© Naver",
            "full_text_exported": False,
            "pdf_body_exported": False,
        },
    }


def _sample_payload() -> dict:
    return {
        "research_report_ingestion_run": {
            "run_uuid": "run-abc-1",
            "payload_version": "research-reports.v1",
            "source": "naver_research",
            "started_at": "2026-05-07T00:00:00+00:00",
            "finished_at": "2026-05-07T00:01:00+00:00",
            "exported_at": "2026-05-07T00:01:05+00:00",
            "report_count": 1,
            "errors": [],
            "flags": [],
            "copyright_notice": "Reports remain property of their publishers",
        },
        "reports": [_sample_report()],
    }


class TestResearchReportPayloadSchemas:
    def test_full_payload_validates(self):
        from app.schemas.research_reports import ResearchReportIngestionRequest

        req = ResearchReportIngestionRequest.model_validate(_sample_payload())
        assert req.research_report_ingestion_run.run_uuid == "run-abc-1"
        assert len(req.reports) == 1
        assert req.reports[0].dedup_key == "naver-research-2026-05-07-AAPL-1"

    def test_rejects_payload_with_full_text_exported_true(self):
        from app.schemas.research_reports import ResearchReportIngestionRequest

        payload = _sample_payload()
        payload["reports"][0]["attribution"]["full_text_exported"] = True

        with pytest.raises(Exception) as exc_info:
            ResearchReportIngestionRequest.model_validate(payload)
        assert "full_text_exported" in str(exc_info.value).lower()

    def test_rejects_payload_with_pdf_body_exported_true(self):
        from app.schemas.research_reports import ResearchReportIngestionRequest

        payload = _sample_payload()
        payload["reports"][0]["attribution"]["pdf_body_exported"] = True

        with pytest.raises(Exception) as exc_info:
            ResearchReportIngestionRequest.model_validate(payload)
        assert "pdf_body_exported" in str(exc_info.value).lower()

    def test_rejects_payload_with_unknown_payload_version(self):
        from app.schemas.research_reports import ResearchReportIngestionRequest

        payload = _sample_payload()
        payload["research_report_ingestion_run"]["payload_version"] = "v2-unknown"

        with pytest.raises(Exception) as exc_info:
            ResearchReportIngestionRequest.model_validate(payload)
        assert "payload_version" in str(exc_info.value).lower()

    def test_summary_text_is_truncated_to_1000_chars(self):
        from app.schemas.research_reports import ResearchReportPayloadV1

        report = _sample_report()
        report["summary_text"] = "x" * 5000
        parsed = ResearchReportPayloadV1.model_validate(report)
        assert parsed.summary_text is not None
        assert len(parsed.summary_text) <= 1000

    def test_detail_excerpt_is_truncated_to_500_chars(self):
        from app.schemas.research_reports import ResearchReportPayloadV1

        report = _sample_report()
        report["detail"]["excerpt"] = "y" * 5000
        parsed = ResearchReportPayloadV1.model_validate(report)
        assert parsed.detail is not None
        assert parsed.detail.excerpt is not None
        assert len(parsed.detail.excerpt) <= 500

    def test_dedup_key_is_required(self):
        from app.schemas.research_reports import ResearchReportPayloadV1

        report = _sample_report()
        report.pop("dedup_key")
        with pytest.raises(ValidationError):
            ResearchReportPayloadV1.model_validate(report)

    def test_citation_schema_has_required_fields(self):
        from app.schemas.research_reports import ResearchReportCitation

        citation = ResearchReportCitation(
            source="naver_research",
            title="Apple Q2 Outlook",
            analyst="김철수",
            published_at_text="2026-05-07 09:00",
            category="기업분석",
            detail_url="https://finance.naver.com/research/x",
            pdf_url=None,
            excerpt="투자의견 매수",
            attribution_publisher="naver_research",
            attribution_copyright_notice="© Naver",
        )
        assert citation.title == "Apple Q2 Outlook"
        assert citation.detail_url is not None
