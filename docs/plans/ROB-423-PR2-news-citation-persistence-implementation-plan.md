# ROB-423 PR2: 뉴스 citation 영속 + Hermes ingest + detail API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR1의 per-symbol 뉴스 evidence를 영속화한다 — 2개 신규 테이블(fetch_runs/citations), Hermes 합성 시점 citation 작성(matching/fail-open), detail API 노출, mock_preview citation 복사.

**Architecture:** auto_trader는 결정적 fetch_run audit를 쓰고, 판단성 citation은 Hermes가 `HermesCompositionResult.news_citations`로 보낸 annotation을 bundle의 news snapshot articles와 매칭해서만 작성한다(in-process LLM 0, no-internal-LLM 가드 준수). 매칭 실패 ref는 drop + 기록(날조 0). report_uuid는 `ingest_composition`의 `insert_report` 이후 확정되며 같은 트랜잭션에서 child row를 쓴다. detail API는 `InvestmentReportBundle`에 additive로 citation을 노출. mock_preview는 live citation을 report-level로 복사(재fetch/재판정 0).

**Tech Stack:** Python 3.13, SQLAlchemy async ORM, Alembic, Pydantic v2, FastAPI, pytest + pytest-asyncio, ruff.

> **선행 의존**: PR1(`b541eeda`..`6b789bad`, symbol_news_service seam + collector `articles[]`/`fetch_records[]` payload)이 머지/존재해야 한다. 본 플랜은 그 payload 계약을 소비한다.

---

## File Structure

| File | 역할 | 변경 |
|------|------|------|
| `app/models/investment_reports.py` | `InvestmentReportNewsFetchRun` + `InvestmentReportNewsCitation` ORM 추가 | Modify |
| `app/models/__init__.py` | 두 모델 import + `__all__` 등록 | Modify |
| `alembic/versions/20260603_rob423_news_citation_tables.py` | 신규 마이그레이션(down=현재 head) | Create |
| `app/schemas/hermes_composition.py` | `HermesNewsCitation` + `HermesCompositionResult.news_citations` additive | Modify |
| `app/services/investment_reports/news_persistence.py` | **신규** 순수 매칭 헬퍼 `build_news_persistence(...)` | Create |
| `app/services/investment_reports/investment_report_news_service.py` | **신규** `InvestmentReportNewsService`(persist/copy/list) | Create |
| `app/services/investment_stages/hermes_ingest.py` | `ingest_composition`에서 persist 호출 + 생성자 배선 | Modify |
| `app/services/investment_reports/repository.py` | `list_items_for_report_ordered_by_id` + citation 조회 메서드 | Modify |
| `app/services/investment_reports/query_service.py` | `get_bundle`에 `news_citations` 추가 | Modify |
| `app/schemas/investment_reports.py` | `InvestmentReportNewsCitationResponse` + `InvestmentReportBundle.news_citations` | Modify |
| `app/routers/investment_reports.py` | `_serialise_bundle`에 citation 매핑 | Modify |
| `app/services/investment_reports/mock_preview/runner.py` | mock citation 복사 호출 | Modify |
| `scripts/...smoke` | advisory-only smoke(기존 smoke 확장 or 신규) | Modify/Create |
| `tests/...` | 각 단위/통합 테스트 | Create/Modify |

> 내부 체크포인트(선택): Task 1~5(영속 코어) → Task 6~8(노출/복사/smoke). 한 PR로 가되 리뷰는 이 경계로 나눠도 됨.

---

## Task 1: 2개 ORM 모델 + 등록

**Files:**
- Modify: `app/models/investment_reports.py` (append)
- Modify: `app/models/__init__.py`
- Test: `tests/models/test_investment_report_news_models.py`

- [ ] **Step 1: Write the failing test**

```python
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

    assert InvestmentReportNewsFetchRun.__tablename__ == "investment_report_news_fetch_runs"
    assert InvestmentReportNewsCitation.__tablename__ == "investment_report_news_citations"
    assert InvestmentReportNewsFetchRun.__table__.schema == "review"
    assert InvestmentReportNewsCitation.__table__.schema == "review"
    # citation references fetch_run (nullable FK) and carries judgment fields
    cols = InvestmentReportNewsCitation.__table__.columns.keys()
    for c in ("report_uuid", "role", "decision_impact", "relevance", "canonical_url"):
        assert c in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/models/test_investment_report_news_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'InvestmentReportNewsFetchRun'`

- [ ] **Step 3: Append the two models**

`app/models/investment_reports.py` 파일 끝에 추가. 기존 import 블록은 `ARRAY, TIMESTAMP, BigInteger, CheckConstraint, ForeignKey, Index, Integer, Numeric, Text, UniqueConstraint` + `JSONB`, `PG_UUID`, `Mapped/mapped_column`, `func/text`, `Base`를 이미 포함(추가 import 불필요; `Boolean`만 신규 필요).

```python
# --- 파일 상단 import에 Boolean 추가 (기존 sqlalchemy import 블록) ---
# from sqlalchemy import (..., BigInteger, Boolean, CheckConstraint, ...)

# --- 파일 끝에 추가 (ROB-423 PR2) ---


class InvestmentReportNewsFetchRun(Base):
    """ROB-423 — per (report, symbol, provider) news fetch audit row.

    Append-only. ``report_uuid`` is a logical reference to
    ``review.investment_reports.report_uuid`` (no FK — items relate by
    integer ``report_id``, so we keep this membership-only). Never stores raw
    provider payloads (``raw_response_stored`` is an audit flag only).
    """

    __tablename__ = "investment_report_news_fetch_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_uuid", name="uq_investment_report_news_fetch_runs_run_uuid"
        ),
        CheckConstraint(
            "status IN ('ok','empty','unavailable','error')",
            name="ck_investment_report_news_fetch_runs_status",
        ),
        Index(
            "ix_investment_report_news_fetch_runs_report_uuid",
            "report_uuid",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    returned_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    used_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    freshness_policy: Mapped[str | None] = mapped_column(Text)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_response_stored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class InvestmentReportNewsCitation(Base):
    """ROB-423 — a news article the report actually cited (Hermes-marked).

    Only articles Hermes flagged as used land here (never fetched-but-unused).
    Judgment fields (``role``/``decision_impact``/``relevance``/
    ``selection_reason``/``confidence``) are Hermes-authored — auto_trader only
    validates + persists. Article fields are a snapshot copy (immutable audit).
    """

    __tablename__ = "investment_report_news_citations"
    __table_args__ = (
        UniqueConstraint(
            "citation_uuid", name="uq_investment_report_news_citations_citation_uuid"
        ),
        CheckConstraint(
            "relevance IN ('direct','related','market_context','crypto_context')",
            name="ck_investment_report_news_citations_relevance",
        ),
        CheckConstraint(
            "role IN ('catalyst','risk','confirmation','contradiction','neutral','noise')",
            name="ck_investment_report_news_citations_role",
        ),
        CheckConstraint(
            "decision_impact IN ('strengthen_buy','weaken_buy','strengthen_sell',"
            "'weaken_sell','hold_watch','no_action')",
            name="ck_investment_report_news_citations_decision_impact",
        ),
        Index(
            "ix_investment_report_news_citations_report_uuid",
            "report_uuid",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    citation_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    section_key: Mapped[str | None] = mapped_column(Text)
    fetch_run_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "review.investment_report_news_fetch_runs.id", ondelete="SET NULL"
        )
    )
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_article_id: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary_snapshot: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    relevance: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    decision_impact: Mapped[str] = mapped_column(Text, nullable=False)
    selection_reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric)
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

`app/models/__init__.py`의 investment_reports import 블록(16-22행)과 `__all__`(142-146행)에 두 모델 추가:

```python
from .investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentReportNewsCitation,
    InvestmentReportNewsFetchRun,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)
```
```python
    "InvestmentReport",
    "InvestmentReportItem",
    "InvestmentReportItemDecision",
    "InvestmentReportNewsCitation",
    "InvestmentReportNewsFetchRun",
    "InvestmentWatchAlert",
    "InvestmentWatchEvent",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/models/test_investment_report_news_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/models/investment_reports.py app/models/__init__.py tests/models/test_investment_report_news_models.py
git commit -m "feat(ROB-423): news fetch_run + citation ORM 모델(review)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Alembic 마이그레이션

**Files:**
- Create: `alembic/versions/20260603_rob423_news_citation_tables.py`

> **down_revision 재확인**: 구현 시작 시 `uv run alembic heads`로 단일 head 확인. 본 플랜 작성 시점 head = `20260602_rob412_main_merge`. main 전진 시 그 값으로 교체.

- [ ] **Step 1: Write the migration**

```python
# alembic/versions/20260603_rob423_news_citation_tables.py
"""rob-423 add investment_report_news_* tables (additive)

Revision ID: 20260603_rob423_news
Revises: 20260602_rob412_main_merge
Create Date: 2026-06-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260603_rob423_news"
down_revision: str | None = "20260602_rob412_main_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb_default(literal: str) -> sa.sql.elements.TextClause:
    return sa.text(f"'{literal}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "investment_report_news_fetch_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("run_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column(
            "returned_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "used_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("freshness_policy", sa.Text(), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "raw_response_stored",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ok','empty','unavailable','error')",
            name="ck_investment_report_news_fetch_runs_status",
        ),
        sa.UniqueConstraint(
            "run_uuid", name="uq_investment_report_news_fetch_runs_run_uuid"
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_report_news_fetch_runs_report_uuid",
        "investment_report_news_fetch_runs",
        ["report_uuid"],
        schema="review",
    )

    op.create_table(
        "investment_report_news_citations",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("citation_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_item_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("section_key", sa.Text(), nullable=True),
        sa.Column("fetch_run_id", sa.BigInteger(), nullable=True),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_article_id", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary_snapshot", sa.Text(), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("relevance", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("decision_impact", sa.Text(), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "relevance IN ('direct','related','market_context','crypto_context')",
            name="ck_investment_report_news_citations_relevance",
        ),
        sa.CheckConstraint(
            "role IN ('catalyst','risk','confirmation','contradiction','neutral','noise')",
            name="ck_investment_report_news_citations_role",
        ),
        sa.CheckConstraint(
            "decision_impact IN ('strengthen_buy','weaken_buy','strengthen_sell',"
            "'weaken_sell','hold_watch','no_action')",
            name="ck_investment_report_news_citations_decision_impact",
        ),
        sa.ForeignKeyConstraint(
            ["fetch_run_id"],
            ["review.investment_report_news_fetch_runs.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "citation_uuid",
            name="uq_investment_report_news_citations_citation_uuid",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_report_news_citations_report_uuid",
        "investment_report_news_citations",
        ["report_uuid"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investment_report_news_citations_report_uuid",
        table_name="investment_report_news_citations",
        schema="review",
    )
    op.drop_table("investment_report_news_citations", schema="review")
    op.drop_index(
        "ix_investment_report_news_fetch_runs_report_uuid",
        table_name="investment_report_news_fetch_runs",
        schema="review",
    )
    op.drop_table("investment_report_news_fetch_runs", schema="review")
```

- [ ] **Step 2: Sanity-check migration imports + model/migration column parity**

Run: `uv run python -c "import importlib.util, pathlib; p='alembic/versions/20260603_rob423_news_citation_tables.py'; spec=importlib.util.spec_from_file_location('m', p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.revision, m.down_revision)"`
Expected: prints `20260603_rob423_news 20260602_rob412_main_merge` (no import error)

- [ ] **Step 3: Confirm single head after adding migration**

Run: `uv run alembic heads`
Expected: single head `20260603_rob423_news (head)` (만약 다른 head가 보이면 main 전진 → down_revision을 실제 head로 교체 후 재확인)

> 참고: 테스트 DB는 timescaledb 확장 때문에 alembic upgrade가 막혀 있어, 통합 테스트는 `db_session` fixture의 `create_all`로 테이블을 만든다(ROB-407 선례). operator가 prod에서 별도 `alembic upgrade head` 실행.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/20260603_rob423_news_citation_tables.py
git commit -m "feat(ROB-423): news citation 테이블 마이그레이션(additive, review)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: HermesNewsCitation 스키마 + HermesCompositionResult.news_citations

**Files:**
- Modify: `app/schemas/hermes_composition.py`
- Test: `tests/schemas/test_hermes_news_citations.py`

`HermesCompositionResult.model_config = ConfigDict(extra="forbid")`이므로 default 있는 새 필드 추가는 기존 payload(미전송) back-compat.

- [ ] **Step 1: Write the failing test**

```python
# tests/schemas/test_hermes_news_citations.py
"""ROB-423 PR2 — HermesNewsCitation additive field."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.hermes_composition import (
    HermesCompositionResult,
    HermesNewsCitation,
)


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
    comp = HermesCompositionResult(**_base_composition())
    assert comp.news_citations == []  # legacy payload back-compat


@pytest.mark.unit
def test_news_citation_requires_ref_and_judgment() -> None:
    cit = HermesNewsCitation(
        canonical_url="https://x/1",
        symbol="AAPL",
        relevance="direct",
        role="catalyst",
        decision_impact="strengthen_buy",
        selection_reason="beat",
        client_item_key="ci-1",
    )
    comp = HermesCompositionResult(
        **_base_composition(news_citations=[cit.model_dump()])
    )
    assert comp.news_citations[0].symbol == "AAPL"
    assert comp.news_citations[0].role == "catalyst"


@pytest.mark.unit
def test_news_citation_rejects_bad_role() -> None:
    with pytest.raises(ValidationError):
        HermesNewsCitation(
            canonical_url="https://x/1",
            symbol="AAPL",
            relevance="direct",
            role="bogus",
            decision_impact="strengthen_buy",
        )


@pytest.mark.unit
def test_news_citation_requires_some_ref() -> None:
    with pytest.raises(ValidationError):
        HermesNewsCitation(
            symbol="AAPL",
            relevance="direct",
            role="catalyst",
            decision_impact="strengthen_buy",
        )  # neither external_article_id nor canonical_url
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/schemas/test_hermes_news_citations.py -v`
Expected: FAIL with `ImportError: cannot import name 'HermesNewsCitation'`

- [ ] **Step 3: Add the schema + field**

`app/schemas/hermes_composition.py`에 추가. (`Literal`, `Decimal`, `UUID`, `BaseModel`, `ConfigDict`, `Field`, `model_validator`는 파일에 이미 import됨 — `model_validator`가 없으면 `from pydantic import model_validator` 추가.)

```python
# --- HermesCompositionResult 클래스 정의 위에 추가 ---

NewsRelevanceLiteral = Literal["direct", "related", "market_context", "crypto_context"]
NewsRoleLiteral = Literal[
    "catalyst", "risk", "confirmation", "contradiction", "neutral", "noise"
]
NewsDecisionImpactLiteral = Literal[
    "strengthen_buy", "weaken_buy", "strengthen_sell", "weaken_sell",
    "hold_watch", "no_action",
]


class HermesNewsCitation(BaseModel):
    """A news article Hermes actually used, with its judgment annotations.

    auto_trader matches this against the bundle's news snapshot articles by
    ``external_article_id`` (preferred) or ``canonical_url`` and persists only
    matches. At least one of the two refs is required. ``client_item_key`` links
    the citation to a specific report item (the same key used in the composed
    ``IngestReportItem``); ``section_key`` is for report-level citations.
    """

    model_config = ConfigDict(extra="forbid")

    external_article_id: str | None = None
    canonical_url: str | None = None
    symbol: str = Field(min_length=1)
    relevance: NewsRelevanceLiteral
    role: NewsRoleLiteral
    decision_impact: NewsDecisionImpactLiteral
    selection_reason: str | None = None
    confidence: Decimal | None = None
    client_item_key: str | None = None
    section_key: str | None = None

    @model_validator(mode="after")
    def _require_ref(self) -> HermesNewsCitation:
        if not self.external_article_id and not self.canonical_url:
            raise ValueError(
                "news citation needs external_article_id or canonical_url"
            )
        return self
```

`HermesCompositionResult`에 필드 추가(`dimension_report_uuids` 다음):

```python
    # ROB-423 — news articles Hermes used as overlay evidence. Empty for legacy
    # composition (byte-identical path). Matched against the bundle's news
    # snapshot on ingest; unmatched refs are dropped + recorded (fail-open).
    news_citations: list[HermesNewsCitation] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/schemas/test_hermes_news_citations.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/schemas/hermes_composition.py tests/schemas/test_hermes_news_citations.py
git commit -m "feat(ROB-423): HermesNewsCitation + composition.news_citations additive

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 순수 매칭 헬퍼 build_news_persistence

**Files:**
- Create: `app/services/investment_reports/news_persistence.py`
- Test: `tests/services/investment_reports/test_news_persistence.py`

DB I/O 없이 테스트 가능한 순수 함수: news snapshot payloads + Hermes citations + item-uuid 맵 → 작성할 fetch_run/citation row dict들 + unmatched 목록.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_reports/test_news_persistence.py -v`
Expected: FAIL with `ModuleNotFoundError: ...news_persistence`

- [ ] **Step 3: Implement the pure helper**

```python
# app/services/investment_reports/news_persistence.py
"""ROB-423 PR2 — pure news-citation persistence planner (no DB I/O).

Matches Hermes-supplied news citations against the bundle's news snapshot
articles and produces the fetch_run + citation rows to insert. Unmatched refs
are dropped and reported (fail-open, no fabrication). Kept pure so the matching
logic is unit-testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.schemas.hermes_composition import HermesNewsCitation

_SUMMARY_MAX = 1000


@dataclass(frozen=True)
class NewsPersistencePlan:
    fetch_runs: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:_SUMMARY_MAX]


def build_news_persistence(
    *,
    news_payloads: list[dict[str, Any]],
    citations: list[HermesNewsCitation],
    item_uuid_by_client_key: dict[str, UUID],
    instrument_type: str,
) -> NewsPersistencePlan:
    by_external: dict[str, dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    market = "us"
    for payload in news_payloads:
        market = payload.get("market") or market
        for art in payload.get("articles", []):
            ext = art.get("external_article_id")
            url = art.get("url")
            if ext:
                by_external.setdefault(ext, art)
            if url:
                by_url.setdefault(url, art)

    # per (symbol, provider) used_count tally
    used_by_key: dict[tuple[str, str], int] = {}

    citation_rows: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for cit in citations:
        art = None
        ref = cit.external_article_id or cit.canonical_url or ""
        if cit.external_article_id:
            art = by_external.get(cit.external_article_id)
        if art is None and cit.canonical_url:
            art = by_url.get(cit.canonical_url)
        if art is None:
            unmatched.append(ref)
            continue

        sym = art.get("symbol") or cit.symbol
        provider = art.get("provider") or "unknown"
        used_by_key[(sym, provider)] = used_by_key.get((sym, provider), 0) + 1

        item_uuid = (
            item_uuid_by_client_key.get(cit.client_item_key)
            if cit.client_item_key
            else None
        )
        citation_rows.append(
            {
                "report_item_uuid": item_uuid,
                "section_key": cit.section_key,
                "market": market,
                "symbol": sym,
                "provider": provider,
                "external_article_id": art.get("external_article_id"),
                "canonical_url": art.get("url") or cit.canonical_url or "",
                "source_name": art.get("source"),
                "title": art.get("title") or "",
                "summary_snapshot": _truncate(art.get("summary")),
                "published_at": _parse_dt(art.get("published_at")),
                "relevance": cit.relevance,
                "role": cit.role,
                "decision_impact": cit.decision_impact,
                "selection_reason": cit.selection_reason,
                "confidence": cit.confidence,
                "_fetch_key": (sym, provider),  # internal: link to fetch_run
            }
        )

    fetch_runs: list[dict[str, Any]] = []
    for payload in news_payloads:
        for rec in payload.get("fetch_records", []):
            sym = rec.get("symbol") or ""
            provider = rec.get("provider") or "unknown"
            fetch_runs.append(
                {
                    "market": market,
                    "symbol": sym,
                    "instrument_type": instrument_type,
                    "provider": provider,
                    "requested_limit": int(rec.get("requested_limit") or 0),
                    "returned_count": int(rec.get("returned_count") or 0),
                    "used_count": used_by_key.get((sym, provider), 0),
                    "status": rec.get("status") or "ok",
                    "error_code": rec.get("error_code"),
                    "_fetch_key": (sym, provider),  # internal: citation linkage
                }
            )

    return NewsPersistencePlan(
        fetch_runs=fetch_runs, citations=citation_rows, unmatched=unmatched
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/investment_reports/test_news_persistence.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/news_persistence.py tests/services/investment_reports/test_news_persistence.py
git commit -m "feat(ROB-423): 순수 news citation 매칭 헬퍼(build_news_persistence)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: InvestmentReportNewsService.persist + hermes_ingest 배선

**Files:**
- Create: `app/services/investment_reports/investment_report_news_service.py`
- Modify: `app/services/investment_reports/repository.py` (citation/fetch_run insert + id-ordered item 조회 + report별 조회)
- Modify: `app/services/investment_stages/hermes_ingest.py` (persist 호출 + 생성자)
- Test: `tests/services/investment_stages/test_hermes_news_citation_ingest.py` (db_session integration)

> **⚠️ 아이템 순서 함정**: 같은 ingest 트랜잭션의 item들은 `created_at`이 동일(`now()`=transaction time)하므로 `list_items_for_report`의 `created_at.asc()`는 비결정적. per-item 링크는 **`id.asc()` 정렬**(삽입 순 == composition.items 순)로 재조회해야 안전.

- [ ] **Step 1: Add repository methods**

`app/services/investment_reports/repository.py`에 추가(파일에 `sa`, `InvestmentReportItem` 이미 import; 신규 모델 import 추가):

```python
# import 블록에 추가
from app.models.investment_reports import (
    InvestmentReportNewsCitation,
    InvestmentReportNewsFetchRun,
)

# --- 클래스 메서드로 추가 ---

    async def list_items_for_report_ordered_by_id(
        self, report_id: int
    ) -> list[InvestmentReportItem]:
        """Insertion-order items (id.asc()). Use for composition-index mapping —
        ``created_at`` ties (single-transaction inserts share ``now()``) make
        the created_at-ordered query non-deterministic for this purpose."""
        result = await self._session.scalars(
            sa.select(InvestmentReportItem)
            .where(InvestmentReportItem.report_id == report_id)
            .order_by(InvestmentReportItem.id.asc())
        )
        return list(result.all())

    async def insert_news_fetch_run(
        self, **fields: Any
    ) -> InvestmentReportNewsFetchRun:
        row = InvestmentReportNewsFetchRun(**fields)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def insert_news_citation(
        self, **fields: Any
    ) -> InvestmentReportNewsCitation:
        row = InvestmentReportNewsCitation(**fields)
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_news_citations_for_report(
        self, report_uuid: UUID
    ) -> list[InvestmentReportNewsCitation]:
        result = await self._session.scalars(
            sa.select(InvestmentReportNewsCitation)
            .where(InvestmentReportNewsCitation.report_uuid == report_uuid)
            .order_by(InvestmentReportNewsCitation.id.asc())
        )
        return list(result.all())
```

- [ ] **Step 2: Implement the service**

```python
# app/services/investment_reports/investment_report_news_service.py
"""ROB-423 PR2 — persist Hermes-marked news citations + fetch_run audit.

auto_trader-side: validation + persistence only (no LLM, no fetch). Reads the
bundle's news snapshot ``articles``/``fetch_records`` (written by the PR1
collector), matches Hermes ``news_citations`` against them, and writes fetch_run
+ citation rows keyed by ``report_uuid``. Unmatched refs are dropped + recorded
in the report's ``unavailable_sources`` metadata (fail-open).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.models.investment_reports import InvestmentReport
from app.schemas.hermes_composition import HermesCompositionResult
from app.services.investment_reports.news_persistence import build_news_persistence
from app.services.investment_reports.repository import InvestmentReportsRepository

_INSTRUMENT_BY_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


class InvestmentReportNewsService:
    def __init__(self, repo: InvestmentReportsRepository) -> None:
        self._repo = repo

    async def persist_from_composition(
        self,
        *,
        report: InvestmentReport,
        composition: HermesCompositionResult,
        news_payloads: list[dict[str, Any]],
    ) -> int:
        """Returns the number of citations written. Never raises on matching
        gaps (fail-open). Call inside the ingest transaction after the report +
        items are persisted."""
        if not composition.news_citations:
            return 0

        # client_item_key -> item_uuid via insertion-order (id.asc()) zip.
        persisted = await self._repo.list_items_for_report_ordered_by_id(report.id)
        item_uuid_by_client_key: dict[str, UUID] = {}
        if len(persisted) == len(composition.items):
            for comp_item, row in zip(composition.items, persisted, strict=True):
                item_uuid_by_client_key[comp_item.client_item_key] = row.item_uuid

        instrument_type = _INSTRUMENT_BY_MARKET.get(report.market, "equity_us")
        plan = build_news_persistence(
            news_payloads=news_payloads,
            citations=composition.news_citations,
            item_uuid_by_client_key=item_uuid_by_client_key,
            instrument_type=instrument_type,
        )

        # fetch_runs first (citations FK them by (symbol, provider)).
        fetch_run_id_by_key: dict[tuple[str, str], int] = {}
        for run in plan.fetch_runs:
            key = run.pop("_fetch_key")
            row = await self._repo.insert_news_fetch_run(
                report_uuid=report.report_uuid,
                fetched_at=_utcnow(),
                **run,
            )
            fetch_run_id_by_key[key] = row.id

        written = 0
        for cit in plan.citations:
            key = cit.pop("_fetch_key")
            await self._repo.insert_news_citation(
                report_uuid=report.report_uuid,
                fetch_run_id=fetch_run_id_by_key.get(key),
                fetched_at=_utcnow(),
                **cit,
            )
            written += 1

        if plan.unmatched:
            # record on the report's metadata (fail-open, no fabrication).
            await self._repo.merge_report_unavailable_sources(
                report.id,
                {"news_citations_unmatched": plan.unmatched},
            )
        return written


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)
```

> `merge_report_unavailable_sources`가 repository에 없으면, 간단히 `update_report`로 `unavailable_sources` JSONB를 병합하는 메서드를 추가하거나, 이 한 줄을 `report.metadata` 병합으로 대체한다(아래 Step 3 통합 테스트에서 검증). 최소 구현: repository에
> ```python
>     async def merge_report_unavailable_sources(self, report_id, extra):
>         row = await self._session.get(InvestmentReport, report_id)
>         if row is None:
>             return
>         merged = {**(row.unavailable_sources or {}), **extra}
>         row.unavailable_sources = merged
>         await self._session.flush()
> ```
> (`InvestmentReport.unavailable_sources` 컬럼이 ROB-269에서 존재. 없으면 `metadata`로 대체.)

- [ ] **Step 3: Wire into hermes_ingest + write the integration test**

`app/services/investment_stages/hermes_ingest.py`:
- 생성자에 `InvestmentReportNewsService` 구성(기존 `self._ingestion`이 보유한 repo 재사용; 없으면 `InvestmentReportsRepository(session)` 생성).
- `ingest_composition`에서 `report = await self._ingestion.ingest(ingest_request)` 직후, `_maybe_finalize_stage_run` 이전에:

```python
        report = await self._ingestion.ingest(ingest_request)

        # ROB-423 — persist Hermes-marked news citations from the bundle's news
        # snapshot. Fail-open: matching gaps never block report creation.
        news_payloads = [
            (snap.payload_json or {})
            for _item, snap in await self._snapshots.list_bundle_items_with_snapshots(
                bundle.id
            )
            if snap.snapshot_kind == "news"
        ]
        await self._news_service.persist_from_composition(
            report=report, composition=composition, news_payloads=news_payloads
        )

        await self._maybe_finalize_stage_run(composition.metadata)
        return report
```

통합 테스트(db_session, create_all로 테이블 생성):

```python
# tests/services/investment_stages/test_hermes_news_citation_ingest.py
"""ROB-423 PR2 — Hermes composition news_citations → persisted rows (fail-open)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_composition_news_citations_persisted_and_unmatched_dropped(
    db_session,
):
    # Arrange: a bundle with a news snapshot (articles + fetch_records), then a
    # Hermes composition citing one real article + one bogus ref.
    # ... build bundle + news snapshot via the snapshots repo/collector payload ...
    # ... call HermesCompositionIngestService.ingest_composition(request) ...
    # Assert:
    #   - 1 citation row written (real ref), role/decision_impact preserved
    #   - bogus ref dropped; report.unavailable_sources['news_citations_unmatched']
    #   - fetch_run rows: used_count==1 for the cited symbol, 0 otherwise
    #   - report created successfully (fail-open)
    ...


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_news_citations_is_noop(db_session):
    # composition with news_citations=[] → 0 citation rows, report still created.
    ...
```

> 통합 테스트의 bundle/snapshot 셋업은 기존 `tests/services/investment_stages/` Hermes ingest 테스트의 fixture 패턴을 따른다(같은 디렉터리에서 `ingest_composition` 호출 예시를 복사). `investment_reports_cleanup_lock`는 xdist TRUNCATE/deadlock flake 회피 필수(ROB-405 선례).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/investment_stages/test_hermes_news_citation_ingest.py tests/services/investment_reports/test_news_persistence.py -v`
Expected: PASS

- [ ] **Step 5: no-internal-LLM 가드 확인 (news_service가 investment_stages/action_report 트리에서 import돼도 LLM 0)**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/investment_reports/investment_report_news_service.py app/services/investment_reports/repository.py app/services/investment_stages/hermes_ingest.py tests/services/investment_stages/test_hermes_news_citation_ingest.py
git commit -m "feat(ROB-423): Hermes 합성 시 news citation 영속(fail-open, evidence-gated)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Detail API 노출

**Files:**
- Modify: `app/schemas/investment_reports.py` (`InvestmentReportNewsCitationResponse` + bundle 필드)
- Modify: `app/services/investment_reports/query_service.py` (`get_bundle`)
- Modify: `app/routers/investment_reports.py` (`_serialise_bundle`)
- Test: `tests/routers/test_investment_reports_news_citations.py` 또는 query_service 단위 테스트

- [ ] **Step 1: Write the failing test**

```python
# tests/services/investment_reports/test_bundle_news_citations.py
"""ROB-423 PR2 — detail bundle exposes news_citations."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_bundle_includes_news_citations(db_session):
    # Arrange: persist a report + one news citation row (via repo).
    # Act: bundle = await query_service.get_bundle(report_uuid)
    # Assert: bundle["news_citations"] has the row with title/source/url/role/...
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_reports/test_bundle_news_citations.py -v`
Expected: FAIL (KeyError: 'news_citations')

- [ ] **Step 3: Add schema + wire bundle**

`app/schemas/investment_reports.py`에 추가:

```python
class InvestmentReportNewsCitationResponse(BaseModel):
    """ROB-423 — one cited news article on a report (read-side)."""

    citation_uuid: UUID
    report_item_uuid: UUID | None = None
    section_key: str | None = None
    market: str
    symbol: str
    provider: str
    external_article_id: str | None = None
    canonical_url: str
    source_name: str | None = None
    title: str
    summary_snapshot: str | None = None
    published_at: datetime | None = None
    fetched_at: datetime
    relevance: str
    role: str
    decision_impact: str
    selection_reason: str | None = None
    confidence: Decimal | None = None

    model_config = ConfigDict(from_attributes=True)
```

`InvestmentReportBundle`에 필드 추가(action_packet 다음):

```python
    # ROB-423 — additive news citations (articles the report actually used).
    # Empty for reports with no Hermes-marked news.
    news_citations: list[InvestmentReportNewsCitationResponse] = Field(
        default_factory=list
    )
```

`app/services/investment_reports/query_service.py`의 `get_bundle` 반환 dict에 추가:

```python
    citations = await self._repo.list_news_citations_for_report(report.report_uuid)
    # ... 기존 return dict에 추가:
        "news_citations": citations,
```

`app/routers/investment_reports.py`의 `_serialise_bundle` 반환 `InvestmentReportBundle(...)`에 추가:

```python
        news_citations=[
            InvestmentReportNewsCitationResponse.model_validate(c)
            for c in bundle["news_citations"]
        ],
```
(`InvestmentReportNewsCitationResponse` import 추가.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/investment_reports/test_bundle_news_citations.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/schemas/investment_reports.py app/services/investment_reports/query_service.py app/routers/investment_reports.py tests/services/investment_reports/test_bundle_news_citations.py
git commit -m "feat(ROB-423): detail API에 news_citations 노출(additive)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: mock_preview citation 복사

**Files:**
- Modify: `app/services/investment_reports/investment_report_news_service.py` (`copy_for_mock`)
- Modify: `app/services/investment_reports/mock_preview/runner.py`
- Test: `tests/services/investment_reports/mock_preview/test_mock_news_citation_copy.py`

mock은 live citation을 report-level로 복사(재fetch/재판정 0). per-item 링크는 NULL(robust; live↔mock item 매핑은 후속).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/investment_reports/mock_preview/test_mock_news_citation_copy.py
"""ROB-423 PR2 — mock_preview copies live news citations (no re-fetch)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mock_preview_copies_live_citations(db_session):
    # Arrange: a live report with 2 news citations.
    # Act: run MockPreviewReportRunner.run(live_report_uuid=...)
    # Assert: mock report has 2 citations copied (same title/url/role/symbol),
    #         report_item_uuid is NULL on the copies, no new fetch occurred.
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_reports/mock_preview/test_mock_news_citation_copy.py -v`
Expected: FAIL

- [ ] **Step 3: Add copy_for_mock + call it in the runner**

`InvestmentReportNewsService`에 추가:

```python
    async def copy_for_mock(
        self, *, live_report_uuid: UUID, mock_report: InvestmentReport
    ) -> int:
        """Copy a live report's news citations onto the mock report (report-level,
        report_item_uuid=NULL). No re-fetch, no re-judgment. Returns count."""
        live_citations = await self._repo.list_news_citations_for_report(
            live_report_uuid
        )
        count = 0
        for c in live_citations:
            await self._repo.insert_news_citation(
                report_uuid=mock_report.report_uuid,
                report_item_uuid=None,
                section_key=c.section_key,
                fetch_run_id=None,
                market=c.market,
                symbol=c.symbol,
                provider=c.provider,
                external_article_id=c.external_article_id,
                canonical_url=c.canonical_url,
                source_name=c.source_name,
                title=c.title,
                summary_snapshot=c.summary_snapshot,
                published_at=c.published_at,
                fetched_at=c.fetched_at,
                relevance=c.relevance,
                role=c.role,
                decision_impact=c.decision_impact,
                selection_reason=c.selection_reason,
                confidence=c.confidence,
                metadata_json={"copied_from_report_uuid": str(live_report_uuid)},
            )
            count += 1
        return count
```

`app/services/investment_reports/mock_preview/runner.py`의 `run`에서, `report, reused, count = await self._ingestion.ingest_with_outcome(request)` + re-point 블록 이후, `return` 이전에:

```python
        # ROB-423 — copy the live report's news citations onto the mock
        # (report-level). Skip when this run idempotently reused an existing
        # mock that already carries them.
        if not reused:
            await self._news_service.copy_for_mock(
                live_report_uuid=live.report_uuid, mock_report=report
            )
```

(runner 생성자에 `self._news_service = InvestmentReportNewsService(self._reports_repo)` 배선.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/investment_reports/mock_preview/test_mock_news_citation_copy.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_reports/investment_report_news_service.py app/services/investment_reports/mock_preview/runner.py tests/services/investment_reports/mock_preview/test_mock_news_citation_copy.py
git commit -m "feat(ROB-423): mock_preview가 live news citation 복사(재fetch 없음)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: advisory-only smoke + 전체 검증 + lint

**Files:**
- Modify/Create: 기존 report-generation smoke 스크립트(`scripts/` 하위; PR1/ROB-373 smoke 재사용) 확장 — advisory report 생성 후 detail citation 출력
- Test: 없음(검증만) 또는 smoke 문서 runbook 한 줄

- [ ] **Step 1: 영역 전체 테스트**

Run:
```bash
uv run pytest \
  tests/models/test_investment_report_news_models.py \
  tests/schemas/test_hermes_news_citations.py \
  tests/services/investment_reports/test_news_persistence.py \
  tests/services/investment_stages/test_hermes_news_citation_ingest.py \
  tests/services/investment_reports/test_bundle_news_citations.py \
  tests/services/investment_reports/mock_preview/test_mock_news_citation_copy.py \
  tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py \
  -v
```
Expected: PASS (전부 green)

- [ ] **Step 2: 회귀 스윕(investment report / hermes / mock)**

Run: `uv run pytest -k "investment_report or hermes or mock_preview or news_citation" -q`
Expected: PASS (회귀 0)

- [ ] **Step 3: lint + format**

Run:
```bash
uv run ruff check app/models/investment_reports.py app/schemas/hermes_composition.py app/schemas/investment_reports.py app/services/investment_reports/news_persistence.py app/services/investment_reports/investment_report_news_service.py app/services/investment_reports/repository.py app/services/investment_reports/query_service.py app/services/investment_stages/hermes_ingest.py app/routers/investment_reports.py app/services/investment_reports/mock_preview/runner.py alembic/versions/20260603_rob423_news_citation_tables.py
uv run ruff format --check app/models/investment_reports.py app/schemas/hermes_composition.py app/schemas/investment_reports.py app/services/investment_reports/ app/services/investment_stages/hermes_ingest.py app/routers/investment_reports.py
```
Expected: clean

- [ ] **Step 4: advisory-only smoke (default-off, operator)**

기존 report-generation smoke를 advisory-only로 1회 실행(또는 dry-run), 생성된 report의 detail에서 `news_citations`를 출력해 확인. 실제 Hermes round-trip은 레포 밖 operator-gated이므로, smoke는 `news_citations`를 담은 합성 composition을 ingest → detail 노출까지 검증(테스트로 대체 가능).

Run: `uv run pytest tests/services/investment_reports/test_bundle_news_citations.py -v` (smoke 대체 — citation이 detail에 노출됨을 증명)
Expected: PASS

- [ ] **Step 5: 최종 커밋**

```bash
git add -A
git commit -m "test(ROB-423): PR2 영속/노출 회귀 스윕 + lint

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (spec 대조)

- **AC#1 (마이그레이션 2테이블 additive)**: Task 1+2. ✅
- **AC#5 (사용된 기사만, 전역 archive 없음)**: Task 4 매칭(unmatched drop) + 신규 archive 0. ✅
- **AC#6 (판단성 필드 Hermes만, in-process LLM 0)**: Task 3(Hermes 입력) + Task 5(persist=검증/영속만) + Step 5 가드. ✅
- **AC#7 (unknown ref drop + 기록, 날조 0)**: Task 4 `unmatched` + Task 5 `unavailable_sources` 기록. ✅
- **AC#8 (fail-open)**: Task 5 `persist_from_composition`는 매칭 갭에 예외 없음. ✅
- **AC#9 (detail에 title/source/url/published_at/provider/symbol/role/decision_impact)**: Task 6 response 스키마. ✅
- **AC#10 (mock은 복사, 재fetch/재판정 없음)**: Task 7. ✅
- **AC#12 (advisory-only smoke로 citation 확인)**: Task 8. ✅
- **migration head**: `20260602_rob412_main_merge`(구현 시 재확인). ✅

### 주의/리스크
- **per-item 링크 정렬**: `id.asc()` 재조회로 결정적 매핑(created_at 동률 회피). len(items) 불일치 시 item 링크 생략(report-level만) — fail-safe.
- **mock per-item 링크**: report-level 복사(report_item_uuid NULL). live↔mock item 매핑은 후속(필요 시).
- **fetch_runs**: live ingest에서만 작성(실제 fetch 근거). mock은 citation만 복사(fetch_run 없음) — 정직.
- **`unavailable_sources` 컬럼 부재 시**: `report.metadata`로 대체(Task 5 Step 2 노트).
- **테스트 DB**: alembic이 timescaledb로 막혀 통합 테스트는 `db_session` create_all 의존(ROB-407 선례). operator가 prod `alembic upgrade head`.
- **integration 테스트 fixture**: 기존 `tests/services/investment_stages/` Hermes ingest 테스트의 bundle/snapshot 셋업 패턴을 복사(상세 셋업은 그 파일들 참조).

## Out of Scope

PR1(완료) · provider 보조 구현 · per-item 링크의 mock 측 매핑 · 실 Hermes JSON-over-wire round-trip(operator-gated) · scheduler · broker/order mutation · production backfill.
