# ROB-140 Research Reports Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a thin auto_trader ingest/read-layer slice for `research-reports.v1` payloads from news-ingestor, exposing compact metadata for Research Session evidence with citation output and copyright guardrails — no full PDF/report bodies stored or returned.

**Architecture:** Follow ROB-128 `market_events` foundation pattern (model + repository + service + read-only router). Two tables: `research_reports` (compact metadata, idempotent by `dedup_key`) and `research_report_ingestion_runs` (audit by `run_uuid`). One repository as sole writer. One query service that returns citation-shaped responses. One read-only router. One CLI for file-based payload import. Pydantic schemas reject any payload claiming full body export.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async, PostgreSQL with JSONB, Alembic, Pydantic v2, pytest + pytest-asyncio.

---

## File Structure

**Create:**
- `app/models/research_reports.py` — ORM models `ResearchReport`, `ResearchReportIngestionRun`
- `app/schemas/research_reports.py` — Pydantic models for payload v1 + citation output
- `app/services/research_reports/__init__.py`
- `app/services/research_reports/repository.py` — `ResearchReportsRepository` (sole writer)
- `app/services/research_reports/ingestion.py` — `ingest_research_reports_v1(...)`
- `app/services/research_reports/query_service.py` — `ResearchReportsQueryService` (read-only, returns citations)
- `app/routers/research_reports.py` — `GET /trading/api/research-reports/recent`
- `alembic/versions/b1c2d3e4_add_research_reports_tables.py` — migration
- `scripts/ingest_research_reports.py` — CLI to ingest payload JSON file (operator helper)
- `docs/runbooks/research-reports-integration.md` — runbook
- `tests/test_research_reports_payload_schemas.py`
- `tests/test_research_reports_repository.py`
- `tests/test_research_reports_ingestion.py`
- `tests/test_research_reports_query_service.py`
- `tests/test_research_reports_router.py`
- `tests/test_research_reports_copyright_guardrails.py`

**Modify:**
- `app/models/__init__.py` — re-export new ORM models
- `app/main.py` — wire up new router
- `CLAUDE.md` — add ROB-140 entry under "주요 워크플로우" / boundaries
- `alembic/versions/<latest>` — note new migration head

---

## Data Model Decisions

### `research_reports`

Stores compact metadata only. Forbidden columns (do not add): full pdf body text, full extracted text, raw article HTML.

| column | type | notes |
| --- | --- | --- |
| `id` | BigInt PK | |
| `dedup_key` | Text NOT NULL UNIQUE | stable hash from upstream |
| `report_type` | Text NOT NULL | e.g. `equity_research` |
| `source` | Text NOT NULL | e.g. `naver_research`, `kis_research` |
| `source_report_id` | Text NULL | upstream id when available |
| `title` | Text NULL | |
| `category` | Text NULL | upstream-provided category label |
| `analyst` | Text NULL | |
| `published_at_text` | Text NULL | upstream string preserved (timezone may be implicit) |
| `published_at` | TIMESTAMPTZ NULL | parsed best-effort, NULL if unparseable |
| `summary_text` | Text NULL | bounded ≤ 1000 chars |
| `detail_url` | Text NULL | |
| `detail_title` | Text NULL | |
| `detail_subtitle` | Text NULL | |
| `detail_excerpt` | Text NULL | bounded ≤ 500 chars |
| `pdf_url` | Text NULL | |
| `pdf_filename` | Text NULL | |
| `pdf_sha256` | Text NULL | |
| `pdf_size_bytes` | BigInt NULL | |
| `pdf_page_count` | Integer NULL | |
| `pdf_text_length` | Integer NULL | upstream-reported PDF text length, metadata only |
| `symbol_candidates` | JSONB NULL | list of {symbol, market, source} dicts |
| `raw_text_policy` | Text NULL | e.g. `metadata_only` |
| `attribution_publisher` | Text NULL | |
| `attribution_copyright_notice` | Text NULL | |
| `attribution_full_text_exported` | Boolean NOT NULL DEFAULT false | mirrored from upstream; **must be false to ingest** |
| `attribution_pdf_body_exported` | Boolean NOT NULL DEFAULT false | mirrored from upstream; **must be false to ingest** |
| `ingestion_run_id` | BigInt FK NULL | optional link to run row |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | onupdate now |

Indexes: `uq_research_reports_dedup_key` (unique), `ix_research_reports_published_at`, `ix_research_reports_source_published_at` (source, published_at desc), GIN on `symbol_candidates` for symbol filtering.

### `research_report_ingestion_runs`

| column | type | notes |
| --- | --- | --- |
| `id` | BigInt PK | |
| `run_uuid` | Text NOT NULL UNIQUE | from payload |
| `payload_version` | Text NOT NULL | expect `research-reports.v1` |
| `source` | Text NOT NULL | |
| `started_at` | TIMESTAMPTZ NULL | |
| `finished_at` | TIMESTAMPTZ NULL | |
| `exported_at` | TIMESTAMPTZ NULL | |
| `report_count` | Integer NOT NULL DEFAULT 0 | total in payload |
| `inserted_count` | Integer NOT NULL DEFAULT 0 | new rows |
| `skipped_count` | Integer NOT NULL DEFAULT 0 | duplicates by dedup_key |
| `errors` | JSONB NULL | upstream-reported errors |
| `flags` | JSONB NULL | upstream-reported flags |
| `copyright_notice` | Text NULL | top-level run copyright |
| `received_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Indexes: `uq_research_report_ingestion_runs_run_uuid` (unique).

---

### Task 1: Add ORM models for `research_reports` and `research_report_ingestion_runs`

**Files:**
- Create: `app/models/research_reports.py`
- Modify: `app/models/__init__.py`

- [ ] **Step 1: Create the models file**

Write `app/models/research_reports.py`:

```python
"""Research report metadata models (ROB-140).

Compact metadata only. Full PDF/report bodies and full extracted text MUST NOT be
stored. All writes must go through ResearchReportsRepository.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class ResearchReportIngestionRun(Base):
    __tablename__ = "research_report_ingestion_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_uuid", name="uq_research_report_ingestion_runs_run_uuid"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    payload_version: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    exported_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    report_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    inserted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    errors: Mapped[list | dict | None] = mapped_column(JSONB)
    flags: Mapped[list | dict | None] = mapped_column(JSONB)
    copyright_notice: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class ResearchReport(Base):
    __tablename__ = "research_reports"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_research_reports_dedup_key"),
        Index("ix_research_reports_published_at", "published_at"),
        Index(
            "ix_research_reports_source_published_at",
            "source",
            "published_at",
        ),
        Index(
            "ix_research_reports_symbol_candidates_gin",
            "symbol_candidates",
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_report_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    analyst: Mapped[str | None] = mapped_column(Text)
    published_at_text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    summary_text: Mapped[str | None] = mapped_column(Text)
    detail_url: Mapped[str | None] = mapped_column(Text)
    detail_title: Mapped[str | None] = mapped_column(Text)
    detail_subtitle: Mapped[str | None] = mapped_column(Text)
    detail_excerpt: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    pdf_filename: Mapped[str | None] = mapped_column(Text)
    pdf_sha256: Mapped[str | None] = mapped_column(Text)
    pdf_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    pdf_page_count: Mapped[int | None] = mapped_column(Integer)
    pdf_text_length: Mapped[int | None] = mapped_column(Integer)
    symbol_candidates: Mapped[list | None] = mapped_column(JSONB)
    raw_text_policy: Mapped[str | None] = mapped_column(Text)
    attribution_publisher: Mapped[str | None] = mapped_column(Text)
    attribution_copyright_notice: Mapped[str | None] = mapped_column(Text)
    attribution_full_text_exported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    attribution_pdf_body_exported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    ingestion_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_report_ingestion_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

- [ ] **Step 2: Re-export from `app/models/__init__.py`**

Add the import after the existing `from .research_run import ...` block:

```python
from .research_reports import ResearchReport, ResearchReportIngestionRun
```

Add `"ResearchReport"` and `"ResearchReportIngestionRun"` to `__all__`.

- [ ] **Step 3: Sanity import check**

Run: `uv run python -c "from app.models import ResearchReport, ResearchReportIngestionRun; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add app/models/research_reports.py app/models/__init__.py
git commit -m "feat(research-reports): add ORM models for ROB-140 research-reports.v1 metadata"
```

---

### Task 2: Add Alembic migration for the two new tables

**Files:**
- Create: `alembic/versions/b1c2d3e4_add_research_reports_tables.py`

- [ ] **Step 1: Find current migration head**

Run: `uv run alembic heads`
Note the current head; use it as `down_revision` below. (Expected: `a7e9c128` or whatever the most recent head is — substitute it into `down_revision` in the migration file.)

- [ ] **Step 2: Write the migration**

```python
"""add research_reports tables (ROB-140)

Revision ID: b1c2d3e4
Revises: <CURRENT_HEAD>
Create Date: 2026-05-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b1c2d3e4"
down_revision: str | Sequence[str] | None = "<CURRENT_HEAD>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_report_ingestion_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("run_uuid", sa.Text(), nullable=False),
        sa.Column("payload_version", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("exported_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("report_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("copyright_notice", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "run_uuid", name="uq_research_report_ingestion_runs_run_uuid"
        ),
    )

    op.create_table(
        "research_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("analyst", sa.Text(), nullable=True),
        sa.Column("published_at_text", sa.Text(), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("detail_url", sa.Text(), nullable=True),
        sa.Column("detail_title", sa.Text(), nullable=True),
        sa.Column("detail_subtitle", sa.Text(), nullable=True),
        sa.Column("detail_excerpt", sa.Text(), nullable=True),
        sa.Column("pdf_url", sa.Text(), nullable=True),
        sa.Column("pdf_filename", sa.Text(), nullable=True),
        sa.Column("pdf_sha256", sa.Text(), nullable=True),
        sa.Column("pdf_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("pdf_page_count", sa.Integer(), nullable=True),
        sa.Column("pdf_text_length", sa.Integer(), nullable=True),
        sa.Column(
            "symbol_candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("raw_text_policy", sa.Text(), nullable=True),
        sa.Column("attribution_publisher", sa.Text(), nullable=True),
        sa.Column("attribution_copyright_notice", sa.Text(), nullable=True),
        sa.Column(
            "attribution_full_text_exported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "attribution_pdf_body_exported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "ingestion_run_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "research_report_ingestion_runs.id", ondelete="SET NULL"
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("dedup_key", name="uq_research_reports_dedup_key"),
    )
    op.create_index(
        "ix_research_reports_published_at",
        "research_reports",
        ["published_at"],
    )
    op.create_index(
        "ix_research_reports_source_published_at",
        "research_reports",
        ["source", "published_at"],
    )
    op.create_index(
        "ix_research_reports_symbol_candidates_gin",
        "research_reports",
        ["symbol_candidates"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_reports_symbol_candidates_gin", table_name="research_reports"
    )
    op.drop_index(
        "ix_research_reports_source_published_at", table_name="research_reports"
    )
    op.drop_index("ix_research_reports_published_at", table_name="research_reports")
    op.drop_table("research_reports")
    op.drop_table("research_report_ingestion_runs")
```

Replace `<CURRENT_HEAD>` with the value from `alembic heads`.

- [ ] **Step 3: Verify migration file is valid**

Run: `uv run alembic heads`
Expected: `b1c2d3e4` listed (alongside or replacing the previous head — there should be exactly one head).

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/b1c2d3e4_add_research_reports_tables.py
git commit -m "feat(research-reports): add migration for research_reports tables (ROB-140)"
```

---

### Task 3: Pydantic schemas for payload v1 + citation output

**Files:**
- Create: `app/schemas/research_reports.py`
- Test: `tests/test_research_reports_payload_schemas.py`

- [ ] **Step 1: Write failing tests for schema validation**

Create `tests/test_research_reports_payload_schemas.py`:

```python
"""Pydantic schemas for research-reports.v1 payload (ROB-140)."""

from __future__ import annotations

import pytest


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
        with pytest.raises(Exception):
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_research_reports_payload_schemas.py -v`
Expected: FAIL with import errors (`ModuleNotFoundError: No module named 'app.schemas.research_reports'`).

- [ ] **Step 3: Write the schemas**

Create `app/schemas/research_reports.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_research_reports_payload_schemas.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/research_reports.py tests/test_research_reports_payload_schemas.py
git commit -m "feat(research-reports): add Pydantic schemas with copyright guardrails (ROB-140)"
```

---

### Task 4: Repository (sole writer) with idempotent upsert by `dedup_key`

**Files:**
- Create: `app/services/research_reports/__init__.py` (empty)
- Create: `app/services/research_reports/repository.py`
- Test: `tests/test_research_reports_repository.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_research_reports_repository.py`:

```python
"""ResearchReportsRepository tests (ROB-140).

Integration tests against a real Postgres test database (matching the conventions
in test_market_events_router.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture(autouse=True)
async def _clean_research_reports(db_session):
    from app.models.research_reports import (
        ResearchReport,
        ResearchReportIngestionRun,
    )

    await db_session.execute(delete(ResearchReport))
    await db_session.execute(delete(ResearchReportIngestionRun))
    await db_session.commit()
    yield


def _sample_report_dict(*, dedup_key: str = "abc-1") -> dict:
    return {
        "dedup_key": dedup_key,
        "report_type": "equity_research",
        "source": "naver_research",
        "source_report_id": "id-1",
        "title": "Apple Outlook",
        "category": "기업분석",
        "analyst": "김분석",
        "published_at_text": "2026-05-07 09:00",
        "published_at": datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
        "summary_text": "단기 약세, 장기 강세",
        "detail_url": "https://example.com/d/1",
        "detail_title": "Apple Outlook",
        "detail_subtitle": "long-term positive",
        "detail_excerpt": "buy, $220",
        "pdf_url": "https://example.com/x.pdf",
        "pdf_filename": "x.pdf",
        "pdf_sha256": "f" * 64,
        "pdf_size_bytes": 1024,
        "pdf_page_count": 10,
        "pdf_text_length": 8000,
        "symbol_candidates": [
            {"symbol": "AAPL", "market": "us", "source": "ticker_match"}
        ],
        "raw_text_policy": "metadata_only",
        "attribution_publisher": "naver_research",
        "attribution_copyright_notice": "© Naver",
        "attribution_full_text_exported": False,
        "attribution_pdf_body_exported": False,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_inserts_new_report(db_session):
    from app.models.research_reports import ResearchReport
    from app.services.research_reports.repository import ResearchReportsRepository

    repo = ResearchReportsRepository(db_session)
    inserted = await repo.upsert_report(_sample_report_dict(dedup_key="k-1"))
    await db_session.commit()
    assert inserted is True

    rows = (await db_session.execute(select(ResearchReport))).scalars().all()
    assert len(rows) == 1
    assert rows[0].dedup_key == "k-1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_skips_duplicate_dedup_key(db_session):
    from app.models.research_reports import ResearchReport
    from app.services.research_reports.repository import ResearchReportsRepository

    repo = ResearchReportsRepository(db_session)
    inserted_first = await repo.upsert_report(_sample_report_dict(dedup_key="k-2"))
    inserted_second = await repo.upsert_report(_sample_report_dict(dedup_key="k-2"))
    await db_session.commit()

    assert inserted_first is True
    assert inserted_second is False

    rows = (await db_session.execute(select(ResearchReport))).scalars().all()
    assert len(rows) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_run_inserts_then_returns_existing(db_session):
    from app.models.research_reports import ResearchReportIngestionRun
    from app.services.research_reports.repository import ResearchReportsRepository

    repo = ResearchReportsRepository(db_session)
    run = await repo.get_or_create_ingestion_run(
        run_uuid="run-1",
        payload_version="research-reports.v1",
        source="naver_research",
        started_at=None,
        finished_at=None,
        exported_at=None,
        report_count=2,
        errors=None,
        flags=None,
        copyright_notice="© test",
    )
    await db_session.commit()
    assert run.id is not None
    first_id = run.id

    again = await repo.get_or_create_ingestion_run(
        run_uuid="run-1",
        payload_version="research-reports.v1",
        source="naver_research",
        started_at=None,
        finished_at=None,
        exported_at=None,
        report_count=2,
        errors=None,
        flags=None,
        copyright_notice="© test",
    )
    await db_session.commit()
    assert again.id == first_id

    rows = (
        await db_session.execute(select(ResearchReportIngestionRun))
    ).scalars().all()
    assert len(rows) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_research_reports_repository.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.research_reports'`.

- [ ] **Step 3: Implement repository**

Create empty `app/services/research_reports/__init__.py`:

```python
"""Research reports service package (ROB-140)."""
```

Create `app/services/research_reports/repository.py`:

```python
"""Sole writer for research_reports / research_report_ingestion_runs (ROB-140).

Idempotency:
* Reports upsert on `dedup_key`. Returns True on insert, False on skip.
* Runs upsert on `run_uuid`. Returns the row.

Mutation policy: this is the ONLY module allowed to write these tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_reports import (
    ResearchReport,
    ResearchReportIngestionRun,
)


class ResearchReportsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def upsert_report(self, payload: dict[str, Any]) -> bool:
        dedup_key = payload["dedup_key"]
        existing = (
            await self.db.execute(
                select(ResearchReport).where(ResearchReport.dedup_key == dedup_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        row = ResearchReport(**payload)
        self.db.add(row)
        await self.db.flush()
        return True

    async def get_or_create_ingestion_run(
        self,
        *,
        run_uuid: str,
        payload_version: str,
        source: str,
        started_at: datetime | None,
        finished_at: datetime | None,
        exported_at: datetime | None,
        report_count: int | None,
        errors: list | dict | None,
        flags: list | dict | None,
        copyright_notice: str | None,
    ) -> ResearchReportIngestionRun:
        existing = (
            await self.db.execute(
                select(ResearchReportIngestionRun).where(
                    ResearchReportIngestionRun.run_uuid == run_uuid
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        row = ResearchReportIngestionRun(
            run_uuid=run_uuid,
            payload_version=payload_version,
            source=source,
            started_at=started_at,
            finished_at=finished_at,
            exported_at=exported_at,
            report_count=report_count or 0,
            errors=errors,
            flags=flags,
            copyright_notice=copyright_notice,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def update_run_counts(
        self,
        run: ResearchReportIngestionRun,
        *,
        inserted_count: int,
        skipped_count: int,
    ) -> None:
        run.inserted_count = inserted_count
        run.skipped_count = skipped_count
        await self.db.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_research_reports_repository.py -v -m integration`
Expected: 3 tests PASS.

If the integration DB is not available, mark this as a follow-up to run in CI. (For local development, ensure `docker compose up -d` is running.)

- [ ] **Step 5: Commit**

```bash
git add app/services/research_reports/__init__.py \
        app/services/research_reports/repository.py \
        tests/test_research_reports_repository.py
git commit -m "feat(research-reports): add repository with idempotent dedup_key upsert (ROB-140)"
```

---

### Task 5: Ingestion service that converts payload v1 → repository writes

**Files:**
- Create: `app/services/research_reports/ingestion.py`
- Test: `tests/test_research_reports_ingestion.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_research_reports_ingestion.py`:

```python
"""Ingestion service tests (ROB-140)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    from app.models.research_reports import (
        ResearchReport,
        ResearchReportIngestionRun,
    )

    await db_session.execute(delete(ResearchReport))
    await db_session.execute(delete(ResearchReportIngestionRun))
    await db_session.commit()
    yield


def _sample_payload(*, dedup_keys: list[str] | None = None) -> dict:
    keys = dedup_keys or ["k-A"]
    reports = []
    for k in keys:
        reports.append(
            {
                "dedup_key": k,
                "report_type": "equity_research",
                "source": "naver_research",
                "title": f"Title {k}",
                "summary_text": "summary",
                "detail": {
                    "url": f"https://example.com/{k}",
                    "excerpt": "excerpt",
                },
                "pdf": {
                    "url": f"https://example.com/{k}.pdf",
                    "sha256": "f" * 64,
                    "page_count": 10,
                    "text_length": 5000,
                },
                "symbol_candidates": [
                    {"symbol": "AAPL", "market": "us", "source": "ticker"}
                ],
                "raw_text_policy": "metadata_only",
                "attribution": {
                    "publisher": "naver_research",
                    "copyright_notice": "© Naver",
                    "full_text_exported": False,
                    "pdf_body_exported": False,
                },
            }
        )
    return {
        "research_report_ingestion_run": {
            "run_uuid": "run-1",
            "payload_version": "research-reports.v1",
            "source": "naver_research",
            "report_count": len(reports),
        },
        "reports": reports,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_inserts_reports_and_run(db_session):
    from app.models.research_reports import (
        ResearchReport,
        ResearchReportIngestionRun,
    )
    from app.schemas.research_reports import ResearchReportIngestionRequest
    from app.services.research_reports.ingestion import ingest_research_reports_v1

    req = ResearchReportIngestionRequest.model_validate(
        _sample_payload(dedup_keys=["k-A", "k-B"])
    )
    response = await ingest_research_reports_v1(db_session, req)
    await db_session.commit()

    assert response.inserted_count == 2
    assert response.skipped_count == 0

    reports = (await db_session.execute(select(ResearchReport))).scalars().all()
    assert {r.dedup_key for r in reports} == {"k-A", "k-B"}

    runs = (
        await db_session.execute(select(ResearchReportIngestionRun))
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].inserted_count == 2
    assert runs[0].skipped_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_is_idempotent_on_duplicate(db_session):
    from app.schemas.research_reports import ResearchReportIngestionRequest
    from app.services.research_reports.ingestion import ingest_research_reports_v1

    req = ResearchReportIngestionRequest.model_validate(
        _sample_payload(dedup_keys=["k-X"])
    )

    first = await ingest_research_reports_v1(db_session, req)
    await db_session.commit()
    second = await ingest_research_reports_v1(db_session, req)
    await db_session.commit()

    assert first.inserted_count == 1
    assert first.skipped_count == 0
    assert second.inserted_count == 0
    assert second.skipped_count == 1


@pytest.mark.unit
def test_ingest_request_rejects_full_text_exported():
    """Schema-level guard: ingestion never sees a payload with full body."""
    from app.schemas.research_reports import ResearchReportIngestionRequest

    payload = _sample_payload(dedup_keys=["k-bad"])
    payload["reports"][0]["attribution"]["full_text_exported"] = True

    with pytest.raises(Exception):
        ResearchReportIngestionRequest.model_validate(payload)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_research_reports_ingestion.py -v`
Expected: import errors for `app.services.research_reports.ingestion`.

- [ ] **Step 3: Implement ingestion**

Create `app/services/research_reports/ingestion.py`:

```python
"""Ingest research-reports.v1 payload into research_reports / runs (ROB-140).

Pure ingestion: no broker / order / watch / scheduling side effects.
Schema-validated payload only — full text / pdf body are rejected upstream.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.research_reports import (
    ResearchReportIngestionRequest,
    ResearchReportIngestionResponse,
    ResearchReportPayloadV1,
)
from app.services.research_reports.repository import ResearchReportsRepository

logger = logging.getLogger(__name__)


def _payload_to_row(
    report: ResearchReportPayloadV1, *, ingestion_run_id: int | None
) -> dict:
    detail = report.detail
    pdf = report.pdf
    return {
        "dedup_key": report.dedup_key,
        "report_type": report.report_type,
        "source": report.source,
        "source_report_id": report.source_report_id,
        "title": report.title,
        "category": report.category,
        "analyst": report.analyst,
        "published_at_text": report.published_at_text,
        "published_at": report.published_at,
        "summary_text": report.summary_text,
        "detail_url": detail.url if detail else None,
        "detail_title": detail.title if detail else None,
        "detail_subtitle": detail.subtitle if detail else None,
        "detail_excerpt": detail.excerpt if detail else None,
        "pdf_url": pdf.url if pdf else None,
        "pdf_filename": pdf.filename if pdf else None,
        "pdf_sha256": pdf.sha256 if pdf else None,
        "pdf_size_bytes": pdf.size_bytes if pdf else None,
        "pdf_page_count": pdf.page_count if pdf else None,
        "pdf_text_length": pdf.text_length if pdf else None,
        "symbol_candidates": [
            sc.model_dump() for sc in report.symbol_candidates
        ]
        if report.symbol_candidates
        else None,
        "raw_text_policy": report.raw_text_policy,
        "attribution_publisher": report.attribution.publisher,
        "attribution_copyright_notice": report.attribution.copyright_notice,
        "attribution_full_text_exported": report.attribution.full_text_exported,
        "attribution_pdf_body_exported": report.attribution.pdf_body_exported,
        "ingestion_run_id": ingestion_run_id,
    }


async def ingest_research_reports_v1(
    db: AsyncSession,
    request: ResearchReportIngestionRequest,
) -> ResearchReportIngestionResponse:
    repo = ResearchReportsRepository(db)
    run_meta = request.research_report_ingestion_run
    run = await repo.get_or_create_ingestion_run(
        run_uuid=run_meta.run_uuid,
        payload_version=run_meta.payload_version,
        source=run_meta.source,
        started_at=run_meta.started_at,
        finished_at=run_meta.finished_at,
        exported_at=run_meta.exported_at,
        report_count=run_meta.report_count,
        errors=run_meta.errors,
        flags=run_meta.flags,
        copyright_notice=run_meta.copyright_notice,
    )

    inserted = 0
    skipped = 0
    for report in request.reports:
        row_dict = _payload_to_row(report, ingestion_run_id=run.id)
        was_new = await repo.upsert_report(row_dict)
        if was_new:
            inserted += 1
        else:
            skipped += 1

    await repo.update_run_counts(
        run, inserted_count=inserted, skipped_count=skipped
    )

    return ResearchReportIngestionResponse(
        run_uuid=run.run_uuid,
        payload_version=run.payload_version,
        inserted_count=inserted,
        skipped_count=skipped,
        report_count=len(request.reports),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_research_reports_ingestion.py -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/research_reports/ingestion.py \
        tests/test_research_reports_ingestion.py
git commit -m "feat(research-reports): add ingestion service for research-reports.v1 (ROB-140)"
```

---

### Task 6: Read-layer query service returning citations

**Files:**
- Create: `app/services/research_reports/query_service.py`
- Test: `tests/test_research_reports_query_service.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_research_reports_query_service.py`:

```python
"""Query service tests (ROB-140)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    from app.models.research_reports import (
        ResearchReport,
        ResearchReportIngestionRun,
    )

    await db_session.execute(delete(ResearchReport))
    await db_session.execute(delete(ResearchReportIngestionRun))
    await db_session.commit()
    yield


async def _seed(db_session, dedup_key, *, source="naver_research", symbol="AAPL",
                published_at: datetime | None = None):
    from app.models.research_reports import ResearchReport

    row = ResearchReport(
        dedup_key=dedup_key,
        report_type="equity_research",
        source=source,
        title=f"Title {dedup_key}",
        summary_text="summary",
        detail_url=f"https://example.com/{dedup_key}",
        detail_excerpt="excerpt body",
        pdf_url=f"https://example.com/{dedup_key}.pdf",
        symbol_candidates=[{"symbol": symbol, "market": "us", "source": "t"}],
        attribution_publisher="naver_research",
        attribution_copyright_notice="© Naver",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=published_at or datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_filters_by_symbol(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    await _seed(db_session, "a-1", symbol="AAPL")
    await _seed(db_session, "a-2", symbol="MSFT")

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(symbol="AAPL")
    assert result.count == 1
    assert result.citations[0].title == "Title a-1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_filters_by_source(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    await _seed(db_session, "b-1", source="naver_research")
    await _seed(db_session, "b-2", source="kis_research")

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(source="kis_research")
    assert result.count == 1
    assert result.citations[0].source == "kis_research"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_filters_by_since(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    now = datetime.now(UTC)
    await _seed(
        db_session, "c-old", published_at=now - timedelta(days=30)
    )
    await _seed(
        db_session, "c-new", published_at=now - timedelta(days=1)
    )

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(since=now - timedelta(days=7))
    assert result.count == 1
    assert result.citations[0].title == "Title c-new"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_relevant_respects_limit(db_session):
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    for i in range(5):
        await _seed(db_session, f"d-{i}")

    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(limit=3)
    assert result.count == 3
    assert len(result.citations) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_citations_never_include_full_body_field(db_session):
    """Read layer must never return any 'pdf_body' / 'full_text' / 'article_content' fields."""
    from app.services.research_reports.query_service import (
        ResearchReportsQueryService,
    )

    await _seed(db_session, "e-1")
    svc = ResearchReportsQueryService(db_session)
    result = await svc.find_relevant(symbol="AAPL")
    assert result.count == 1
    serialized = result.citations[0].model_dump()
    forbidden = {"pdf_body", "full_text", "article_content", "raw_payload"}
    assert forbidden.isdisjoint(serialized.keys()), (
        f"Forbidden body fields present: {set(serialized.keys()) & forbidden}"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_research_reports_query_service.py -v`
Expected: import errors.

- [ ] **Step 3: Implement query service**

Create `app/services/research_reports/query_service.py`:

```python
"""Read-only query service returning citation-shaped results (ROB-140).

Never returns body/full-text fields.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_reports import ResearchReport
from app.schemas.research_reports import (
    ResearchReportCitation,
    ResearchReportCitationListResponse,
    ResearchReportSymbolCandidate,
)


def _row_to_citation(row: ResearchReport) -> ResearchReportCitation:
    candidates: list[ResearchReportSymbolCandidate] = []
    if row.symbol_candidates:
        for sc in row.symbol_candidates:
            try:
                candidates.append(
                    ResearchReportSymbolCandidate.model_validate(sc)
                )
            except Exception:
                continue
    excerpt = row.detail_excerpt or row.summary_text
    return ResearchReportCitation(
        source=row.source,
        title=row.title or row.detail_title,
        analyst=row.analyst,
        published_at_text=row.published_at_text,
        published_at=row.published_at,
        category=row.category,
        detail_url=row.detail_url,
        pdf_url=row.pdf_url,
        excerpt=excerpt,
        symbol_candidates=candidates,
        attribution_publisher=row.attribution_publisher,
        attribution_copyright_notice=row.attribution_copyright_notice,
    )


class ResearchReportsQueryService:
    DEFAULT_LIMIT = 20
    MAX_LIMIT = 100

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_relevant(
        self,
        *,
        symbol: str | None = None,
        query: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> ResearchReportCitationListResponse:
        effective_limit = min(
            self.MAX_LIMIT,
            max(1, limit or self.DEFAULT_LIMIT),
        )

        stmt = select(ResearchReport).order_by(
            ResearchReport.published_at.desc().nulls_last(),
            ResearchReport.id.desc(),
        )

        if source is not None:
            stmt = stmt.where(ResearchReport.source == source)
        if since is not None:
            stmt = stmt.where(ResearchReport.published_at >= since)
        if until is not None:
            stmt = stmt.where(ResearchReport.published_at <= until)

        if symbol is not None:
            stmt = stmt.where(
                ResearchReport.symbol_candidates.cast(JSONB).op("@>")(
                    [{"symbol": symbol}]
                )
            )

        if query is not None:
            like_q = f"%{query}%"
            stmt = stmt.where(
                ResearchReport.title.ilike(like_q)
                | ResearchReport.summary_text.ilike(like_q)
                | ResearchReport.detail_excerpt.ilike(like_q)
            )

        stmt = stmt.limit(effective_limit)
        rows = (await self.db.execute(stmt)).scalars().all()
        citations = [_row_to_citation(r) for r in rows]
        return ResearchReportCitationListResponse(
            count=len(citations), citations=citations
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_research_reports_query_service.py -v`
Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/research_reports/query_service.py \
        tests/test_research_reports_query_service.py
git commit -m "feat(research-reports): add citation-shaped read-layer query service (ROB-140)"
```

---

### Task 7: Copyright guardrails — model-level + read-layer tests

**Files:**
- Test: `tests/test_research_reports_copyright_guardrails.py`

This task adds an explicit lint-style test that documents what must NEVER appear in the system, so the guardrails survive future schema/router changes.

- [ ] **Step 1: Write the guardrail tests**

Create `tests/test_research_reports_copyright_guardrails.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_research_reports_copyright_guardrails.py -v`
Expected: 5 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_research_reports_copyright_guardrails.py
git commit -m "test(research-reports): copyright guardrails (ROB-140)"
```

---

### Task 8: Read-only router

**Files:**
- Create: `app/routers/research_reports.py`
- Modify: `app/main.py` — wire router
- Test: `tests/test_research_reports_router.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_research_reports_router.py`:

```python
"""Read-only research reports router (ROB-140)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    from app.models.research_reports import (
        ResearchReport,
        ResearchReportIngestionRun,
    )

    await db_session.execute(delete(ResearchReport))
    await db_session.execute(delete(ResearchReportIngestionRun))
    await db_session.commit()
    yield


def _app() -> FastAPI:
    from app.core.db import get_db
    from app.routers import research_reports as router_module
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=42)

    async def _override_get_db():
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


async def _seed(db_session, dedup_key="r-1", *, symbol="AAPL"):
    from app.models.research_reports import ResearchReport

    row = ResearchReport(
        dedup_key=dedup_key,
        report_type="equity_research",
        source="naver_research",
        title=f"Title {dedup_key}",
        summary_text="summary",
        detail_url=f"https://example.com/{dedup_key}",
        detail_excerpt="excerpt",
        pdf_url=f"https://example.com/{dedup_key}.pdf",
        symbol_candidates=[{"symbol": symbol, "market": "us", "source": "t"}],
        attribution_publisher="naver_research",
        attribution_copyright_notice="© Naver",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()


@pytest.mark.integration
def test_recent_endpoint_returns_empty(db_session):
    with TestClient(_app()) as client:
        resp = client.get("/trading/api/research-reports/recent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["citations"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recent_endpoint_filters_by_symbol(db_session):
    await _seed(db_session, "x-1", symbol="AAPL")
    await _seed(db_session, "x-2", symbol="MSFT")
    with TestClient(_app()) as client:
        resp = client.get(
            "/trading/api/research-reports/recent?symbol=AAPL"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["citations"][0]["title"] == "Title x-1"


@pytest.mark.integration
def test_recent_endpoint_unauthorized_without_override():
    from app.routers import research_reports as router_module

    app = FastAPI()
    app.include_router(router_module.router)
    with TestClient(app) as client:
        resp = client.get("/trading/api/research-reports/recent")
        assert resp.status_code in (401, 403)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recent_response_does_not_include_body_fields(db_session):
    await _seed(db_session, "y-1", symbol="AAPL")
    with TestClient(_app()) as client:
        resp = client.get(
            "/trading/api/research-reports/recent?symbol=AAPL"
        )
        body = resp.json()
        assert body["count"] == 1
        citation = body["citations"][0]
        for forbidden in (
            "pdf_body",
            "pdf_text",
            "full_text",
            "article_content",
            "article_body",
            "raw_payload",
        ):
            assert forbidden not in citation
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_research_reports_router.py -v`
Expected: import errors / 404.

- [ ] **Step 3: Implement router**

Create `app/routers/research_reports.py`:

```python
"""Read-only research reports router (ROB-140).

GET only. No mutation. Auth required (matches existing trading/api pattern).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.research_reports import ResearchReportCitationListResponse
from app.services.research_reports.query_service import (
    ResearchReportsQueryService,
)

router = APIRouter(prefix="/trading", tags=["research-reports"])


@router.get(
    "/api/research-reports/recent",
    response_model=ResearchReportCitationListResponse,
)
async def get_recent_research_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    symbol: str | None = None,
    query: str | None = None,
    source: str | None = None,
    since: Annotated[datetime | None, Query(description="ISO8601 inclusive lower bound on published_at")] = None,
    until: Annotated[datetime | None, Query(description="ISO8601 inclusive upper bound on published_at")] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> ResearchReportCitationListResponse:
    svc = ResearchReportsQueryService(db)
    try:
        return await svc.find_relevant(
            symbol=symbol,
            query=query,
            source=source,
            since=since,
            until=until,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
```

- [ ] **Step 4: Wire router in `app/main.py`**

In `app/main.py`, add `research_reports` to the import block (sorted alphabetically among existing router imports):

```python
    research_reports,
```

After `app.include_router(market_events.router)`, add:

```python
    app.include_router(research_reports.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_research_reports_router.py -v`
Expected: 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routers/research_reports.py app/main.py \
        tests/test_research_reports_router.py
git commit -m "feat(research-reports): add read-only /trading/api/research-reports/recent (ROB-140)"
```

---

### Task 9: Operator CLI to ingest a payload JSON file

**Files:**
- Create: `scripts/ingest_research_reports.py`

This is the boundary point: news-ingestor exports a payload file; operator imports it. Auto_trader does not call news-ingestor internals at runtime.

- [ ] **Step 1: Write the CLI**

```python
#!/usr/bin/env python3
"""Ingest a research-reports.v1 payload JSON file into auto_trader (ROB-140).

Usage:
    uv run python -m scripts.ingest_research_reports --file path/to/payload.json [--dry-run]

Reads the file, validates against ResearchReportIngestionRequest, and (unless dry-run)
upserts into research_reports / research_report_ingestion_runs. Prints a JSON summary.

Boundary: this is the only entry point that ingests news-ingestor output. Auto_trader
runtime never calls news-ingestor internals.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.monitoring.sentry import capture_exception, init_sentry
from app.schemas.research_reports import ResearchReportIngestionRequest
from app.services.research_reports.ingestion import ingest_research_reports_v1

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest a research-reports.v1 payload JSON file (ROB-140)."
    )
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="Path to a research-reports.v1 JSON payload file.",
    )
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    return parser.parse_args(argv)


async def main_async(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_sentry(service_name="ingest-research-reports")
    ns = parse_args(argv)

    if not ns.file.is_file():
        logger.error("file not found: %s", ns.file)
        return 1

    raw = json.loads(ns.file.read_text(encoding="utf-8"))
    try:
        request = ResearchReportIngestionRequest.model_validate(raw)
    except Exception as exc:
        logger.error("payload validation failed: %s", exc)
        capture_exception(exc, process="ingest_research_reports")
        return 2

    if ns.dry_run:
        summary = {
            "dry_run": True,
            "run_uuid": request.research_report_ingestion_run.run_uuid,
            "report_count": len(request.reports),
        }
        print(json.dumps(summary))
        return 0

    async with AsyncSessionLocal() as db:
        try:
            response = await ingest_research_reports_v1(db, request)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.error("ingest failed: %s", exc, exc_info=True)
            capture_exception(exc, process="ingest_research_reports")
            return 3

    print(json.dumps(response.model_dump()))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-import the script (does not run network/db)**

Run: `uv run python -c "import scripts.ingest_research_reports as m; print(m.parse_args.__doc__ or m.__doc__[:60])"`
Expected: prints first line of docstring.

- [ ] **Step 3: Commit**

```bash
git add scripts/ingest_research_reports.py
git commit -m "feat(research-reports): add operator CLI for payload-file ingest (ROB-140)"
```

---

### Task 10: Runbook + CLAUDE.md note

**Files:**
- Create: `docs/runbooks/research-reports-integration.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write runbook**

Create `docs/runbooks/research-reports-integration.md`:

```markdown
# Research Reports Integration (ROB-140)

> Thin ingest/read-layer slice for `research-reports.v1` payloads from news-ingestor.
> No broker mutation. No full PDF/report bodies stored or returned.

## What this is

Auto_trader pulls **compact metadata** for broker research reports (Naver Research,
KIS Research, etc.) from news-ingestor's `research-reports.v1` payload and exposes
them as **citations** for Research Session evidence.

## Tables

* `research_reports` — one row per report, idempotent by `dedup_key`.
* `research_report_ingestion_runs` — one row per upstream run, idempotent by
  `run_uuid`. Audit only.

All writes go through `ResearchReportsRepository`. No direct SQL writes.

## Boundary policy

* Auto_trader runtime does **not** call news-ingestor internals.
* Auto_trader receives a **payload file** (or in-band ingest endpoint, future) and
  validates it against `ResearchReportIngestionRequest` (Pydantic v2).
* Payloads with `attribution.full_text_exported=true` or
  `attribution.pdf_body_exported=true` are **rejected** at the schema layer.
* `summary_text` is truncated to 1000 chars; `detail.excerpt` to 500 chars.

## Operator CLI

```bash
# Validate without writing
uv run python -m scripts.ingest_research_reports \
  --file path/to/payload.json --dry-run

# Ingest
uv run python -m scripts.ingest_research_reports --file path/to/payload.json
```

Output is a JSON summary with `inserted_count` and `skipped_count`.

## Read API

```
GET /trading/api/research-reports/recent
  ?symbol=AAPL&source=naver_research&since=2026-04-01T00:00:00Z&limit=20
```

Response is `ResearchReportCitationListResponse` — citations only, never full body.

### Sample citation payload

```json
{
  "count": 1,
  "citations": [
    {
      "source": "naver_research",
      "title": "Apple Q2 Outlook",
      "analyst": "김철수",
      "published_at_text": "2026-05-07 09:00",
      "published_at": "2026-05-07T00:00:00+00:00",
      "category": "기업분석",
      "detail_url": "https://finance.naver.com/research/company_read.naver?nid=abc123",
      "pdf_url": "https://example.com/report.pdf",
      "excerpt": "투자의견 매수, 목표가 220달러",
      "symbol_candidates": [
        {"symbol": "AAPL", "market": "us", "source": "ticker_match"}
      ],
      "attribution_publisher": "naver_research",
      "attribution_copyright_notice": "© Naver"
    }
  ]
}
```

## Tests

```bash
uv run pytest tests/test_research_reports_payload_schemas.py -v
uv run pytest tests/test_research_reports_repository.py -v -m integration
uv run pytest tests/test_research_reports_ingestion.py -v -m integration
uv run pytest tests/test_research_reports_query_service.py -v -m integration
uv run pytest tests/test_research_reports_router.py -v -m integration
uv run pytest tests/test_research_reports_copyright_guardrails.py -v
```

## Migration

```bash
uv run alembic upgrade head    # applies b1c2d3e4_add_research_reports_tables
uv run alembic downgrade -1    # to roll back
```

## Safety

* No broker / order / watch / scheduling side effects.
* No full PDF bytes or full extracted PDF text accepted, stored, or returned.
* Read layer never reads body-style columns (none exist on the model).
* Citation responses include `attribution_publisher` and `attribution_copyright_notice`
  so downstream consumers can render attribution.

## Future follow-ups (out of scope)

* HTTP ingest endpoint (currently file-based via CLI).
* Research Session integration: wire `ResearchReportsQueryService` into the
  Research Session evidence gather step.
* Symbol normalization with `symbol_universe` services.
```

- [ ] **Step 2: Add ROB-140 entry to `CLAUDE.md`**

Insert after the `### Market Events Ingestion Foundation (ROB-128)` section, mirroring the existing format:

```markdown
### Research Reports Integration (ROB-140)

브로커 리서치 리포트 (Naver Research / KIS Research 등) `research-reports.v1` 페이로드의 thin ingest/read-layer 통합.

- **모델**: `app/models/research_reports.py` — `ResearchReport`, `ResearchReportIngestionRun`
- **스키마**: `app/schemas/research_reports.py` — `ResearchReportIngestionRequest`, `ResearchReportCitation`, copyright 가드
- **서비스**: `app/services/research_reports/` — `repository`, `ingestion`, `query_service`
- **라우터**: `app/routers/research_reports.py` — GET `/trading/api/research-reports/recent`
- **CLI**: `scripts/ingest_research_reports.py` — `--file path/to/payload.json [--dry-run]`
- **런북**: `docs/runbooks/research-reports-integration.md`

**안전 경계**: 풀 PDF 본문 / 전체 추출 텍스트는 스키마 단계에서 거부 (`full_text_exported`/`pdf_body_exported=true` 페이로드는 reject). `summary_text` 1000자, `detail.excerpt` 500자로 트렁케이트. 모든 DB 쓰기는 `ResearchReportsRepository` 경유. 브로커/주문/감시 mutation 없음.
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/research-reports-integration.md CLAUDE.md
git commit -m "docs(research-reports): runbook + CLAUDE.md entry (ROB-140)"
```

---

### Task 11: Run lint + full focused test sweep

- [ ] **Step 1: Run ruff**

Run: `uv run ruff check app/models/research_reports.py app/schemas/research_reports.py app/services/research_reports/ app/routers/research_reports.py scripts/ingest_research_reports.py tests/test_research_reports_*.py`
Expected: no errors. Fix any reported issues.

- [ ] **Step 2: Run ruff format**

Run: `uv run ruff format app/models/research_reports.py app/schemas/research_reports.py app/services/research_reports/ app/routers/research_reports.py scripts/ingest_research_reports.py tests/test_research_reports_*.py`
Expected: only the new files reformatted (or no changes).

- [ ] **Step 3: Run ty (typecheck)**

Run: `make typecheck` or `uv run ty check app/services/research_reports/ app/schemas/research_reports.py app/routers/research_reports.py app/models/research_reports.py`
Expected: no errors in new files.

- [ ] **Step 4: Run focused test sweep**

Run:
```bash
uv run pytest tests/test_research_reports_payload_schemas.py \
              tests/test_research_reports_copyright_guardrails.py -v
uv run pytest tests/test_research_reports_repository.py \
              tests/test_research_reports_ingestion.py \
              tests/test_research_reports_query_service.py \
              tests/test_research_reports_router.py -v -m integration
```
Expected: all PASS.

- [ ] **Step 5: Run wider regression sweep on adjacent areas**

Run: `uv run pytest tests/test_market_events_router.py tests/test_news_ingestor_bulk.py -v`
Expected: existing tests still pass (no regressions in adjacent foundations).

- [ ] **Step 6: If anything was changed in step 1-3, commit**

```bash
git add -p
git commit -m "chore(research-reports): lint/format/typecheck cleanup (ROB-140)"
```

---

### Task 12: Open PR

- [ ] **Step 1: Push branch**

Run: `git push -u origin feature/ROB-140-research-reports-integration`

- [ ] **Step 2: Open PR**

```bash
gh pr create --base main \
  --title "feat(research-reports): thin ingest/read-layer for research-reports.v1 (ROB-140)" \
  --body "$(cat <<'EOF'
## Summary

Implements ROB-140: thin auto_trader ingest/read-layer slice for broker research
report payloads from news-ingestor (`research-reports.v1`). Adds compact metadata
storage with copyright guardrails and a citation-shaped read layer for Research
Session evidence.

## What this PR does

- Adds two tables: `research_reports` (idempotent by `dedup_key`) and
  `research_report_ingestion_runs` (idempotent by `run_uuid`).
- Adds Pydantic schemas that **reject** payloads claiming
  `attribution.full_text_exported=true` or `pdf_body_exported=true`, and truncate
  `summary_text` (1000) / `detail.excerpt` (500).
- Adds `ResearchReportsRepository` as the sole writer.
- Adds `ResearchReportsQueryService.find_relevant(symbol|query|source|since|until|limit)`
  returning `ResearchReportCitation` items — never body fields.
- Adds read-only `GET /trading/api/research-reports/recent` route.
- Adds `scripts/ingest_research_reports.py` operator CLI for payload-file ingest.
- Adds runbook + CLAUDE.md entry.

## Migration

```bash
uv run alembic upgrade head     # applies b1c2d3e4_add_research_reports_tables
uv run alembic downgrade -1     # rollback
```

## Test commands

```bash
uv run pytest tests/test_research_reports_payload_schemas.py \
              tests/test_research_reports_copyright_guardrails.py -v
uv run pytest tests/test_research_reports_repository.py \
              tests/test_research_reports_ingestion.py \
              tests/test_research_reports_query_service.py \
              tests/test_research_reports_router.py -v -m integration
```

## Sample citation payload

```json
{
  "count": 1,
  "citations": [
    {
      "source": "naver_research",
      "title": "Apple Q2 Outlook",
      "analyst": "김철수",
      "published_at_text": "2026-05-07 09:00",
      "category": "기업분석",
      "detail_url": "https://finance.naver.com/research/company_read.naver?nid=abc123",
      "pdf_url": "https://example.com/report.pdf",
      "excerpt": "투자의견 매수, 목표가 220달러",
      "symbol_candidates": [
        {"symbol": "AAPL", "market": "us", "source": "ticker_match"}
      ],
      "attribution_publisher": "naver_research",
      "attribution_copyright_notice": "© Naver"
    }
  ]
}
```

## Safety boundaries

- No broker / order / watch / scheduler mutations.
- No full PDF bodies or extracted text accepted, stored, or returned.
- Read layer never references body-style fields (enforced by guardrail tests).
- All DB writes flow through `ResearchReportsRepository`.

## Out of scope (follow-ups)

- HTTP ingest endpoint (currently file-based via CLI).
- Wiring `ResearchReportsQueryService` into the Research Session evidence-gather
  step (kept thin per issue scope).
- Symbol normalization with `symbol_universe` services.

## Linear

ROB-140

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Capture PR URL** for the final report.

---

## Self-Review

**Spec coverage:**
- Internal ingest/read model — Tasks 1–2 (models + migration). ✅
- Persist compact metadata only — Task 1 (model fields exclude any body); Task 7 (guardrail test). ✅
- Read-layer/query helper by symbol/query/source/time window — Task 6. ✅
- Citation payload shape — Task 3 schema + Task 6/8 returned shape. ✅
- Tests proving no full PDF text/body — Tasks 3, 7, 8. ✅
- Idempotent by `dedup_key` — Tasks 4, 5. ✅
- No live broker orders / news-ingestor internals — Tasks 9 (file boundary), 10 (runbook). ✅
- Lint/tests pass — Task 11. ✅
- PR description with migration + tests + sample — Task 12. ✅

**Placeholder scan:** No TBD/TODO. Each step shows actual code. The only intentional placeholder is `<CURRENT_HEAD>` in Task 2 step 2 — explicitly resolved by the `alembic heads` output in step 1.

**Type consistency:** `ResearchReport`, `ResearchReportIngestionRun`, `ResearchReportPayloadV1`, `ResearchReportIngestionRequest`, `ResearchReportCitation`, `ResearchReportCitationListResponse`, `ResearchReportsRepository`, `ResearchReportsQueryService`, `ingest_research_reports_v1` — names used consistently across all tasks.
