"""Pydantic schemas for research-reports.v1 payload + citation output (ROB-140).

Hard guardrails:
* Reject any report claiming full text or PDF body export.
* Truncate `summary_text` to 1000 chars and `detail.excerpt` to 500 chars.
* No raw PDF bytes or full extracted text fields are accepted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PAYLOAD_VERSION_V1 = "research-reports.v1"

SUMMARY_TEXT_MAX = 1000
DETAIL_EXCERPT_MAX = 500


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit]


class ResearchReportDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str | None = None
    title: str | None = None
    subtitle: str | None = None
    excerpt: str | None = None

    @field_validator("excerpt")
    @classmethod
    def _truncate_excerpt(cls, v: str | None) -> str | None:
        return _truncate(v, DETAIL_EXCERPT_MAX)


class ResearchReportPdf(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str | None = None
    filename: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    page_count: int | None = None
    text_length: int | None = None


class ResearchReportAttribution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    publisher: str | None = None
    copyright_notice: str | None = None
    full_text_exported: bool = False
    pdf_body_exported: bool = False


class ResearchReportSymbolCandidate(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    market: str | None = None
    source: str | None = None


class ResearchReportPayloadV1(BaseModel):
    """One report from a research-reports.v1 payload."""

    model_config = ConfigDict(extra="ignore")

    dedup_key: str
    report_type: str
    source: str
    source_report_id: str | None = None
    title: str | None = None
    category: str | None = None
    analyst: str | None = None
    published_at_text: str | None = None
    published_at: datetime | None = None
    summary_text: str | None = None

    detail: ResearchReportDetail | None = None
    pdf: ResearchReportPdf | None = None
    symbol_candidates: list[ResearchReportSymbolCandidate] = Field(
        default_factory=list
    )
    raw_text_policy: str | None = None
    attribution: ResearchReportAttribution = Field(
        default_factory=ResearchReportAttribution
    )

    @field_validator("summary_text")
    @classmethod
    def _truncate_summary(cls, v: str | None) -> str | None:
        return _truncate(v, SUMMARY_TEXT_MAX)

    @model_validator(mode="after")
    def _enforce_no_full_body(self) -> "ResearchReportPayloadV1":
        if self.attribution.full_text_exported:
            raise ValueError(
                "ROB-140 copyright guardrail: full_text_exported=true is rejected"
            )
        if self.attribution.pdf_body_exported:
            raise ValueError(
                "ROB-140 copyright guardrail: pdf_body_exported=true is rejected"
            )
        return self


class ResearchReportIngestionRunMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_uuid: str
    payload_version: Literal["research-reports.v1"]
    source: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exported_at: datetime | None = None
    report_count: int | None = None
    errors: list | dict | None = None
    flags: list | dict | None = None
    copyright_notice: str | None = None


class ResearchReportIngestionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    research_report_ingestion_run: ResearchReportIngestionRunMeta
    reports: list[ResearchReportPayloadV1] = Field(default_factory=list)


class ResearchReportIngestionResponse(BaseModel):
    run_uuid: str
    payload_version: str
    inserted_count: int
    skipped_count: int
    report_count: int


class ResearchReportCitation(BaseModel):
    """Compact citation for Research Session output. Never includes full body."""

    source: str
    title: str | None = None
    analyst: str | None = None
    published_at_text: str | None = None
    published_at: datetime | None = None
    category: str | None = None
    detail_url: str | None = None
    pdf_url: str | None = None
    excerpt: str | None = None
    symbol_candidates: list[ResearchReportSymbolCandidate] = Field(
        default_factory=list
    )
    attribution_publisher: str | None = None
    attribution_copyright_notice: str | None = None


class ResearchReportCitationListResponse(BaseModel):
    count: int
    citations: list[ResearchReportCitation] = Field(default_factory=list)
