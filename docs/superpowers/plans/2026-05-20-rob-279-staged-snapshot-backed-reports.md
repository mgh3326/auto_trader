# ROB-279 Staged Snapshot-Backed Report Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ROB-269 가 만든 immutable snapshot bundle → idempotent ingest → stale gate 토대 위에, 감사 가능한 중간 stage artifact 들로부터 final `/invest/reports` 리포트를 합성하는 staged pipeline 을 추가한다.

**Architecture:** 신규 `investment_stage_runs` / `investment_stage_artifacts` 테이블을 snapshot bundle 에 귀속시키고, 8개 stage (5 deterministic + 3 LLM reducer) 가 bundle payload 만 읽어 구조화된 artifact 를 생성한다. final composer 가 artifact 들을 합쳐 기존 `InvestmentReportIngestionService.ingest()` 에 흘려보내고, stale gate 가 막아도 stage diagnostic 은 run-scoped API 로 조회된다.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2 (async) / Alembic / Pydantic v2 / pytest / React + Vitest + Testing Library / Google Gemini (`model_rate_limiter` 경유).

---

## Linear context

- Issue: https://linear.app/mgh3326/issue/ROB-279
- Parent: ROB-269 (Done)
- 결정 사항: 본 plan 은 issue description + "Design refinements (2026-05-20)" 코멘트 조합을 spec 으로 한다.

## Risk callouts (read first)

| 위험 | 영향 | 완화 |
|---|---|---|
| **마이그레이션 — 큰 신규 테이블 2개** | snapshot bundle 과의 FK 일관성, replication 동기화 | nullable FK, `ondelete="SET NULL"` 로 bundle 삭제 시 stage run 보존; alembic upgrade/downgrade 모두 테스트 |
| **LLM 비용 폭증** | report 1건당 호출이 8배로 늘어날 위험 | v1 stage 8개 중 **5개는 deterministic extraction 만**, **3개 reducer + 1 composer = 최대 4 LLM 호출** 하드 캡. `model_rate_limiter` 경유 강제. budget 초과 시 reducer 를 deterministic fallback 으로 강등 |
| **legacy `investment_report_generate_from_bundle` 호환성** | 기존 caller 가 깨지면 운영 중단 | 신규 동작은 `auto_compose=true` flag 뒤로 격리. flag 없으면 기존 `classify_items()` 경로 그대로. flag 가 false 일 때 stage run 도 생성하지 않음 |
| **stale gate 로 blocked 된 보고서의 stage diagnostic 미조회** | "왜 막혔는지" 추적 불가 | stage run/artifact 는 `ingest()` **이전에** persist. ingest 가 실패해도 `run_uuid` 는 남고, `GET /trading/api/investment-stage-runs/{run_uuid}` 로 조회 |
| **citation 강제 누락** | 환각/근거 없는 액션 권고 | composer 가 `cited_snapshot_uuids` 비어있는 stage artifact 의 결론은 caveat 또는 omit. acceptance test 로 보강 |
| **append-only invariant 위반** | 과거 artifact 변조 → 감사 불가 | DB trigger 또는 repository layer 에서 `INSERT only` (UPDATE/DELETE 금지) 강제. 단위 테스트로 회귀 방지 |

## File structure

**New backend files:**

```
alembic/versions/
  20260520_rob279_p1_add_stage_runs_and_artifacts.py    # 신규 테이블 2개 + 인덱스

app/models/
  investment_stages.py                                   # InvestmentStageRun, InvestmentStageArtifact ORM

app/schemas/
  investment_stages.py                                   # StageArtifactPayload, StageRunSummary 등

app/services/investment_stages/
  __init__.py
  repository.py                                          # InvestmentStagesRepository (append-only)
  query_service.py                                       # StageRunQueryService (read-only)
  budget.py                                              # StageLLMBudget — per-report call 캡
  stage_runner.py                                        # StageRunner — orchestrator
  composer.py                                            # FinalComposer — stage artifacts → report items
  stages/
    __init__.py
    base.py                                              # Stage protocol + StageContext
    market.py                                            # deterministic
    news.py                                              # deterministic
    portfolio_journal.py                                  # deterministic
    watch_context.py                                     # deterministic
    candidate_universe.py                                # deterministic
    bull_reducer.py                                      # LLM (gemini, budget-gated)
    bear_reducer.py                                      # LLM (gemini, budget-gated)
    risk_review.py                                       # LLM (gemini, budget-gated)

app/routers/
  investment_stage_runs.py                              # GET-only: run/artifact 조회
```

**Modified backend files:**

```
app/schemas/investment_reports.py                       # ReportGenerationRequest.auto_compose: bool = False
app/services/action_report/snapshot_backed/generator.py # auto_compose=true 분기
app/main.py                                             # router include
```

**New frontend files:**

```
frontend/invest/src/api/
  investmentStages.ts                                   # fetchReportStageArtifacts, fetchStageRun

frontend/invest/src/hooks/
  useReportStageArtifacts.ts                            # report-scoped (via bundle)
  useStageRun.ts                                        # run-scoped (blocked-report 진단용)

frontend/invest/src/components/investment-reports/
  IntermediateAnalysisPanel.tsx                         # report detail "중간 분석" 섹션
  StageArtifactCard.tsx                                 # verdict/confidence/citations/missing_data
  stageLabels.ts                                        # 한국어 stage_type 라벨

frontend/invest/src/types/
  # investmentReports.ts 에 StageRun/StageArtifact 타입 추가 (modify, not new file)
```

**Modified frontend files:**

```
frontend/invest/src/types/investmentReports.ts          # StageRun, StageArtifact, StageVerdict
frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx
                                                        # IntermediateAnalysisPanel mount
```

**New tests:**

```
tests/services/investment_stages/
  __init__.py
  test_repository.py
  test_stage_runner.py
  test_composer.py
  test_budget.py
  test_query_service.py
  test_stage_market.py
  test_stage_news.py
  test_stage_portfolio_journal.py
  test_stage_watch_context.py
  test_stage_candidate_universe.py
  test_stage_bull_reducer.py
  test_stage_bear_reducer.py
  test_stage_risk_review.py

tests/services/action_report/snapshot_backed/
  test_generator_auto_compose.py

tests/routers/
  test_investment_stage_runs.py

frontend/invest/src/__tests__/
  IntermediateAnalysisPanel.test.tsx
  StageArtifactCard.test.tsx
  useReportStageArtifacts.test.ts
```

---

## Phase 1 — Persistence foundation

> Goal: `investment_stage_runs` / `investment_stage_artifacts` 테이블 + ORM + Pydantic schema + append-only repository.
> 위험: nullable FK 설계, append-only invariant 회귀.

### Task 1.1: Alembic migration for stage tables

**Files:**
- Create: `alembic/versions/20260520_rob279_p1_add_stage_runs_and_artifacts.py`

- [ ] **Step 1: Write the migration**

```python
"""rob-279 p1: add investment_stage_runs and investment_stage_artifacts

Revision ID: 20260520_rob279_p1
Revises: 20260520_rob274_p2
Create Date: 2026-05-20

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260520_rob279_p1"
down_revision: str | None = "20260520_rob274_p2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "investment_stage_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "snapshot_bundle_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("market_session", sa.Text(), nullable=True),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=False, server_default=sa.text("'v1'")),
        sa.Column("generator_version", sa.Text(), nullable=False, server_default=sa.text("'v1'")),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('running','completed','failed','blocked')",
            name="ck_investment_stage_runs_status",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_stage_runs_bundle_uuid",
        "investment_stage_runs",
        ["snapshot_bundle_uuid"],
        schema="review",
    )

    op.create_table(
        "investment_stage_artifacts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "artifact_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("stage_type", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("key_points", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("buy_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sell_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("risk_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("missing_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "cited_snapshot_uuids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("ARRAY[]::uuid[]"),
        ),
        sa.Column("freshness_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "verdict IN ('bull','bear','neutral','unavailable')",
            name="ck_investment_stage_artifacts_verdict",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_investment_stage_artifacts_confidence_range",
        ),
        sa.CheckConstraint(
            "stage_type IN ("
            "'market','news','portfolio_journal','watch_context','candidate_universe',"
            "'bull_reducer','bear_reducer','risk_review')",
            name="ck_investment_stage_artifacts_stage_type_v1",
        ),
        sa.ForeignKeyConstraint(
            ["run_uuid"],
            ["review.investment_stage_runs.run_uuid"],
            name="fk_stage_artifacts_run_uuid",
            ondelete="CASCADE",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_stage_artifacts_run_stage",
        "investment_stage_artifacts",
        ["run_uuid", "stage_type"],
        unique=True,
        schema="review",
    )


def downgrade() -> None:
    op.drop_index("ix_investment_stage_artifacts_run_stage", table_name="investment_stage_artifacts", schema="review")
    op.drop_table("investment_stage_artifacts", schema="review")
    op.drop_index("ix_investment_stage_runs_bundle_uuid", table_name="investment_stage_runs", schema="review")
    op.drop_table("investment_stage_runs", schema="review")
```

- [ ] **Step 2: Run upgrade against local dev DB**

Run: `uv run alembic upgrade head`
Expected: `INFO  [alembic.runtime.migration] Running upgrade 20260520_rob274_p2 -> 20260520_rob279_p1`

- [ ] **Step 3: Verify schema**

Run: `docker compose exec postgres psql -U postgres -d auto_trader -c "\d review.investment_stage_runs"`
Expected: columns `run_uuid uuid NOT NULL`, `status text NOT NULL`, CHECK on status set.

- [ ] **Step 4: Run downgrade then upgrade to validate roundtrip**

Run: `uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: both succeed; no leftover constraints in `\d review.*`.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260520_rob279_p1_add_stage_runs_and_artifacts.py
git commit -m "feat(rob-279): add investment_stage_runs and investment_stage_artifacts tables"
```

### Task 1.2: ORM models

**Files:**
- Create: `app/models/investment_stages.py`

- [ ] **Step 1: Write failing test for model import + table mapping**

Create `tests/models/test_investment_stages_model.py`:

```python
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.investment_stages import InvestmentStageArtifact, InvestmentStageRun


@pytest.mark.asyncio
async def test_stage_run_insert_returns_uuid(db_session):
    run = InvestmentStageRun(
        snapshot_bundle_uuid=uuid.uuid4(),
        market="kr",
        market_session="regular",
        account_scope="kis_live",
    )
    db_session.add(run)
    await db_session.flush()
    assert run.run_uuid is not None
    assert run.status == "running"


@pytest.mark.asyncio
async def test_stage_artifact_fk_cascade(db_session):
    run = InvestmentStageRun(
        snapshot_bundle_uuid=uuid.uuid4(),
        market="kr",
    )
    db_session.add(run)
    await db_session.flush()

    artifact = InvestmentStageArtifact(
        run_uuid=run.run_uuid,
        stage_type="market",
        verdict="neutral",
        confidence=50,
        cited_snapshot_uuids=[],
    )
    db_session.add(artifact)
    await db_session.flush()

    fetched = await db_session.scalar(
        select(InvestmentStageArtifact).where(InvestmentStageArtifact.run_uuid == run.run_uuid)
    )
    assert fetched is not None
    assert fetched.stage_type == "market"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/models/test_investment_stages_model.py -v`
Expected: `ModuleNotFoundError: No module named 'app.models.investment_stages'`.

- [ ] **Step 3: Write the ORM module**

```python
"""Investment stage runs/artifacts ORM (ROB-279)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class InvestmentStageRun(Base):
    __tablename__ = "investment_stage_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','failed','blocked')",
            name="ck_investment_stage_runs_status",
        ),
        Index("ix_investment_stage_runs_bundle_uuid", "snapshot_bundle_uuid"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    snapshot_bundle_uuid: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    market_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'v1'"))
    generator_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'v1'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    artifacts: Mapped[list["InvestmentStageArtifact"]] = relationship(
        "InvestmentStageArtifact",
        primaryjoin="InvestmentStageRun.run_uuid==foreign(InvestmentStageArtifact.run_uuid)",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class InvestmentStageArtifact(Base):
    __tablename__ = "investment_stage_artifacts"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('bull','bear','neutral','unavailable')",
            name="ck_investment_stage_artifacts_verdict",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_investment_stage_artifacts_confidence_range",
        ),
        CheckConstraint(
            "stage_type IN ("
            "'market','news','portfolio_journal','watch_context','candidate_universe',"
            "'bull_reducer','bear_reducer','risk_review')",
            name="ck_investment_stage_artifacts_stage_type_v1",
        ),
        UniqueConstraint("run_uuid", "stage_type", name="ix_investment_stage_artifacts_run_stage"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("review.investment_stage_runs.run_uuid", ondelete="CASCADE"),
        nullable=False,
    )
    stage_type: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_points: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    buy_evidence: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    sell_evidence: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    risk_evidence: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    missing_data: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    cited_snapshot_uuids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default=text("ARRAY[]::uuid[]"),
    )
    freshness_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/models/test_investment_stages_model.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/models/investment_stages.py tests/models/test_investment_stages_model.py
git commit -m "feat(rob-279): ORM models for stage runs and artifacts"
```

### Task 1.3: Pydantic schemas

**Files:**
- Create: `app/schemas/investment_stages.py`

- [ ] **Step 1: Write failing test**

Create `tests/schemas/test_investment_stages_schema.py`:

```python
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)


def test_stage_artifact_payload_minimal_valid():
    payload = StageArtifactPayload(
        stage_type="market",
        verdict=StageVerdict.NEUTRAL,
        confidence=42,
    )
    assert payload.confidence == 42
    assert payload.cited_snapshots == []


def test_stage_artifact_payload_rejects_confidence_out_of_range():
    with pytest.raises(ValidationError):
        StageArtifactPayload(
            stage_type="market",
            verdict=StageVerdict.BULL,
            confidence=120,
        )


def test_stage_citation_requires_snapshot_uuid():
    citation = StageCitation(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="news",
        payload_path="$.articles[0].title",
    )
    assert citation.payload_path.startswith("$")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/schemas/test_investment_stages_schema.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the schema module**

```python
"""Pydantic schemas for investment stage runs/artifacts (ROB-279)."""

from __future__ import annotations

import enum
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

StageTypeLiteral = (
    "market",
    "news",
    "portfolio_journal",
    "watch_context",
    "candidate_universe",
    "bull_reducer",
    "bear_reducer",
    "risk_review",
)


class StageVerdict(str, enum.Enum):
    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"
    UNAVAILABLE = "unavailable"


class StageCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_uuid: uuid.UUID
    snapshot_kind: str
    payload_path: str | None = None


class StageArtifactPayload(BaseModel):
    """Structured output that every stage MUST emit."""

    model_config = ConfigDict(extra="forbid")

    stage_type: str
    verdict: StageVerdict
    confidence: int = Field(ge=0, le=100)
    summary: str | None = None
    key_points: list[str] = Field(default_factory=list)
    buy_evidence: list[str] = Field(default_factory=list)
    sell_evidence: list[str] = Field(default_factory=list)
    risk_evidence: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    cited_snapshots: list[StageCitation] = Field(default_factory=list)
    freshness_summary: dict[str, Any] | None = None
    model_name: str | None = None
    prompt_version: str | None = None


class StageRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    run_uuid: uuid.UUID
    snapshot_bundle_uuid: uuid.UUID
    market: str
    market_session: str | None
    account_scope: str | None
    policy_version: str
    generator_version: str
    status: str
    started_at: str
    completed_at: str | None
    metadata_json: dict[str, Any] | None = None


class StageArtifactRead(StageArtifactPayload):
    artifact_uuid: uuid.UUID
    run_uuid: uuid.UUID
    created_at: str
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/schemas/test_investment_stages_schema.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/investment_stages.py tests/schemas/test_investment_stages_schema.py
git commit -m "feat(rob-279): Pydantic schemas for stage artifacts"
```

### Task 1.4: Repository with append-only invariant

**Files:**
- Create: `app/services/investment_stages/__init__.py`
- Create: `app/services/investment_stages/repository.py`
- Create: `tests/services/investment_stages/__init__.py`
- Create: `tests/services/investment_stages/test_repository.py`

- [ ] **Step 1: Write failing tests**

```python
import uuid

import pytest

from app.models.investment_stages import InvestmentStageArtifact
from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.repository import (
    AppendOnlyViolation,
    InvestmentStagesRepository,
)


@pytest.mark.asyncio
async def test_repository_creates_run_and_returns_uuid(db_session):
    repo = InvestmentStagesRepository(db_session)
    bundle_uuid = uuid.uuid4()
    run = await repo.create_run(
        snapshot_bundle_uuid=bundle_uuid,
        market="kr",
        market_session="regular",
        account_scope="kis_live",
        policy_version="v1",
        generator_version="v1",
    )
    assert run.run_uuid is not None
    assert run.status == "running"


@pytest.mark.asyncio
async def test_repository_persist_artifact_then_reject_overwrite(db_session):
    repo = InvestmentStagesRepository(db_session)
    bundle_uuid = uuid.uuid4()
    run = await repo.create_run(
        snapshot_bundle_uuid=bundle_uuid, market="kr"
    )
    payload = StageArtifactPayload(
        stage_type="market",
        verdict=StageVerdict.NEUTRAL,
        confidence=50,
    )
    artifact = await repo.persist_artifact(run.run_uuid, payload)
    assert artifact.stage_type == "market"

    with pytest.raises(AppendOnlyViolation):
        await repo.persist_artifact(run.run_uuid, payload)


@pytest.mark.asyncio
async def test_repository_complete_run_sets_status(db_session):
    repo = InvestmentStagesRepository(db_session)
    run = await repo.create_run(
        snapshot_bundle_uuid=uuid.uuid4(), market="kr"
    )
    await repo.complete_run(run.run_uuid, status="completed")
    refreshed = await repo.get_run(run.run_uuid)
    assert refreshed.status == "completed"
    assert refreshed.completed_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/investment_stages/test_repository.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the repository module**

```python
"""Append-only repository for stage runs/artifacts (ROB-279)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_stages import InvestmentStageArtifact, InvestmentStageRun
from app.schemas.investment_stages import StageArtifactPayload


class AppendOnlyViolation(Exception):
    """Raised when caller attempts to overwrite an existing stage artifact."""


class InvestmentStagesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(
        self,
        *,
        snapshot_bundle_uuid: uuid.UUID,
        market: str,
        market_session: str | None = None,
        account_scope: str | None = None,
        policy_version: str = "v1",
        generator_version: str = "v1",
    ) -> InvestmentStageRun:
        run = InvestmentStageRun(
            snapshot_bundle_uuid=snapshot_bundle_uuid,
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            policy_version=policy_version,
            generator_version=generator_version,
        )
        self._session.add(run)
        await self._session.flush()
        await self._session.refresh(run)
        return run

    async def persist_artifact(
        self, run_uuid: uuid.UUID, payload: StageArtifactPayload
    ) -> InvestmentStageArtifact:
        artifact = InvestmentStageArtifact(
            run_uuid=run_uuid,
            stage_type=payload.stage_type,
            verdict=payload.verdict.value,
            confidence=payload.confidence,
            summary=payload.summary,
            key_points=payload.key_points,
            buy_evidence=payload.buy_evidence,
            sell_evidence=payload.sell_evidence,
            risk_evidence=payload.risk_evidence,
            missing_data=payload.missing_data,
            cited_snapshot_uuids=[c.snapshot_uuid for c in payload.cited_snapshots],
            freshness_summary=payload.freshness_summary,
            model_name=payload.model_name,
            prompt_version=payload.prompt_version,
        )
        self._session.add(artifact)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "ix_investment_stage_artifacts_run_stage" in str(exc):
                raise AppendOnlyViolation(
                    f"stage_type={payload.stage_type} already persisted for run {run_uuid}"
                ) from exc
            raise
        await self._session.refresh(artifact)
        return artifact

    async def complete_run(self, run_uuid: uuid.UUID, *, status: str) -> None:
        run = await self._session.scalar(
            select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid)
        )
        if run is None:
            raise ValueError(f"run not found: {run_uuid}")
        if status not in {"completed", "failed", "blocked"}:
            raise ValueError(f"invalid terminal status: {status}")
        run.status = status
        run.completed_at = dt.datetime.now(tz=dt.UTC)
        await self._session.flush()

    async def get_run(self, run_uuid: uuid.UUID) -> InvestmentStageRun | None:
        return await self._session.scalar(
            select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid)
        )

    async def list_artifacts_for_run(
        self, run_uuid: uuid.UUID
    ) -> list[InvestmentStageArtifact]:
        result = await self._session.scalars(
            select(InvestmentStageArtifact)
            .where(InvestmentStageArtifact.run_uuid == run_uuid)
            .order_by(InvestmentStageArtifact.created_at)
        )
        return list(result.all())
```

Also touch `app/services/investment_stages/__init__.py` (empty file).

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/services/investment_stages/test_repository.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/__init__.py app/services/investment_stages/repository.py tests/services/investment_stages/
git commit -m "feat(rob-279): append-only repository for stage runs"
```

### Task 1.5: Query service (read-only)

**Files:**
- Create: `app/services/investment_stages/query_service.py`
- Create: `tests/services/investment_stages/test_query_service.py`

- [ ] **Step 1: Write failing test for report-scoped query**

```python
import uuid

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.query_service import StageRunQueryService
from app.services.investment_stages.repository import InvestmentStagesRepository


@pytest.mark.asyncio
async def test_query_service_returns_artifacts_by_run(db_session):
    repo = InvestmentStagesRepository(db_session)
    bundle_uuid = uuid.uuid4()
    run = await repo.create_run(snapshot_bundle_uuid=bundle_uuid, market="kr")
    await repo.persist_artifact(
        run.run_uuid,
        StageArtifactPayload(stage_type="market", verdict=StageVerdict.NEUTRAL, confidence=10),
    )
    await db_session.flush()

    svc = StageRunQueryService(db_session)
    result = await svc.get_run_with_artifacts(run.run_uuid)

    assert result is not None
    assert result.run.run_uuid == run.run_uuid
    assert len(result.artifacts) == 1
    assert result.artifacts[0].stage_type == "market"


@pytest.mark.asyncio
async def test_query_service_returns_none_for_missing_run(db_session):
    svc = StageRunQueryService(db_session)
    assert await svc.get_run_with_artifacts(uuid.uuid4()) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/services/investment_stages/test_query_service.py -v`
Expected: `ImportError`.

- [ ] **Step 3: Implement query service**

```python
"""Read-only query service for stage runs (ROB-279)."""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_stages import InvestmentStageArtifact, InvestmentStageRun


@dataclasses.dataclass(frozen=True)
class StageRunWithArtifacts:
    run: InvestmentStageRun
    artifacts: list[InvestmentStageArtifact]


class StageRunQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_run_with_artifacts(
        self, run_uuid: uuid.UUID
    ) -> StageRunWithArtifacts | None:
        run = await self._session.scalar(
            select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid)
        )
        if run is None:
            return None
        artifacts = list(
            (
                await self._session.scalars(
                    select(InvestmentStageArtifact)
                    .where(InvestmentStageArtifact.run_uuid == run_uuid)
                    .order_by(InvestmentStageArtifact.created_at)
                )
            ).all()
        )
        return StageRunWithArtifacts(run=run, artifacts=artifacts)

    async def list_runs_for_bundle(
        self, snapshot_bundle_uuid: uuid.UUID
    ) -> list[InvestmentStageRun]:
        result = await self._session.scalars(
            select(InvestmentStageRun)
            .where(InvestmentStageRun.snapshot_bundle_uuid == snapshot_bundle_uuid)
            .order_by(InvestmentStageRun.started_at.desc())
        )
        return list(result.all())
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/investment_stages/test_query_service.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/query_service.py tests/services/investment_stages/test_query_service.py
git commit -m "feat(rob-279): stage run query service"
```

---

## Phase 2 — Stage runner + 8 stages

> Goal: bundle 만 읽어 8개 stage 의 `StageArtifactPayload` 를 생성. v1 5개 deterministic + 3개 LLM reducer. budget cap, model_rate_limiter 경유.
> 위험: LLM 비용 폭증, deterministic extraction 의 brittleness.

### Task 2.1: Stage protocol + StageContext

**Files:**
- Create: `app/services/investment_stages/stages/__init__.py`
- Create: `app/services/investment_stages/stages/base.py`
- Create: `tests/services/investment_stages/stages/__init__.py`
- Create: `tests/services/investment_stages/stages/test_base.py`

- [ ] **Step 1: Write failing test**

```python
import uuid

import pytest

from app.services.investment_stages.stages.base import (
    Stage,
    StageContext,
    UnavailableStageError,
)


class _FakeBundleReadService:
    async def get_bundle(self, *, bundle_uuid):
        raise NotImplementedError


def test_stage_context_holds_bundle_and_snapshots():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"market": []},
        bundle_metadata={"freshness_overall": "fresh"},
    )
    assert ctx.snapshots_for("market") == []
    assert ctx.snapshots_for("unknown") == []


def test_unavailable_stage_error_carries_reason():
    with pytest.raises(UnavailableStageError) as exc:
        raise UnavailableStageError("portfolio snapshot missing")
    assert "portfolio" in str(exc.value)
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/services/investment_stages/stages/test_base.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement base**

```python
"""Stage protocol and shared context (ROB-279)."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Protocol

from app.models.investment_snapshots import InvestmentSnapshot
from app.schemas.investment_stages import StageArtifactPayload


class UnavailableStageError(Exception):
    """Raised by a stage when required snapshots are absent.
    The runner converts this to an `UNAVAILABLE` artifact rather than failing the run."""


@dataclasses.dataclass(frozen=True)
class StageContext:
    bundle_uuid: uuid.UUID
    snapshots_by_kind: dict[str, list[InvestmentSnapshot]]
    bundle_metadata: dict[str, Any]

    def snapshots_for(self, kind: str) -> list[InvestmentSnapshot]:
        return self.snapshots_by_kind.get(kind, [])


class Stage(Protocol):
    stage_type: str

    async def run(self, context: StageContext) -> StageArtifactPayload: ...
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/investment_stages/stages/test_base.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/{__init__.py,base.py} tests/services/investment_stages/stages/
git commit -m "feat(rob-279): stage protocol + StageContext"
```

### Task 2.2: Stage runner (orchestrator)

**Files:**
- Create: `app/services/investment_stages/stage_runner.py`
- Create: `tests/services/investment_stages/test_stage_runner.py`

The runner:
1. opens a `StageRun` in DB
2. loads bundle items via `SnapshotBundleReadService`
3. groups snapshots by `snapshot_kind`
4. invokes each registered stage sequentially (deterministic first, reducers last)
5. persists every artifact (including `UNAVAILABLE` for missing-data stages)
6. marks run `completed` (or `failed` on infrastructure error — single-stage failure does not fail the run)

- [ ] **Step 1: Write failing test (mocked stages)**

```python
import uuid

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.stage_runner import StageRunner
from app.services.investment_stages.stages.base import (
    Stage,
    StageContext,
    UnavailableStageError,
)


class _MarketStub:
    stage_type = "market"

    async def run(self, ctx: StageContext) -> StageArtifactPayload:
        return StageArtifactPayload(
            stage_type="market", verdict=StageVerdict.BULL, confidence=70
        )


class _NewsUnavailable:
    stage_type = "news"

    async def run(self, ctx: StageContext) -> StageArtifactPayload:
        raise UnavailableStageError("no news snapshot")


class _StubBundleReadService:
    def __init__(self, bundle_uuid):
        self._bundle_uuid = bundle_uuid

    async def get_bundle(self, *, bundle_uuid):
        from types import SimpleNamespace
        return SimpleNamespace(
            bundle=SimpleNamespace(bundle_uuid=bundle_uuid, status="complete"),
            items=[],
        )


@pytest.mark.asyncio
async def test_stage_runner_runs_all_stages_and_persists(db_session):
    bundle_uuid = uuid.uuid4()
    runner = StageRunner(
        session=db_session,
        bundle_read_service=_StubBundleReadService(bundle_uuid),
        stages=[_MarketStub(), _NewsUnavailable()],
    )

    run = await runner.run(
        snapshot_bundle_uuid=bundle_uuid,
        market="kr",
        market_session="regular",
        account_scope="kis_live",
    )

    assert run.status == "completed"
    artifacts = sorted(run.artifacts, key=lambda a: a.stage_type)
    assert [a.stage_type for a in artifacts] == ["market", "news"]
    assert artifacts[0].verdict == "bull"
    assert artifacts[1].verdict == "unavailable"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/services/investment_stages/test_stage_runner.py -v`
Expected: `ImportError`.

- [ ] **Step 3: Implement runner**

```python
"""Stage runner orchestrator (ROB-279)."""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_stages import InvestmentStageRun
from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_snapshots.read_service import SnapshotBundleReadService
from app.services.investment_stages.repository import InvestmentStagesRepository
from app.services.investment_stages.stages.base import (
    Stage,
    StageContext,
    UnavailableStageError,
)

_logger = logging.getLogger(__name__)


class StageRunner:
    def __init__(
        self,
        *,
        session: AsyncSession,
        bundle_read_service: SnapshotBundleReadService | object,
        stages: Iterable[Stage],
    ) -> None:
        self._session = session
        self._bundle_read = bundle_read_service
        self._stages = list(stages)
        self._repo = InvestmentStagesRepository(session)

    async def run(
        self,
        *,
        snapshot_bundle_uuid: uuid.UUID,
        market: str,
        market_session: str | None,
        account_scope: str | None,
        policy_version: str = "v1",
        generator_version: str = "v1",
    ) -> InvestmentStageRun:
        run = await self._repo.create_run(
            snapshot_bundle_uuid=snapshot_bundle_uuid,
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            policy_version=policy_version,
            generator_version=generator_version,
        )

        bundle_response = await self._bundle_read.get_bundle(bundle_uuid=snapshot_bundle_uuid)
        snapshots_by_kind: dict[str, list] = defaultdict(list)
        for item in getattr(bundle_response, "items", []):
            snapshot = getattr(item, "snapshot", None) or item
            kind = getattr(snapshot, "snapshot_kind", None)
            if kind:
                snapshots_by_kind[kind].append(snapshot)

        bundle = getattr(bundle_response, "bundle", None)
        ctx = StageContext(
            bundle_uuid=snapshot_bundle_uuid,
            snapshots_by_kind=dict(snapshots_by_kind),
            bundle_metadata={
                "status": getattr(bundle, "status", None),
                "freshness_summary": getattr(bundle, "freshness_summary", None),
            },
        )

        for stage in self._stages:
            try:
                payload = await stage.run(ctx)
            except UnavailableStageError as exc:
                _logger.info("stage %s unavailable: %s", stage.stage_type, exc)
                payload = StageArtifactPayload(
                    stage_type=stage.stage_type,
                    verdict=StageVerdict.UNAVAILABLE,
                    confidence=0,
                    summary=str(exc),
                    missing_data=[stage.stage_type],
                )
            except Exception as exc:  # noqa: BLE001 — explicit unavailable on any stage failure
                _logger.exception("stage %s failed", stage.stage_type)
                payload = StageArtifactPayload(
                    stage_type=stage.stage_type,
                    verdict=StageVerdict.UNAVAILABLE,
                    confidence=0,
                    summary=f"stage error: {exc!r}",
                    missing_data=[stage.stage_type],
                )
            await self._repo.persist_artifact(run.run_uuid, payload)

        await self._repo.complete_run(run.run_uuid, status="completed")
        await self._session.refresh(run)
        return run
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/services/investment_stages/test_stage_runner.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stage_runner.py tests/services/investment_stages/test_stage_runner.py
git commit -m "feat(rob-279): stage runner orchestrator"
```

### Task 2.3: LLM budget guard

**Files:**
- Create: `app/services/investment_stages/budget.py`
- Create: `tests/services/investment_stages/test_budget.py`

- [ ] **Step 1: Write failing test**

```python
import pytest

from app.services.investment_stages.budget import (
    BudgetExceeded,
    StageLLMBudget,
)


def test_budget_consumes_within_cap():
    b = StageLLMBudget(max_calls=4)
    for _ in range(4):
        b.consume("bull_reducer")
    assert b.remaining == 0


def test_budget_rejects_overshoot():
    b = StageLLMBudget(max_calls=2)
    b.consume("a")
    b.consume("b")
    with pytest.raises(BudgetExceeded):
        b.consume("c")
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/services/investment_stages/test_budget.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement budget**

```python
"""LLM call budget guard for staged reports (ROB-279).

Per-report cap: 4 (3 reducers + 1 composer). Stages that would overshoot
must degrade to deterministic-only fallback or `UNAVAILABLE`."""

from __future__ import annotations

import dataclasses


class BudgetExceeded(Exception):
    pass


@dataclasses.dataclass
class StageLLMBudget:
    max_calls: int = 4
    _used: list[str] = dataclasses.field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(self.max_calls - len(self._used), 0)

    def consume(self, label: str) -> None:
        if len(self._used) >= self.max_calls:
            raise BudgetExceeded(
                f"LLM budget exhausted (cap={self.max_calls}, used={self._used})"
            )
        self._used.append(label)

    def used(self) -> list[str]:
        return list(self._used)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/investment_stages/test_budget.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/budget.py tests/services/investment_stages/test_budget.py
git commit -m "feat(rob-279): per-report LLM budget guard"
```

### Task 2.4: Deterministic stage — `market`

**Files:**
- Create: `app/services/investment_stages/stages/market.py`
- Create: `tests/services/investment_stages/stages/test_market.py`

The market stage extracts index direction (KOSPI/KOSDAQ for KR) from `market` snapshots and emits a verdict purely from numeric thresholds (no LLM).

- [ ] **Step 1: Write failing test**

```python
import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.market import MarketStage


def _snapshot(payload):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="market",
        payload_json=payload,
    )


@pytest.mark.asyncio
async def test_market_stage_emits_bull_when_index_up():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"KOSPI": {"change_percent": 1.5}}})]
        },
        bundle_metadata={},
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.confidence >= 50
    assert len(payload.cited_snapshots) == 1


@pytest.mark.asyncio
async def test_market_stage_emits_bear_when_index_down():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"KOSPI": {"change_percent": -2.0}}})]
        },
        bundle_metadata={},
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BEAR


@pytest.mark.asyncio
async def test_market_stage_raises_unavailable_when_no_snapshot():
    ctx = StageContext(bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={})
    with pytest.raises(UnavailableStageError):
        await MarketStage().run(ctx)
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/services/investment_stages/stages/test_market.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement market stage**

```python
"""Deterministic market stage (ROB-279)."""

from __future__ import annotations

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages.base import StageContext, UnavailableStageError

_BULL_THRESHOLD = 0.5
_BEAR_THRESHOLD = -0.5


class MarketStage:
    stage_type = "market"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("market")
        if not snapshots:
            raise UnavailableStageError("market snapshot missing from bundle")

        snapshot = snapshots[0]
        indices = (snapshot.payload_json or {}).get("indices", {})
        kospi = indices.get("KOSPI") or indices.get("kospi") or {}
        change = float(kospi.get("change_percent", 0.0))

        if change >= _BULL_THRESHOLD:
            verdict = StageVerdict.BULL
        elif change <= _BEAR_THRESHOLD:
            verdict = StageVerdict.BEAR
        else:
            verdict = StageVerdict.NEUTRAL

        confidence = min(int(abs(change) * 30), 90)

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=max(confidence, 30 if verdict != StageVerdict.NEUTRAL else 20),
            summary=f"KOSPI change_percent={change:+.2f}%",
            key_points=[f"KOSPI {change:+.2f}%"],
            buy_evidence=[f"KOSPI 상승 {change:+.2f}%"] if verdict == StageVerdict.BULL else [],
            sell_evidence=[f"KOSPI 하락 {change:+.2f}%"] if verdict == StageVerdict.BEAR else [],
            cited_snapshots=[
                StageCitation(
                    snapshot_uuid=snapshot.snapshot_uuid,
                    snapshot_kind="market",
                    payload_path="$.indices.KOSPI.change_percent",
                )
            ],
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/investment_stages/stages/test_market.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/market.py tests/services/investment_stages/stages/test_market.py
git commit -m "feat(rob-279): deterministic market stage"
```

### Task 2.5: Deterministic stage — `news`

**Files:**
- Create: `app/services/investment_stages/stages/news.py`
- Create: `tests/services/investment_stages/stages/test_news.py`

News stage groups article headlines by sentiment hint (positive/negative keyword counts) — no LLM. Tests follow the same shape as Task 2.4 with snapshot payload `{"articles": [{"title": ..., "sentiment": "positive"}, ...]}`. Implementation aggregates sentiment counts and emits BULL/BEAR/NEUTRAL.

- [ ] **Step 1: Write failing test**

```python
import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.news import NewsStage


def _snap(articles):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="news",
        payload_json={"articles": articles},
    )


@pytest.mark.asyncio
async def test_news_stage_neutral_on_empty_articles():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"news": [_snap([])]},
        bundle_metadata={},
    )
    payload = await NewsStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL


@pytest.mark.asyncio
async def test_news_stage_bull_when_positive_dominates():
    articles = [{"title": "good", "sentiment": "positive"}] * 5 + [
        {"title": "bad", "sentiment": "negative"}
    ]
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"news": [_snap(articles)]},
        bundle_metadata={},
    )
    payload = await NewsStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.cited_snapshots
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/services/investment_stages/stages/test_news.py -v`

- [ ] **Step 3: Implement**

```python
"""Deterministic news stage (ROB-279)."""

from __future__ import annotations

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages.base import StageContext, UnavailableStageError


class NewsStage:
    stage_type = "news"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("news")
        if not snapshots:
            raise UnavailableStageError("news snapshot missing")

        articles: list[dict] = []
        citations: list[StageCitation] = []
        for snap in snapshots:
            payload = snap.payload_json or {}
            for art in payload.get("articles", []):
                articles.append(art)
            citations.append(
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="news",
                    payload_path="$.articles",
                )
            )

        pos = sum(1 for a in articles if a.get("sentiment") == "positive")
        neg = sum(1 for a in articles if a.get("sentiment") == "negative")
        total = len(articles)
        if total == 0:
            verdict = StageVerdict.NEUTRAL
            confidence = 10
        elif pos >= neg * 2 and pos >= 3:
            verdict = StageVerdict.BULL
            confidence = min(40 + pos * 5, 80)
        elif neg >= pos * 2 and neg >= 3:
            verdict = StageVerdict.BEAR
            confidence = min(40 + neg * 5, 80)
        else:
            verdict = StageVerdict.NEUTRAL
            confidence = 30

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=confidence,
            summary=f"{total} articles (pos={pos}, neg={neg})",
            key_points=[a.get("title", "")[:60] for a in articles[:5]],
            cited_snapshots=citations,
            missing_data=[] if articles else ["news_articles"],
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/investment_stages/stages/test_news.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/news.py tests/services/investment_stages/stages/test_news.py
git commit -m "feat(rob-279): deterministic news stage"
```

### Task 2.6: Deterministic stage — `portfolio_journal`

**Files:**
- Create: `app/services/investment_stages/stages/portfolio_journal.py`
- Create: `tests/services/investment_stages/stages/test_portfolio_journal.py`

Reads `portfolio` and `journal` snapshots. Verdict is informational: if cash buying power < 5% of portfolio NAV → confidence-reducer flag; if open journal entries exist with targets → emit `key_points` listing them.

- [ ] **Step 1: Write failing tests**

```python
import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.portfolio_journal import PortfolioJournalStage


def _snap(kind, payload):
    return SimpleNamespace(snapshot_uuid=uuid.uuid4(), snapshot_kind=kind, payload_json=payload)


@pytest.mark.asyncio
async def test_portfolio_journal_unavailable_without_portfolio():
    ctx = StageContext(bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={})
    with pytest.raises(UnavailableStageError):
        await PortfolioJournalStage().run(ctx)


@pytest.mark.asyncio
async def test_portfolio_journal_emits_neutral_with_buying_power():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [_snap("portfolio", {"buying_power_krw": 200000, "nav_krw": 1000000})],
            "journal": [_snap("journal", {"entries": [{"symbol": "035420", "thesis": "tech"}]})],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL
    assert "035420" in (payload.summary or "")
    assert len(payload.cited_snapshots) >= 1
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/services/investment_stages/stages/test_portfolio_journal.py -v`

- [ ] **Step 3: Implement**

```python
"""Deterministic portfolio+journal stage (ROB-279)."""

from __future__ import annotations

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages.base import StageContext, UnavailableStageError


class PortfolioJournalStage:
    stage_type = "portfolio_journal"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        portfolio_snaps = context.snapshots_for("portfolio")
        if not portfolio_snaps:
            raise UnavailableStageError("portfolio snapshot missing — required")
        portfolio = portfolio_snaps[0]
        journal_snaps = context.snapshots_for("journal")

        nav = float((portfolio.payload_json or {}).get("nav_krw", 0.0))
        buying_power = float((portfolio.payload_json or {}).get("buying_power_krw", 0.0))
        bp_ratio = (buying_power / nav) if nav > 0 else 0.0

        entries = []
        for snap in journal_snaps:
            entries.extend((snap.payload_json or {}).get("entries", []))

        citations = [
            StageCitation(
                snapshot_uuid=portfolio.snapshot_uuid,
                snapshot_kind="portfolio",
                payload_path="$.buying_power_krw",
            )
        ]
        for snap in journal_snaps:
            citations.append(
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="journal",
                    payload_path="$.entries",
                )
            )

        symbols = ", ".join(e.get("symbol", "?") for e in entries[:5])
        summary = (
            f"NAV={nav:,.0f}, buying_power_krw={buying_power:,.0f} "
            f"({bp_ratio:.1%}), open journal: {symbols or 'none'}"
        )

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=StageVerdict.NEUTRAL,
            confidence=60 if bp_ratio >= 0.05 else 40,
            summary=summary,
            key_points=[e.get("thesis", "") for e in entries[:5] if e.get("thesis")],
            risk_evidence=[] if bp_ratio >= 0.05 else ["buying_power < 5% NAV"],
            cited_snapshots=citations,
            missing_data=[] if journal_snaps else ["journal"],
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/investment_stages/stages/test_portfolio_journal.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/portfolio_journal.py tests/services/investment_stages/stages/test_portfolio_journal.py
git commit -m "feat(rob-279): deterministic portfolio_journal stage"
```

### Task 2.7: Deterministic stage — `watch_context`

**Files:**
- Create: `app/services/investment_stages/stages/watch_context.py`
- Create: `tests/services/investment_stages/stages/test_watch_context.py`

Reads `watch_context` snapshot. Lists active watch alerts and previously triggered intents. Verdict NEUTRAL; key_points are alert summaries.

- [ ] **Step 1: Write failing test**

```python
import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.watch_context import WatchContextStage


def _snap(payload):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="watch_context",
        payload_json=payload,
    )


@pytest.mark.asyncio
async def test_watch_context_unavailable():
    with pytest.raises(UnavailableStageError):
        await WatchContextStage().run(
            StageContext(bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={})
        )


@pytest.mark.asyncio
async def test_watch_context_lists_active_alerts():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "watch_context": [
                _snap(
                    {
                        "active_alerts": [
                            {"symbol": "035420", "condition": "price < 200000"},
                            {"symbol": "015760", "condition": "price > 25000"},
                        ]
                    }
                )
            ]
        },
        bundle_metadata={},
    )
    payload = await WatchContextStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL
    assert any("035420" in kp for kp in payload.key_points)
```

- [ ] **Step 2-3: Implement**

```python
"""Deterministic watch_context stage (ROB-279)."""

from __future__ import annotations

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages.base import StageContext, UnavailableStageError


class WatchContextStage:
    stage_type = "watch_context"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("watch_context")
        if not snapshots:
            raise UnavailableStageError("watch_context snapshot missing — required")

        snap = snapshots[0]
        payload = snap.payload_json or {}
        active = payload.get("active_alerts", [])

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=StageVerdict.NEUTRAL,
            confidence=50 if active else 30,
            summary=f"{len(active)} active watch alerts",
            key_points=[
                f"{a.get('symbol', '?')}: {a.get('condition', '?')}" for a in active[:5]
            ],
            cited_snapshots=[
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="watch_context",
                    payload_path="$.active_alerts",
                )
            ],
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/investment_stages/stages/test_watch_context.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/watch_context.py tests/services/investment_stages/stages/test_watch_context.py
git commit -m "feat(rob-279): deterministic watch_context stage"
```

### Task 2.8: Deterministic stage — `candidate_universe`

**Files:**
- Create: `app/services/investment_stages/stages/candidate_universe.py`
- Create: `tests/services/investment_stages/stages/test_candidate_universe.py`

Reads `candidate_universe` snapshot. Verdict NEUTRAL or BULL based on whether top candidates exist with positive momentum.

- [ ] **Step 1: Write failing test**

```python
import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.candidate_universe import CandidateUniverseStage


def _snap(payload):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="candidate_universe",
        payload_json=payload,
    )


@pytest.mark.asyncio
async def test_candidate_universe_empty():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"candidate_universe": [_snap({"candidates": []})]},
        bundle_metadata={},
    )
    payload = await CandidateUniverseStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL
    assert payload.confidence < 40


@pytest.mark.asyncio
async def test_candidate_universe_bull_when_top_candidates():
    candidates = [
        {"symbol": s, "score": 8.0, "reason": "momentum"}
        for s in ("035420", "015760", "005930")
    ]
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"candidate_universe": [_snap({"candidates": candidates})]},
        bundle_metadata={},
    )
    payload = await CandidateUniverseStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert "035420" in payload.summary
```

- [ ] **Step 2-3: Implement**

```python
"""Deterministic candidate_universe stage (ROB-279)."""

from __future__ import annotations

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages.base import StageContext, UnavailableStageError


class CandidateUniverseStage:
    stage_type = "candidate_universe"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("candidate_universe")
        if not snapshots:
            raise UnavailableStageError("candidate_universe snapshot missing")
        snap = snapshots[0]
        candidates = (snap.payload_json or {}).get("candidates", [])
        top = sorted(candidates, key=lambda c: c.get("score", 0.0), reverse=True)[:5]

        if not top:
            verdict = StageVerdict.NEUTRAL
            confidence = 20
            summary = "no candidates returned by screener"
        elif top[0].get("score", 0.0) >= 7.0:
            verdict = StageVerdict.BULL
            confidence = min(40 + len(top) * 8, 75)
            summary = "top candidates: " + ", ".join(c.get("symbol", "?") for c in top)
        else:
            verdict = StageVerdict.NEUTRAL
            confidence = 35
            summary = "candidates present but low score"

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=confidence,
            summary=summary,
            key_points=[
                f"{c.get('symbol', '?')} (score={c.get('score', 0):.1f}): {c.get('reason', '')}"
                for c in top
            ],
            buy_evidence=[c.get("symbol", "?") for c in top] if verdict == StageVerdict.BULL else [],
            cited_snapshots=[
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="candidate_universe",
                    payload_path="$.candidates",
                )
            ],
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/investment_stages/stages/test_candidate_universe.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/candidate_universe.py tests/services/investment_stages/stages/test_candidate_universe.py
git commit -m "feat(rob-279): deterministic candidate_universe stage"
```

### Task 2.9: LLM stage — `bull_reducer`

**Files:**
- Create: `app/services/investment_stages/stages/bull_reducer.py`
- Create: `tests/services/investment_stages/stages/test_bull_reducer.py`

Reducer takes the 5 deterministic stage artifacts produced earlier (passed via `StageContext.bundle_metadata['prior_stages']`) plus their cited snapshots, and asks Gemini to summarize the bull case. **MUST go through `model_rate_limiter`** and **MUST consume from `StageLLMBudget`**. If budget exhausted, degrade to deterministic concat of `buy_evidence` from prior stages.

> **Note for Task 2.10/2.11:** the runner extension to pass `prior_stages` happens in Task 2.12. For now, reducer reads from `context.bundle_metadata.get("prior_stages", [])`.

- [ ] **Step 1: Write failing test (mock LLM client)**

```python
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.bull_reducer import BullReducerStage


@pytest.mark.asyncio
async def test_bull_reducer_uses_llm_when_budget_available():
    llm = AsyncMock()
    llm.complete_json.return_value = {
        "summary": "복수 stage 가 매수 지지",
        "confidence": 70,
        "key_points": ["KOSPI 상승", "후보 종목 풍부"],
    }
    budget = StageLLMBudget(max_calls=4)
    prior = [
        StageArtifactPayload(
            stage_type="market",
            verdict=StageVerdict.BULL,
            confidence=60,
            buy_evidence=["KOSPI +1.5%"],
        ).model_dump()
    ]
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={},
        bundle_metadata={"prior_stages": prior},
    )

    stage = BullReducerStage(llm_client=llm, budget=budget)
    payload = await stage.run(ctx)

    assert payload.verdict == StageVerdict.BULL
    assert payload.confidence == 70
    assert budget.remaining == 3
    llm.complete_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_bull_reducer_degrades_when_budget_exhausted():
    llm = AsyncMock()
    budget = StageLLMBudget(max_calls=0)
    prior = [
        StageArtifactPayload(
            stage_type="market",
            verdict=StageVerdict.BULL,
            confidence=60,
            buy_evidence=["KOSPI +1.5%"],
        ).model_dump()
    ]
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={},
        bundle_metadata={"prior_stages": prior},
    )

    stage = BullReducerStage(llm_client=llm, budget=budget)
    payload = await stage.run(ctx)

    llm.complete_json.assert_not_awaited()
    assert "KOSPI +1.5%" in (payload.summary or "")
    assert payload.confidence <= 50  # degraded
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/services/investment_stages/stages/test_bull_reducer.py -v`

- [ ] **Step 3: Implement**

```python
"""LLM bull reducer stage (ROB-279)."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.budget import BudgetExceeded, StageLLMBudget
from app.services.investment_stages.stages.base import StageContext

_logger = logging.getLogger(__name__)


class LLMJsonClient(Protocol):
    async def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...


_SYSTEM_PROMPT = (
    "당신은 자동 거래 시스템의 bull-case reducer 입니다. "
    "주어진 stage artifact 들에서 매수 근거만 추려 구조화된 JSON 으로 답하세요. "
    "근거가 없는 주장 금지. 모든 결론은 stage artifact 의 buy_evidence 에서만 도출되어야 합니다."
)


class BullReducerStage:
    stage_type = "bull_reducer"

    def __init__(self, *, llm_client: LLMJsonClient, budget: StageLLMBudget) -> None:
        self._llm = llm_client
        self._budget = budget

    async def run(self, context: StageContext) -> StageArtifactPayload:
        prior = list(context.bundle_metadata.get("prior_stages") or [])
        buy_lines: list[str] = []
        citations: list[StageCitation] = []
        for p in prior:
            buy_lines.extend(p.get("buy_evidence") or [])
            for c in p.get("cited_snapshots") or []:
                citations.append(StageCitation(**c))

        try:
            self._budget.consume("bull_reducer")
        except BudgetExceeded:
            _logger.info("bull_reducer: budget exhausted, degrading to deterministic")
            return StageArtifactPayload(
                stage_type=self.stage_type,
                verdict=StageVerdict.BULL if buy_lines else StageVerdict.NEUTRAL,
                confidence=40 if buy_lines else 20,
                summary="; ".join(buy_lines) or "no buy evidence",
                buy_evidence=buy_lines,
                cited_snapshots=citations,
                model_name=None,
            )

        user_prompt = (
            "다음 stage artifact 의 buy_evidence 를 종합하여 매수 측 논리를 요약하세요.\n"
            f"prior stages: {prior}\n"
            "응답은 {summary, confidence(0-100), key_points: list[str]} JSON 만."
        )
        response = await self._llm.complete_json(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            schema={
                "type": "object",
                "required": ["summary", "confidence", "key_points"],
                "properties": {
                    "summary": {"type": "string"},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    "key_points": {"type": "array", "items": {"type": "string"}},
                },
            },
        )

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=StageVerdict.BULL if buy_lines else StageVerdict.NEUTRAL,
            confidence=int(response.get("confidence", 50)),
            summary=str(response.get("summary", "")),
            key_points=list(response.get("key_points", [])),
            buy_evidence=buy_lines,
            cited_snapshots=citations,
            model_name="gemini",
            prompt_version="bull_reducer_v1",
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/investment_stages/stages/test_bull_reducer.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/bull_reducer.py tests/services/investment_stages/stages/test_bull_reducer.py
git commit -m "feat(rob-279): LLM bull_reducer with budget gating"
```

### Task 2.10: LLM stage — `bear_reducer`

**Files:**
- Create: `app/services/investment_stages/stages/bear_reducer.py`
- Create: `tests/services/investment_stages/stages/test_bear_reducer.py`

Symmetric to bull_reducer but reads `sell_evidence`. Tests are the same shape as Task 2.9 with `sell_evidence` replacing `buy_evidence` and verdict `BEAR` when present.

- [ ] **Step 1: Write failing test**

Copy `test_bull_reducer.py` shape, substituting `BearReducerStage`, `sell_evidence`, `StageVerdict.BEAR`.

- [ ] **Step 2: Verify fail; Step 3: Implement (mirror bull_reducer.py)**

Implementation is identical to Task 2.9 with these substitutions:
- class `BearReducerStage`, `stage_type = "bear_reducer"`
- read `sell_evidence` instead of `buy_evidence`
- verdict `BEAR` when sell_lines present, else `NEUTRAL`
- system prompt switches "매수" → "매도/위험" and "buy_evidence" → "sell_evidence"
- budget label `"bear_reducer"`, prompt_version `"bear_reducer_v1"`

- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

```bash
git commit -m "feat(rob-279): LLM bear_reducer with budget gating"
```

### Task 2.11: LLM stage — `risk_review`

**Files:**
- Create: `app/services/investment_stages/stages/risk_review.py`
- Create: `tests/services/investment_stages/stages/test_risk_review.py`

Reads bull_reducer + bear_reducer artifacts plus the watch_context and portfolio_journal artifacts. LLM emits final risk verdict; degrades to "review required" when budget exhausted.

- [ ] **Step 1: Write failing test**

```python
import uuid
from unittest.mock import AsyncMock

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.risk_review import RiskReviewStage


@pytest.mark.asyncio
async def test_risk_review_uses_bull_and_bear_artifacts():
    llm = AsyncMock()
    llm.complete_json.return_value = {
        "summary": "균형: bull > bear, 대형 포지션 진입은 보류",
        "confidence": 60,
        "risk_evidence": ["KOSPI 변동성 확대"],
    }
    budget = StageLLMBudget(max_calls=4)
    prior = [
        StageArtifactPayload(
            stage_type="bull_reducer", verdict=StageVerdict.BULL, confidence=70
        ).model_dump(),
        StageArtifactPayload(
            stage_type="bear_reducer", verdict=StageVerdict.BEAR, confidence=40
        ).model_dump(),
    ]
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={},
        bundle_metadata={"prior_stages": prior},
    )

    payload = await RiskReviewStage(llm_client=llm, budget=budget).run(ctx)

    assert payload.confidence == 60
    assert "KOSPI" in (payload.risk_evidence or [""])[0]
    llm.complete_json.assert_awaited_once()
```

- [ ] **Step 2-3: Implement** (same shape as bull_reducer.py; reads bull/bear prior, emits `risk_evidence`; budget label `"risk_review"`)

- [ ] **Step 4: Run tests; Step 5: Commit**

```bash
git commit -m "feat(rob-279): LLM risk_review reducer"
```

### Task 2.12: Wire stages into runner and expose prior_stages context

**Files:**
- Modify: `app/services/investment_stages/stage_runner.py` (extend `run()` to track artifacts and forward `prior_stages` to subsequent stages via mutable `bundle_metadata` copy)
- Create: `app/services/investment_stages/stages/registry.py`
- Create: `tests/services/investment_stages/test_stage_runner_integration.py`

- [ ] **Step 1: Write failing integration test**

```python
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stage_runner import StageRunner
from app.services.investment_stages.stages.registry import build_default_v1_stages


class _StubBundleRead:
    def __init__(self, bundle_uuid, items):
        self._uuid = bundle_uuid
        self._items = items

    async def get_bundle(self, *, bundle_uuid):
        return SimpleNamespace(
            bundle=SimpleNamespace(bundle_uuid=bundle_uuid, status="complete"),
            items=self._items,
        )


def _snap(kind, payload):
    return SimpleNamespace(
        snapshot=SimpleNamespace(
            snapshot_uuid=uuid.uuid4(),
            snapshot_kind=kind,
            payload_json=payload,
        )
    )


@pytest.mark.asyncio
async def test_default_v1_runs_all_8_stages_with_mock_llm(db_session):
    bundle_uuid = uuid.uuid4()
    items = [
        _snap("market", {"indices": {"KOSPI": {"change_percent": 1.2}}}),
        _snap("news", {"articles": [{"title": "good", "sentiment": "positive"}] * 4}),
        _snap("portfolio", {"buying_power_krw": 200000, "nav_krw": 1000000}),
        _snap("journal", {"entries": [{"symbol": "035420", "thesis": "tech"}]}),
        _snap("watch_context", {"active_alerts": [{"symbol": "035420", "condition": "below"}]}),
        _snap("candidate_universe", {"candidates": [{"symbol": "035420", "score": 8.0, "reason": "x"}]}),
    ]

    llm = AsyncMock()
    llm.complete_json.return_value = {
        "summary": "ok",
        "confidence": 60,
        "key_points": [],
        "risk_evidence": [],
    }
    budget = StageLLMBudget(max_calls=4)
    stages = build_default_v1_stages(llm_client=llm, budget=budget)

    runner = StageRunner(
        session=db_session,
        bundle_read_service=_StubBundleRead(bundle_uuid, items),
        stages=stages,
    )
    run = await runner.run(
        snapshot_bundle_uuid=bundle_uuid,
        market="kr",
        market_session="regular",
        account_scope="kis_live",
    )

    assert run.status == "completed"
    assert len(run.artifacts) == 8
    types = {a.stage_type for a in run.artifacts}
    assert types == {
        "market",
        "news",
        "portfolio_journal",
        "watch_context",
        "candidate_universe",
        "bull_reducer",
        "bear_reducer",
        "risk_review",
    }
    assert llm.complete_json.await_count == 3  # 3 reducers consumed budget
```

- [ ] **Step 2-3: Implement registry + extend runner**

`app/services/investment_stages/stages/registry.py`:

```python
"""Default v1 stage registry (ROB-279)."""

from __future__ import annotations

from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.base import Stage
from app.services.investment_stages.stages.bear_reducer import BearReducerStage
from app.services.investment_stages.stages.bull_reducer import BullReducerStage
from app.services.investment_stages.stages.candidate_universe import CandidateUniverseStage
from app.services.investment_stages.stages.market import MarketStage
from app.services.investment_stages.stages.news import NewsStage
from app.services.investment_stages.stages.portfolio_journal import PortfolioJournalStage
from app.services.investment_stages.stages.risk_review import RiskReviewStage
from app.services.investment_stages.stages.watch_context import WatchContextStage


def build_default_v1_stages(*, llm_client, budget: StageLLMBudget) -> list[Stage]:
    return [
        MarketStage(),
        NewsStage(),
        PortfolioJournalStage(),
        WatchContextStage(),
        CandidateUniverseStage(),
        BullReducerStage(llm_client=llm_client, budget=budget),
        BearReducerStage(llm_client=llm_client, budget=budget),
        RiskReviewStage(llm_client=llm_client, budget=budget),
    ]
```

Modify `app/services/investment_stages/stage_runner.py`:

In `run()`, change the stages loop to forward each emitted payload into the next stage's `prior_stages` metadata:

```python
prior_payloads: list[dict] = []
for stage in self._stages:
    stage_ctx = StageContext(
        bundle_uuid=ctx.bundle_uuid,
        snapshots_by_kind=ctx.snapshots_by_kind,
        bundle_metadata={**ctx.bundle_metadata, "prior_stages": list(prior_payloads)},
    )
    try:
        payload = await stage.run(stage_ctx)
    except UnavailableStageError as exc:
        # ... as before
    # persist + append to prior_payloads
    await self._repo.persist_artifact(run.run_uuid, payload)
    prior_payloads.append(payload.model_dump(mode="json"))
```

- [ ] **Step 4: Run integration test**

Run: `uv run pytest tests/services/investment_stages/test_stage_runner_integration.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/registry.py app/services/investment_stages/stage_runner.py tests/services/investment_stages/test_stage_runner_integration.py
git commit -m "feat(rob-279): wire v1 stages and propagate prior_stages"
```

---

## Phase 3 — Final composer + ingest integration

> Goal: stage artifact → final report items (action/watch/risk/no_action_note). 100% citation 강제. legacy `investment_report_generate_from_bundle` 경로는 `auto_compose=false` 일 때 변경 없이 보존.
> 위험: citation 누락된 LLM 출력 → caveat/omission 강제 누락.

### Task 3.1: Composer skeleton + citation enforcement

**Files:**
- Create: `app/services/investment_stages/composer.py`
- Create: `tests/services/investment_stages/test_composer.py`

- [ ] **Step 1: Write failing tests**

```python
import uuid
from unittest.mock import AsyncMock

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageCitation, StageVerdict
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.composer import FinalComposer


def _artifact(stage_type, verdict, cited=True):
    return StageArtifactPayload(
        stage_type=stage_type,
        verdict=verdict,
        confidence=60,
        summary=f"{stage_type} summary",
        buy_evidence=[f"{stage_type} buy"] if verdict == StageVerdict.BULL else [],
        sell_evidence=[f"{stage_type} sell"] if verdict == StageVerdict.BEAR else [],
        cited_snapshots=(
            [StageCitation(snapshot_uuid=uuid.uuid4(), snapshot_kind=stage_type)]
            if cited
            else []
        ),
    )


@pytest.mark.asyncio
async def test_composer_emits_action_item_when_bull_dominates():
    llm = AsyncMock()
    llm.complete_json.return_value = {
        "title": "매수 후보 검토",
        "items": [
            {
                "client_item_key": "candidate-035420",
                "item_kind": "action",
                "summary": "035420 매수 검토",
                "cited_stage_types": ["candidate_universe", "bull_reducer"],
            }
        ],
    }
    artifacts = [
        _artifact("market", StageVerdict.BULL),
        _artifact("candidate_universe", StageVerdict.BULL),
        _artifact("bull_reducer", StageVerdict.BULL),
        _artifact("bear_reducer", StageVerdict.NEUTRAL),
        _artifact("risk_review", StageVerdict.NEUTRAL),
    ]

    composer = FinalComposer(llm_client=llm, budget=StageLLMBudget(max_calls=4))
    output = await composer.compose(artifacts=artifacts)

    assert output.title.startswith("매수")
    assert len(output.items) == 1
    assert output.items[0].item_kind == "action"


@pytest.mark.asyncio
async def test_composer_strips_uncited_items():
    llm = AsyncMock()
    llm.complete_json.return_value = {
        "title": "테스트",
        "items": [
            {
                "client_item_key": "uncited",
                "item_kind": "action",
                "summary": "no citation",
                "cited_stage_types": ["unknown"],
            }
        ],
    }
    composer = FinalComposer(llm_client=llm, budget=StageLLMBudget(max_calls=4))
    output = await composer.compose(artifacts=[_artifact("market", StageVerdict.NEUTRAL, cited=False)])
    assert output.items == []  # uncited stripped
    assert any("no_action_note" in i.item_kind for i in output.fallback_items)
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/services/investment_stages/test_composer.py -v`

- [ ] **Step 3: Implement composer**

```python
"""Final composer — stage artifacts -> report items (ROB-279)."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Protocol

from app.schemas.investment_reports import IngestReportItem
from app.schemas.investment_stages import StageArtifactPayload
from app.services.investment_stages.budget import BudgetExceeded, StageLLMBudget

_logger = logging.getLogger(__name__)


class LLMJsonClient(Protocol):
    async def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...


@dataclasses.dataclass(frozen=True)
class ComposerOutput:
    title: str
    summary: str
    items: list[IngestReportItem]
    fallback_items: list[IngestReportItem]


_SYSTEM = (
    "당신은 자동 거래 시스템의 final composer 입니다. "
    "주어진 stage artifact 들로부터만 action/watch/risk 항목을 합성하세요. "
    "각 항목은 반드시 cited_stage_types 에 1개 이상 stage 를 인용해야 합니다. "
    "인용 불가능한 추정은 절대 생성하지 마세요."
)


class FinalComposer:
    def __init__(self, *, llm_client: LLMJsonClient, budget: StageLLMBudget) -> None:
        self._llm = llm_client
        self._budget = budget

    async def compose(self, *, artifacts: list[StageArtifactPayload]) -> ComposerOutput:
        try:
            self._budget.consume("final_composer")
            response = await self._llm.complete_json(
                system=_SYSTEM,
                user=self._build_user_prompt(artifacts),
                schema=self._schema(),
            )
        except BudgetExceeded:
            _logger.warning("composer: budget exhausted, returning no_action_note")
            return self._no_action_fallback(artifacts)

        cited_stage_types = {a.stage_type for a in artifacts if a.cited_snapshots}
        items: list[IngestReportItem] = []
        fallback: list[IngestReportItem] = []
        for raw in response.get("items", []):
            required = set(raw.get("cited_stage_types") or [])
            if not required or not (required & cited_stage_types):
                _logger.info("composer: dropping uncited item %s", raw.get("client_item_key"))
                continue
            items.append(self._to_ingest_item(raw))

        if not items:
            fallback.append(self._no_action_item(reason="composer produced no cited items"))

        return ComposerOutput(
            title=str(response.get("title", "auto-composed report")),
            summary=str(response.get("summary", "")),
            items=items,
            fallback_items=fallback,
        )

    def _build_user_prompt(self, artifacts: list[StageArtifactPayload]) -> str:
        return (
            "다음 stage artifact 들에서 action/watch/risk 항목을 합성하세요.\n"
            f"artifacts: {[a.model_dump(mode='json') for a in artifacts]}\n"
            "각 item.cited_stage_types 는 인용 가능한 stage_type 만 포함."
        )

    def _schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["title", "items"],
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["client_item_key", "item_kind", "summary", "cited_stage_types"],
                        "properties": {
                            "client_item_key": {"type": "string"},
                            "item_kind": {"type": "string", "enum": ["action", "watch", "risk"]},
                            "summary": {"type": "string"},
                            "cited_stage_types": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        }

    def _to_ingest_item(self, raw: dict[str, Any]) -> IngestReportItem:
        return IngestReportItem(
            client_item_key=raw["client_item_key"],
            item_kind=raw["item_kind"],
            operation="review",
            apply_policy="requires_user_approval",
            proposed_state={"summary": raw.get("summary", "")},
        )

    def _no_action_item(self, *, reason: str) -> IngestReportItem:
        return IngestReportItem(
            client_item_key="auto-no-action",
            item_kind="risk",
            operation="review",
            apply_policy="requires_user_approval",
            proposed_state={"summary": reason},
        )

    def _no_action_fallback(self, artifacts: list[StageArtifactPayload]) -> ComposerOutput:
        return ComposerOutput(
            title="자동 합성 보류",
            summary="LLM budget 소진으로 최종 합성이 진행되지 않았습니다.",
            items=[],
            fallback_items=[self._no_action_item(reason="LLM budget exhausted")],
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/investment_stages/test_composer.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/composer.py tests/services/investment_stages/test_composer.py
git commit -m "feat(rob-279): final composer with citation enforcement"
```

### Task 3.2: Extend ReportGenerationRequest with auto_compose flag

**Files:**
- Modify: `app/schemas/investment_reports.py` (locate `ReportGenerationRequest`, add `auto_compose: bool = False`)
- Create: `tests/schemas/test_report_generation_request_auto_compose.py`

- [ ] **Step 1: Failing test**

```python
from app.schemas.investment_reports import ReportGenerationRequest


def test_report_generation_request_defaults_auto_compose_false():
    req = ReportGenerationRequest(
        report_type="kr_action_v1",
        market="kr",
        kst_date="2026-05-20",
        items=[],
    )
    assert req.auto_compose is False


def test_report_generation_request_accepts_auto_compose_true():
    req = ReportGenerationRequest(
        report_type="kr_action_v1",
        market="kr",
        kst_date="2026-05-20",
        items=[],
        auto_compose=True,
    )
    assert req.auto_compose is True
```

- [ ] **Step 2: Verify fail**

Run: `uv run pytest tests/schemas/test_report_generation_request_auto_compose.py -v`
Expected: fail (field missing).

- [ ] **Step 3: Add field to `ReportGenerationRequest` in `app/schemas/investment_reports.py`**

Find the `ReportGenerationRequest` class and add:

```python
auto_compose: bool = Field(
    default=False,
    description=(
        "ROB-279: when True, items will be auto-composed from stage artifacts "
        "instead of using caller-supplied items. Legacy behavior preserved when False."
    ),
)
```

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/schemas/test_report_generation_request_auto_compose.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/investment_reports.py tests/schemas/test_report_generation_request_auto_compose.py
git commit -m "feat(rob-279): ReportGenerationRequest.auto_compose flag"
```

### Task 3.3: Wire stage runner + composer into generator

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py` (insert `auto_compose=True` branch between `_ensure_service.ensure()` and `classify_items()` call)
- Create: `tests/services/action_report/snapshot_backed/test_generator_auto_compose.py`

The branch behavior when `request.auto_compose is True`:

1. After `ensure_response = await self._ensure_service.ensure(...)`, run `StageRunner` with default v1 stages.
2. Pass stage artifacts to `FinalComposer.compose(...)`.
3. Replace `request.items` with composer output items (skip `classify_items()`).
4. Persist `run.run_uuid` to the eventual report via `metadata_json` on the run row referencing `report_uuid` (set after ingest).

- [ ] **Step 1: Write failing test using mock runner/composer**

```python
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.investment_reports import (
    IngestReportItem,
    ReportGenerationRequest,
)
from app.services.action_report.snapshot_backed.generator import (
    SnapshotBackedReportGenerator,
)


@pytest.mark.asyncio
async def test_generator_auto_compose_replaces_items_and_links_run(monkeypatch, db_session):
    bundle_uuid = uuid.uuid4()
    ensure_response = MagicMock(
        bundle_uuid=bundle_uuid,
        status="complete",
        missing_sources=[],
        freshness_summary={"overall": "fresh"},
        coverage_summary={},
    )
    ensure_service = AsyncMock(ensure=AsyncMock(return_value=ensure_response))

    runner_mock = MagicMock()
    runner_mock.run = AsyncMock(
        return_value=MagicMock(run_uuid=uuid.uuid4(), artifacts=[])
    )
    composer_mock = MagicMock()
    composer_mock.compose = AsyncMock(
        return_value=MagicMock(
            title="auto",
            summary="ok",
            items=[
                IngestReportItem(
                    client_item_key="x", item_kind="action", operation="review"
                )
            ],
            fallback_items=[],
        )
    )

    ingestion_mock = AsyncMock()
    ingestion_mock.ingest = AsyncMock(return_value=MagicMock(report_uuid=uuid.uuid4()))

    gen = SnapshotBackedReportGenerator(db_session)
    gen._ensure_service = ensure_service
    gen._ingestion_service = ingestion_mock
    monkeypatch.setattr(
        "app.services.action_report.snapshot_backed.generator.StageRunner",
        lambda **kw: runner_mock,
    )
    monkeypatch.setattr(
        "app.services.action_report.snapshot_backed.generator.FinalComposer",
        lambda **kw: composer_mock,
    )

    request = ReportGenerationRequest(
        report_type="kr_action_v1",
        market="kr",
        kst_date="2026-05-20",
        items=[],
        auto_compose=True,
    )
    response = await gen.generate(request)

    runner_mock.run.assert_awaited_once()
    composer_mock.compose.assert_awaited_once()
    ingestion_mock.ingest.assert_awaited_once()
    ingest_arg = ingestion_mock.ingest.await_args.args[0]
    assert len(ingest_arg.items) == 1
    assert ingest_arg.items[0].client_item_key == "x"
```

- [ ] **Step 2: Verify fail**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator_auto_compose.py -v`

- [ ] **Step 3: Implement the branch in generator.py**

Locate `generate()`; after `ensure_response = await self._ensure_service.ensure(...)` and before the existing classifier_context block, insert:

```python
if request.auto_compose:
    from app.services.investment_stages.budget import StageLLMBudget
    from app.services.investment_stages.composer import FinalComposer
    from app.services.investment_stages.stage_runner import StageRunner
    from app.services.investment_stages.stages.registry import build_default_v1_stages
    from app.services.investment_snapshots.read_service import SnapshotBundleReadService

    budget = StageLLMBudget(max_calls=4)
    llm_client = self._build_llm_client()  # see note below
    stages = build_default_v1_stages(llm_client=llm_client, budget=budget)
    runner = StageRunner(
        session=self._session,
        bundle_read_service=SnapshotBundleReadService(self._session),
        stages=stages,
    )
    stage_run = await runner.run(
        snapshot_bundle_uuid=ensure_response.bundle_uuid,
        market=request.market,
        market_session=request.market_session,
        account_scope=request.account_scope,
    )
    composer = FinalComposer(llm_client=llm_client, budget=budget)
    composer_output = await composer.compose(
        artifacts=[
            # rehydrate StageArtifactPayload from ORM artifact rows
            self._artifact_to_payload(a) for a in stage_run.artifacts
        ]
    )
    request = request.model_copy(
        update={
            "items": composer_output.items or composer_output.fallback_items,
            "title": composer_output.title,
        }
    )
    self._stage_run_uuid_for_metadata = stage_run.run_uuid
else:
    self._stage_run_uuid_for_metadata = None
```

Note: `self._build_llm_client()` and `self._artifact_to_payload(...)` are new helpers — implement them as thin adapters. `_build_llm_client()` returns an object with `complete_json(system, user, schema)` that delegates to existing Gemini wrapper plus `model_rate_limiter`. `_artifact_to_payload` converts ORM row to `StageArtifactPayload`.

Also: at the end of `generate()`, after `report = await self._ingestion_service.ingest(...)`, link the run to the report:

```python
if self._stage_run_uuid_for_metadata is not None:
    await self._repo_stages.link_run_to_report(
        run_uuid=self._stage_run_uuid_for_metadata,
        report_uuid=report.report_uuid,
    )
```

`link_run_to_report` writes `report_uuid` into `investment_stage_runs.metadata_json['report_uuid']`. Add this helper to `InvestmentStagesRepository`:

```python
async def link_run_to_report(self, *, run_uuid, report_uuid) -> None:
    run = await self._session.scalar(
        select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid)
    )
    if run is None:
        return
    metadata = dict(run.metadata_json or {})
    metadata["report_uuid"] = str(report_uuid)
    run.metadata_json = metadata
    await self._session.flush()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator_auto_compose.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/generator.py app/services/investment_stages/repository.py tests/services/action_report/snapshot_backed/test_generator_auto_compose.py
git commit -m "feat(rob-279): wire stage runner + composer into generator behind auto_compose flag"
```

### Task 3.4: Legacy-path regression test

**Files:**
- Create: `tests/services/action_report/snapshot_backed/test_generator_legacy_path.py`

Goal: prove that with `auto_compose=False` (default), nothing about the legacy path changes — no stage run is created.

- [ ] **Step 1: Write test**

```python
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.models.investment_stages import InvestmentStageRun
from app.schemas.investment_reports import IngestReportItem, ReportGenerationRequest
from app.services.action_report.snapshot_backed.generator import (
    SnapshotBackedReportGenerator,
)


@pytest.mark.asyncio
async def test_legacy_path_does_not_create_stage_run(db_session):
    bundle_uuid = uuid.uuid4()
    ensure_response = MagicMock(
        bundle_uuid=bundle_uuid, status="complete", missing_sources=[],
        freshness_summary={"overall": "fresh"}, coverage_summary={},
    )
    gen = SnapshotBackedReportGenerator(db_session)
    gen._ensure_service = AsyncMock(ensure=AsyncMock(return_value=ensure_response))
    gen._ingestion_service = AsyncMock(ingest=AsyncMock(return_value=MagicMock(report_uuid=uuid.uuid4())))

    request = ReportGenerationRequest(
        report_type="kr_action_v1",
        market="kr",
        kst_date="2026-05-20",
        items=[IngestReportItem(client_item_key="legacy-1", item_kind="action")],
        auto_compose=False,
    )
    await gen.generate(request)

    rows = list((await db_session.scalars(select(InvestmentStageRun))).all())
    assert rows == []
```

- [ ] **Step 2-4: Run test (should pass without further changes)**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator_legacy_path.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/services/action_report/snapshot_backed/test_generator_legacy_path.py
git commit -m "test(rob-279): legacy auto_compose=false path does not create stage run"
```

---

## Phase 4 — API / MCP surface

> Goal: read-only HTTP endpoints for both **report-scoped** (via bundle membership) and **run-scoped** (blocked-report diagnostic) stage artifact access.
> 위험: 권한/라우터 prefix 충돌, MCP 도구 등록 누락.

### Task 4.1: Router for stage runs

**Files:**
- Create: `app/routers/investment_stage_runs.py`
- Create: `tests/routers/test_investment_stage_runs.py`
- Modify: `app/main.py` (include router)

Endpoints:
- `GET /trading/api/investment-stage-runs/{run_uuid}` → `StageRunBundleResponse` (run summary + artifacts)
- `GET /trading/api/investment-reports/{report_uuid}/stage-artifacts` → list of artifacts via bundle membership (resolves `report.snapshot_bundle_uuid` → all runs for that bundle → all artifacts; filters to runs linked to this report via `metadata_json['report_uuid']`)

- [ ] **Step 1: Write failing test**

```python
import uuid

import pytest
from httpx import AsyncClient

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.repository import InvestmentStagesRepository


@pytest.mark.asyncio
async def test_get_stage_run_returns_artifacts(db_session, async_client: AsyncClient):
    repo = InvestmentStagesRepository(db_session)
    run = await repo.create_run(snapshot_bundle_uuid=uuid.uuid4(), market="kr")
    await repo.persist_artifact(
        run.run_uuid,
        StageArtifactPayload(stage_type="market", verdict=StageVerdict.BULL, confidence=70),
    )
    await db_session.commit()

    res = await async_client.get(f"/trading/api/investment-stage-runs/{run.run_uuid}")
    assert res.status_code == 200
    body = res.json()
    assert body["run"]["run_uuid"] == str(run.run_uuid)
    assert len(body["artifacts"]) == 1
    assert body["artifacts"][0]["stage_type"] == "market"


@pytest.mark.asyncio
async def test_get_stage_run_404_when_missing(async_client: AsyncClient):
    res = await async_client.get(f"/trading/api/investment-stage-runs/{uuid.uuid4()}")
    assert res.status_code == 404
```

- [ ] **Step 2: Verify fail**

Run: `uv run pytest tests/routers/test_investment_stage_runs.py -v`

- [ ] **Step 3: Implement router**

```python
"""Read-only router for investment stage runs (ROB-279)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_authenticated_user, get_db
from app.models.users import User
from app.services.investment_stages.query_service import StageRunQueryService

router = APIRouter(tags=["investment-stage-runs"])


@router.get("/trading/api/investment-stage-runs/{run_uuid}")
async def get_stage_run(
    run_uuid: uuid.UUID,
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    svc = StageRunQueryService(db)
    result = await svc.get_run_with_artifacts(run_uuid)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="stage run not found")

    return {
        "run": {
            "run_uuid": str(result.run.run_uuid),
            "snapshot_bundle_uuid": str(result.run.snapshot_bundle_uuid),
            "market": result.run.market,
            "market_session": result.run.market_session,
            "account_scope": result.run.account_scope,
            "policy_version": result.run.policy_version,
            "generator_version": result.run.generator_version,
            "status": result.run.status,
            "started_at": result.run.started_at.isoformat(),
            "completed_at": result.run.completed_at.isoformat() if result.run.completed_at else None,
            "metadata_json": result.run.metadata_json,
        },
        "artifacts": [
            {
                "artifact_uuid": str(a.artifact_uuid),
                "stage_type": a.stage_type,
                "verdict": a.verdict,
                "confidence": a.confidence,
                "summary": a.summary,
                "key_points": a.key_points,
                "buy_evidence": a.buy_evidence,
                "sell_evidence": a.sell_evidence,
                "risk_evidence": a.risk_evidence,
                "missing_data": a.missing_data,
                "cited_snapshot_uuids": [str(s) for s in a.cited_snapshot_uuids],
                "freshness_summary": a.freshness_summary,
                "model_name": a.model_name,
                "prompt_version": a.prompt_version,
                "created_at": a.created_at.isoformat(),
            }
            for a in result.artifacts
        ],
    }
```

- [ ] **Step 4: Wire into main.py**

In `app/main.py` add:
```python
from app.routers import investment_stage_runs
app.include_router(investment_stage_runs.router)
```

- [ ] **Step 5: Run tests + commit**

Run: `uv run pytest tests/routers/test_investment_stage_runs.py -v`
Expected: 2 passed.

```bash
git add app/routers/investment_stage_runs.py app/main.py tests/routers/test_investment_stage_runs.py
git commit -m "feat(rob-279): GET /trading/api/investment-stage-runs/{run_uuid}"
```

### Task 4.2: Report-scoped stage artifact endpoint

**Files:**
- Modify: `app/routers/investment_stage_runs.py` (add report-scoped route)
- Modify: `tests/routers/test_investment_stage_runs.py`

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_get_stage_artifacts_by_report_uuid(db_session, async_client):
    # 1) Create stage run + artifact
    # 2) Insert minimal InvestmentReport row with same snapshot_bundle_uuid + link via run.metadata_json
    # 3) GET /trading/api/investment-reports/{report_uuid}/stage-artifacts
    # 4) Assert artifacts returned
    ...  # fixture details elided; mirror Task 4.1 test
```

- [ ] **Step 2-3: Implement**

Add to router:

```python
@router.get("/trading/api/investment-reports/{report_uuid}/stage-artifacts")
async def list_stage_artifacts_for_report(
    report_uuid: uuid.UUID,
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    # 1. resolve report → snapshot_bundle_uuid
    # 2. list_runs_for_bundle (filter to runs whose metadata_json->>report_uuid matches)
    # 3. flatten artifacts
    report_repo = InvestmentReportsRepository(db)
    report = await report_repo.get_by_uuid(report_uuid)
    if report is None or report.snapshot_bundle_uuid is None:
        raise HTTPException(404, "report or snapshot bundle not found")

    svc = StageRunQueryService(db)
    runs = await svc.list_runs_for_bundle(report.snapshot_bundle_uuid)
    runs_for_report = [
        r for r in runs
        if (r.metadata_json or {}).get("report_uuid") == str(report_uuid)
    ]
    artifacts = []
    for r in runs_for_report:
        result = await svc.get_run_with_artifacts(r.run_uuid)
        if result:
            artifacts.extend(result.artifacts)

    return {"report_uuid": str(report_uuid), "artifacts": [...]}  # serialize as in Task 4.1
```

- [ ] **Step 4: Run tests + Step 5: Commit**

```bash
git commit -m "feat(rob-279): GET /trading/api/investment-reports/{uuid}/stage-artifacts"
```

---

## Phase 5 — UI: 중간 분석 panel

> Goal: `/invest/reports/:reportUuid` 에 "중간 분석" 섹션 추가. `ReportSnapshotEvidencePanel` 의 lazy-load 패턴 답습.
> 위험: 기존 컴포넌트 회귀.

### Task 5.1: Types + API client

**Files:**
- Modify: `frontend/invest/src/types/investmentReports.ts` (add `StageRun`, `StageArtifact`, `StageVerdict`)
- Create: `frontend/invest/src/api/investmentStages.ts`
- Create: `frontend/invest/src/__tests__/investmentStagesApi.test.ts`

- [ ] **Step 1: Add types**

Append to `frontend/invest/src/types/investmentReports.ts`:

```typescript
export type StageVerdict = "bull" | "bear" | "neutral" | "unavailable";

export type StageType =
  | "market"
  | "news"
  | "portfolio_journal"
  | "watch_context"
  | "candidate_universe"
  | "bull_reducer"
  | "bear_reducer"
  | "risk_review";

export interface StageArtifact {
  artifactUuid: string;
  stageType: StageType;
  verdict: StageVerdict;
  confidence: number;
  summary: string | null;
  keyPoints: string[];
  buyEvidence: string[];
  sellEvidence: string[];
  riskEvidence: string[];
  missingData: string[];
  citedSnapshotUuids: string[];
  modelName: string | null;
  promptVersion: string | null;
  createdAt: string;
}

export interface StageRun {
  runUuid: string;
  snapshotBundleUuid: string;
  market: string;
  marketSession: string | null;
  status: "running" | "completed" | "failed" | "blocked";
  startedAt: string;
  completedAt: string | null;
}

export interface ReportStageArtifactsResponse {
  reportUuid: string;
  artifacts: StageArtifact[];
}
```

- [ ] **Step 2: Write failing test**

```typescript
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fetchReportStageArtifacts } from "../api/investmentStages";

describe("fetchReportStageArtifacts", () => {
  beforeEach(() => {
    global.fetch = vi.fn();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("normalizes snake_case to camelCase", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        report_uuid: "r1",
        artifacts: [
          {
            artifact_uuid: "a1",
            stage_type: "market",
            verdict: "bull",
            confidence: 70,
            summary: "ok",
            key_points: ["x"],
            buy_evidence: [],
            sell_evidence: [],
            risk_evidence: [],
            missing_data: [],
            cited_snapshot_uuids: ["s1"],
            model_name: null,
            prompt_version: null,
            created_at: "2026-05-20T00:00:00Z",
          },
        ],
      }),
    });

    const result = await fetchReportStageArtifacts("r1");
    expect(result.artifacts).toHaveLength(1);
    expect(result.artifacts[0].stageType).toBe("market");
    expect(result.artifacts[0].citedSnapshotUuids).toEqual(["s1"]);
  });
});
```

- [ ] **Step 3: Implement client**

```typescript
import type { ReportStageArtifactsResponse, StageArtifact } from "../types/investmentReports";

const STAGE_ARTIFACTS_ENDPOINT = (reportUuid: string) =>
  `/invest/api/investment-reports/${encodeURIComponent(reportUuid)}/stage-artifacts`;

async function readJson<T>(endpoint: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(endpoint, { credentials: "include", signal });
  if (!res.ok) throw new Error(`${endpoint} ${res.status}`);
  return res.json();
}

function normalizeArtifact(raw: any): StageArtifact {
  return {
    artifactUuid: String(raw.artifact_uuid),
    stageType: raw.stage_type,
    verdict: raw.verdict,
    confidence: Number(raw.confidence ?? 0),
    summary: raw.summary ?? null,
    keyPoints: Array.isArray(raw.key_points) ? raw.key_points : [],
    buyEvidence: Array.isArray(raw.buy_evidence) ? raw.buy_evidence : [],
    sellEvidence: Array.isArray(raw.sell_evidence) ? raw.sell_evidence : [],
    riskEvidence: Array.isArray(raw.risk_evidence) ? raw.risk_evidence : [],
    missingData: Array.isArray(raw.missing_data) ? raw.missing_data : [],
    citedSnapshotUuids: Array.isArray(raw.cited_snapshot_uuids)
      ? raw.cited_snapshot_uuids.map(String)
      : [],
    modelName: raw.model_name ?? null,
    promptVersion: raw.prompt_version ?? null,
    createdAt: String(raw.created_at),
  };
}

export async function fetchReportStageArtifacts(
  reportUuid: string,
  signal?: AbortSignal,
): Promise<ReportStageArtifactsResponse> {
  const raw = await readJson<any>(STAGE_ARTIFACTS_ENDPOINT(reportUuid), signal);
  return {
    reportUuid: String(raw.report_uuid),
    artifacts: Array.isArray(raw.artifacts) ? raw.artifacts.map(normalizeArtifact) : [],
  };
}
```

- [ ] **Step 4: Run test**

Run: `cd frontend/invest && npx vitest run investmentStagesApi`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/types/investmentReports.ts frontend/invest/src/api/investmentStages.ts frontend/invest/src/__tests__/investmentStagesApi.test.ts
git commit -m "feat(rob-279): frontend types + API client for stage artifacts"
```

### Task 5.2: useReportStageArtifacts hook

**Files:**
- Create: `frontend/invest/src/hooks/useReportStageArtifacts.ts`
- Create: `frontend/invest/src/__tests__/useReportStageArtifacts.test.ts`

Mirror `useReportSnapshotBundle` shape: `{status, artifacts, error, reload}` with AbortController cleanup.

- [ ] **Step 1: Write failing test**

```typescript
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useReportStageArtifacts } from "../hooks/useReportStageArtifacts";

describe("useReportStageArtifacts", () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ report_uuid: "r1", artifacts: [] }),
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("fetches artifacts on mount", async () => {
    const { result } = renderHook(() => useReportStageArtifacts("r1"));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.artifacts).toEqual([]);
  });
});
```

- [ ] **Step 2-3: Implement** (mirror `useReportSnapshotBundle.ts` line by line, swap API call)
- [ ] **Step 4: Run test; Step 5: Commit**

```bash
git commit -m "feat(rob-279): useReportStageArtifacts hook"
```

### Task 5.3: StageArtifactCard component

**Files:**
- Create: `frontend/invest/src/components/investment-reports/StageArtifactCard.tsx`
- Create: `frontend/invest/src/components/investment-reports/stageLabels.ts`
- Create: `frontend/invest/src/__tests__/StageArtifactCard.test.tsx`

Renders one stage: badge with verdict (colored), confidence bar, summary, key_points list, missing_data chips, cited_snapshot_uuids count.

- [ ] **Step 1: Failing test**

```typescript
import { render, screen } from "@testing-library/react";
import { StageArtifactCard } from "../components/investment-reports/StageArtifactCard";

const baseArtifact = {
  artifactUuid: "a1",
  stageType: "market" as const,
  verdict: "bull" as const,
  confidence: 72,
  summary: "KOSPI 1.5%",
  keyPoints: ["KOSPI +1.5%"],
  buyEvidence: [],
  sellEvidence: [],
  riskEvidence: [],
  missingData: [],
  citedSnapshotUuids: ["s1", "s2"],
  modelName: null,
  promptVersion: null,
  createdAt: "2026-05-20T00:00:00Z",
};

test("renders verdict label and confidence", () => {
  render(<StageArtifactCard artifact={baseArtifact} />);
  expect(screen.getByText(/매수/)).toBeInTheDocument();
  expect(screen.getByText(/72/)).toBeInTheDocument();
  expect(screen.getByText(/KOSPI \+1\.5%/)).toBeInTheDocument();
  expect(screen.getByText(/스냅샷 2개/)).toBeInTheDocument();
});

test("flags missing_data", () => {
  render(
    <StageArtifactCard
      artifact={{ ...baseArtifact, missingData: ["news_articles"] }}
    />,
  );
  expect(screen.getByText(/누락 데이터/)).toBeInTheDocument();
});
```

- [ ] **Step 2-3: Implement**

`stageLabels.ts`:

```typescript
import type { StageType, StageVerdict } from "../../types/investmentReports";

export const STAGE_TYPE_LABELS: Record<StageType, string> = {
  market: "시장",
  news: "뉴스",
  portfolio_journal: "포트폴리오·저널",
  watch_context: "와치 컨텍스트",
  candidate_universe: "후보 종목",
  bull_reducer: "매수 측 요약",
  bear_reducer: "매도/위험 측 요약",
  risk_review: "리스크 리뷰",
};

export const VERDICT_LABELS: Record<StageVerdict, string> = {
  bull: "매수 측",
  bear: "매도 측",
  neutral: "중립",
  unavailable: "확인 불가",
};
```

`StageArtifactCard.tsx`:

```tsx
import type { StageArtifact } from "../../types/investmentReports";
import { STAGE_TYPE_LABELS, VERDICT_LABELS } from "./stageLabels";

const VERDICT_COLORS = {
  bull: "#2e7d32",
  bear: "#c62828",
  neutral: "#5c6b73",
  unavailable: "#9e9e9e",
} as const;

export function StageArtifactCard({ artifact }: { artifact: StageArtifact }) {
  return (
    <article
      style={{
        border: "1px solid #d0d7de",
        borderRadius: 8,
        padding: 14,
        display: "grid",
        gap: 8,
      }}
      data-testid={`stage-card-${artifact.stageType}`}
    >
      <header style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
        <strong>{STAGE_TYPE_LABELS[artifact.stageType]}</strong>
        <span style={{ color: VERDICT_COLORS[artifact.verdict], fontWeight: 600 }}>
          {VERDICT_LABELS[artifact.verdict]}
        </span>
        <span style={{ marginLeft: "auto", fontSize: 12, color: "#5c6b73" }}>
          신뢰도 {artifact.confidence}
        </span>
      </header>
      {artifact.summary ? <p style={{ margin: 0 }}>{artifact.summary}</p> : null}
      {artifact.keyPoints.length > 0 ? (
        <ul style={{ margin: 0, paddingLeft: 16 }}>
          {artifact.keyPoints.map((kp, i) => (
            <li key={i}>{kp}</li>
          ))}
        </ul>
      ) : null}
      {artifact.missingData.length > 0 ? (
        <div style={{ fontSize: 12, color: "#9a3324" }}>
          누락 데이터: {artifact.missingData.join(", ")}
        </div>
      ) : null}
      <footer style={{ fontSize: 11, color: "#5c6b73" }}>
        근거 스냅샷 {artifact.citedSnapshotUuids.length}개
        {artifact.modelName ? ` · ${artifact.modelName}` : ""}
      </footer>
    </article>
  );
}
```

- [ ] **Step 4: Run test; Step 5: Commit**

```bash
git commit -m "feat(rob-279): StageArtifactCard component"
```

### Task 5.4: IntermediateAnalysisPanel + mount

**Files:**
- Create: `frontend/invest/src/components/investment-reports/IntermediateAnalysisPanel.tsx`
- Modify: `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx`
- Create: `frontend/invest/src/__tests__/IntermediateAnalysisPanel.test.tsx`

- [ ] **Step 1: Failing test**

```typescript
import { render, screen, waitFor } from "@testing-library/react";
import { vi, beforeEach, afterEach, describe, it, expect } from "vitest";
import { IntermediateAnalysisPanel } from "../components/investment-reports/IntermediateAnalysisPanel";

describe("IntermediateAnalysisPanel", () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        report_uuid: "r1",
        artifacts: [
          {
            artifact_uuid: "a1",
            stage_type: "market",
            verdict: "bull",
            confidence: 70,
            summary: "ok",
            key_points: [],
            buy_evidence: [],
            sell_evidence: [],
            risk_evidence: [],
            missing_data: [],
            cited_snapshot_uuids: [],
            model_name: null,
            prompt_version: null,
            created_at: "2026-05-20T00:00:00Z",
          },
        ],
      }),
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("renders all stage cards lazily after fetch", async () => {
    render(<IntermediateAnalysisPanel reportUuid="r1" />);
    expect(screen.getByText(/중간 분석 로드 중/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("stage-card-market")).toBeInTheDocument(),
    );
  });

  it("renders empty-state when no artifacts", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ report_uuid: "r1", artifacts: [] }),
    });
    render(<IntermediateAnalysisPanel reportUuid="r1" />);
    await waitFor(() =>
      expect(screen.getByText(/중간 분석 결과가 없습니다/)).toBeInTheDocument(),
    );
  });
});
```

- [ ] **Step 2-3: Implement**

```tsx
import { useReportStageArtifacts } from "../../hooks/useReportStageArtifacts";
import { StageArtifactCard } from "./StageArtifactCard";

export function IntermediateAnalysisPanel({ reportUuid }: { reportUuid: string }) {
  const { status, artifacts, error, reload } = useReportStageArtifacts(reportUuid);

  if (status === "loading") {
    return <div>중간 분석 로드 중…</div>;
  }
  if (status === "error") {
    return (
      <div>
        중간 분석 조회 실패: {error}
        <button onClick={reload}>다시 시도</button>
      </div>
    );
  }
  if (artifacts.length === 0) {
    return (
      <div style={{ color: "#5c6b73" }}>
        중간 분석 결과가 없습니다 (legacy 또는 auto_compose=false 리포트).
      </div>
    );
  }

  return (
    <section style={{ display: "grid", gap: 10 }}>
      <h2 style={{ margin: 0 }}>중간 분석</h2>
      {artifacts.map((a) => (
        <StageArtifactCard key={a.artifactUuid} artifact={a} />
      ))}
    </section>
  );
}
```

In `InvestmentReportBundleContent.tsx`, find the section after `ReportSnapshotEvidencePanel` mount (around line 470) and add:

```tsx
<IntermediateAnalysisPanel reportUuid={bundle.report.reportUuid} />
```

Plus import: `import { IntermediateAnalysisPanel } from "./IntermediateAnalysisPanel";`

- [ ] **Step 4: Run tests**

Run: `cd frontend/invest && npx vitest run IntermediateAnalysisPanel`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/components/investment-reports/IntermediateAnalysisPanel.tsx frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx frontend/invest/src/__tests__/IntermediateAnalysisPanel.test.tsx
git commit -m "feat(rob-279): mount IntermediateAnalysisPanel on report detail"
```

---

## Phase 6 — Integration tests + blocked-report smoke

> Goal: end-to-end integration + stale-gate blocked-report diagnostic test + legacy compatibility.

### Task 6.1: End-to-end happy path

**Files:**
- Create: `tests/integration/test_rob279_e2e_auto_compose.py`

Spins up: real bundle (via factory) → real stage runner with mocked LLM → real composer → real ingestion. Asserts report row exists with snapshot_bundle_uuid + linked stage run.

- [ ] **Step 1-3: Write + implement test**
- [ ] **Step 4: Run integration test**

Run: `uv run pytest tests/integration/test_rob279_e2e_auto_compose.py -v -m integration`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git commit -m "test(rob-279): e2e auto_compose integration"
```

### Task 6.2: Blocked-report diagnostic test

**Files:**
- Create: `tests/integration/test_rob279_stale_gate_blocked.py`

Setup: bundle with `freshness_summary.overall = "hard_stale"` + `auto_compose=true` + `request.status = "published"`. Expected: stale gate rejects the ingest, but `GET /trading/api/investment-stage-runs/{run_uuid}` still returns artifacts.

- [ ] **Step 1-3: Write + implement**
- [ ] **Step 4: Run test**

Run: `uv run pytest tests/integration/test_rob279_stale_gate_blocked.py -v -m integration`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git commit -m "test(rob-279): blocked-report stage diagnostic remains queryable"
```

### Task 6.3: Run full backend + frontend test suite

- [ ] **Step 1: Run all backend tests**

Run: `make test`
Expected: all pass; new ROB-279 tests included. Triage any pre-existing failures and document them in the PR.

- [ ] **Step 2: Run frontend tests**

Run: `cd frontend/invest && npm test -- --run`
Expected: all pass.

- [ ] **Step 3: Run lint**

Run: `make lint`
Expected: clean (or pre-existing only).

- [ ] **Step 4: Manual UI smoke**

Run: `make dev`
Then in browser open `/invest/reports/<existing-report-uuid>` → confirm "중간 분석" section renders (empty state for legacy reports is OK).

- [ ] **Step 5: Final commit + PR**

```bash
git log --oneline main..HEAD   # confirm all commits
gh pr create --title "feat(rob-279): staged snapshot-backed report generation pipeline" --body "$(cat <<'EOF'
## Summary
- Adds investment_stage_runs / investment_stage_artifacts tables
- 8 v1 stages (5 deterministic + 3 LLM reducers) + final composer behind auto_compose=true flag
- Run-scoped diagnostic API for stale-gate blocked reports
- /invest/reports IntermediateAnalysisPanel mount

## Test plan
- [x] Phase 1-6 unit tests pass
- [x] e2e integration green
- [x] stale-gate blocked-report still queryable
- [x] legacy auto_compose=false unchanged

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**1. Spec coverage:**

| Spec item | Plan task |
|---|---|
| `investment_stage_runs` / `_artifacts` 명명 | Task 1.1, 1.2 |
| append-only invariant | Task 1.4 (`AppendOnlyViolation`) |
| 8 v1 stages | Tasks 2.4–2.11 |
| LLM budget cap (4 calls) | Task 2.3 + 2.9/2.10/2.11 + 3.1 |
| `model_rate_limiter` 경유 | Task 3.3 `_build_llm_client()` adapter note |
| run-scoped 진단 라우트 | Task 4.1 |
| report-scoped artifact API | Task 4.2 |
| stale gate blocked-report diagnostic | Task 6.2 |
| 100% citation 강제 | Task 3.1 (composer strips uncited) |
| legacy compat | Task 3.4 |
| 중간 분석 UI 탭 | Tasks 5.1–5.4 |

**2. Placeholder scan:** No "TODO", "TBD", "similar to Task N" references that omit code. Bear/Risk reducer tasks (2.10, 2.11) explicitly call out "mirror Task 2.9 with substitutions" — acceptable because the substitutions are listed concretely.

**3. Type consistency:** `StageArtifactPayload` shape in Pydantic (Task 1.3) matches ORM `InvestmentStageArtifact` columns (Task 1.2) and frontend `StageArtifact` interface (Task 5.1). `StageVerdict` enum values (`bull|bear|neutral|unavailable`) consistent across DB CHECK, Pydantic enum, and TS literal.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-20-rob-279-staged-snapshot-backed-reports.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
