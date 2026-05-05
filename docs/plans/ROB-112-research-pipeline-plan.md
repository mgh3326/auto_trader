# ROB-112 — Research Pipeline 단계별 분석/저장 워크플로우 통합 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear:** [ROB-112](https://linear.app/mgh3326/issue/ROB-112/auto-trader-research-pipeline-단계별-분석저장-워크플로우-통합)
**Branch:** `feature/ROB-112-research-pipeline-stage-workflow`
**Status:** Plan only. **No code changes until reviewed.**

---

**Goal:** 종목별 deep research 를 Market / News / Fundamentals / Social 4개의 독립 stage + Summary 로 분리해 append-only DB 시계열로 축적하고, Summary 가 어떤 stage row 를 인용했는지 `summary_stage_links` 로 명시한다. 기존 `StockAnalysisResult` 와 MCP `analyze_stock` 계약은 dual-write + feature flag 로 호환을 유지한다.

**Architecture:**
- 기존 `ResearchRun` (시장/스캔 단위 후보 생성) 은 그대로 두고, 종목별 `ResearchSession` 레이어를 새로 추가한다. 각 stage analyzer 는 서로의 출력을 보지 않고(`독립`), Summary 단계만 latest stage row 들을 모아 bull/bear debate 를 수행한다.
- 기존 `Analyzer` (`app/analysis/`) 와 `tradingagents_research_service.py` 는 advisory layer 로 보존하고, 새 pipeline 은 `app/analysis/stages/`, `app/analysis/pipeline.py`, `app/analysis/debate.py` 로 추가한다.
- Summary 가 finalize 되면 `RESEARCH_PIPELINE_DUAL_WRITE_ENABLED` 가 켜진 경우에만 legacy `StockAnalysisResult` row 를 insert. MCP `analyze_stock` 은 `RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED` 가 켜졌고 dry-run 검증을 통과한 경우에만 새 pipeline 경로를 거치며, 응답 schema 는 항상 동일.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async, Alembic, Pydantic v2, FastAPI, MCP (existing tooling), pytest + pytest-asyncio (`unit`/`integration` markers), React + Vite (frontend, 선택 사항).

**Non-goals (CLAUDE.md / 이슈 본문 재확인):**
- broker order place / cancel / modify (KIS / Upbit / Alpaca) 금지
- watch alert / order intent / scheduler mutation 금지
- 기존 `StockAnalysisResult` 행 bulk update / delete / 자연어 backfill 금지
- TradingAgents 를 새 pipeline 으로 대체하지 않음 (advisory-only 유지)
- 기존 `StockAnalysisResult` 읽기 경로 제거 금지

---

## File Structure

| Layer | Path | Responsibility |
|---|---|---|
| ORM | `app/models/research_pipeline.py` (NEW) | `ResearchSession`, `StageAnalysis`, `ResearchSummary`, `SummaryStageLink`, `UserResearchNote` |
| ORM index | `app/models/__init__.py` (MODIFY) | re-export new models |
| Migration | `alembic/versions/<hash>_add_research_pipeline_tables.py` (NEW) | create 5 tables + indexes + check constraints |
| Schemas | `app/schemas/research_pipeline.py` (NEW) | `MarketSignals`, `NewsSignals`, `FundamentalsSignals`, `SocialSignals`, `SourceFreshness`, `StageVerdict`, `SummaryDecision`, `BullBearArgument` |
| Stage base | `app/analysis/stages/__init__.py` (NEW) | exports |
| Stage base | `app/analysis/stages/base.py` (NEW) | `BaseStageAnalyzer` ABC + DB write helper + freshness utils |
| Market | `app/analysis/stages/market_stage.py` (NEW) | OHLCV / quote / indicator → `MarketSignals` |
| News | `app/analysis/stages/news_stage.py` (NEW) | recent news headlines → `NewsSignals` |
| Fundamentals | `app/analysis/stages/fundamentals_stage.py` (NEW) | PER/PBR/시총/peer → `FundamentalsSignals` |
| Social | `app/analysis/stages/social_stage.py` (NEW) | placeholder row, `verdict='unavailable'`, `confidence=0` |
| Debate | `app/analysis/debate.py` (NEW) | latest stage rows → bull/bear → `SummaryDecision` |
| Pipeline | `app/analysis/pipeline.py` (NEW) | orchestrator: create session, run stages in parallel, run summary, dual-write |
| Service | `app/services/research_pipeline_service.py` (NEW) | DB-facing wrapper used by MCP & router (sole writer) |
| Adapter | `app/services/legacy_stock_analysis_adapter.py` (NEW) | summary → `StockAnalysisResult` mapping (dual-write) |
| Config | `app/core/config.py` (MODIFY) | add 3 feature flags |
| MCP | `app/mcp_server/tooling/analysis_analyze.py` (MODIFY) | feature-flagged dispatch to new pipeline; response schema unchanged |
| MCP | `app/mcp_server/tooling/research_pipeline_read.py` (NEW) | read-only MCP tools: `research_session_get`, `research_session_list_recent`, `stage_analysis_get`, `research_summary_get` |
| MCP | `app/mcp_server/tooling/analysis_registration.py` (MODIFY) | register new read tools |
| Router | `app/routers/research_pipeline.py` (NEW) | GET `/api/research-pipeline/sessions/...`, GET `/api/research-pipeline/sessions/{id}/stages`, GET `/api/research-pipeline/sessions/{id}/summary` |
| Frontend | `frontend/trading-decision/src/api/researchPipeline.ts` (NEW) | typed client |
| Frontend | `frontend/trading-decision/src/pages/ResearchSessionPage.tsx` (NEW, 선택) | 5-tab read-only view |
| Frontend | `frontend/trading-decision/src/pages/ResearchSessionPage.module.css` (NEW, 선택) | styling |
| Tests | `tests/models/test_research_pipeline_models.py` (NEW) | model shape + constraints |
| Tests | `tests/schemas/test_research_pipeline_schemas.py` (NEW) | Pydantic validation |
| Tests | `tests/analysis/stages/test_*_stage.py` (NEW × 4) | each stage analyzer |
| Tests | `tests/analysis/test_debate.py` (NEW) | summary citation logic |
| Tests | `tests/analysis/test_pipeline.py` (NEW) | end-to-end orchestrator (mocked stage analyzers) |
| Tests | `tests/services/test_legacy_stock_analysis_adapter.py` (NEW) | dual-write mapping |
| Tests | `tests/mcp_server/test_analyze_stock_pipeline_compat.py` (NEW) | response schema unchanged + flag fallback |
| Tests | `tests/mcp_server/test_research_pipeline_read_tools.py` (NEW) | read-only MCP tools |
| Tests | `tests/services/test_research_pipeline_safety.py` (NEW) | importing pipeline does NOT load broker / watch / order_intent / scheduler modules |
| Runbook | `docs/runbooks/research-pipeline.md` (NEW) | feature flag matrix, rollback steps, dual-write toggle, observability |

---

## Sequencing Notes

- Tasks 1 → 12 must run in order. Tasks 4 / 5 / 6 / 7 (stage analyzers) are independent of each other once Task 3 lands.
- Task 13 (frontend) is **deferred-allowed**: if the backend tasks consume the timebox, leave a clear handoff note (Task 14) and ship backend-only in this PR. The Linear issue explicitly permits this.
- Each task ends with a single commit per the `superpowers:test-driven-development` rhythm: red → green → refactor → commit.

---

## Task 1: ORM models + Alembic migration for new pipeline tables

**Files:**
- Create: `app/models/research_pipeline.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/<auto-hash>_add_research_pipeline_tables.py` (via `alembic revision --autogenerate`)
- Test: `tests/models/test_research_pipeline_models.py`

**Schema decisions (locked):**
- All identifiers use `Integer` PK (matches `StockAnalysisResult`, `ResearchRun`).
- `stage_analysis` is **append-only**. No `superseded_by` column. Latest row per `(session_id, stage_type)` is computed via `MAX(executed_at)` query helper.
- `stage_type` is `String(32)` + `CheckConstraint("stage_type IN ('market','news','fundamentals','social')")` — chosen over PG enum to avoid migration-on-extend.
- `verdict` is `String(16)` + `CheckConstraint("verdict IN ('bull','bear','neutral','unavailable')")`.
- `decision` on `research_summary` is `String(8)` + `CheckConstraint("decision IN ('buy','hold','sell')")`.
- `direction` on `summary_stage_links` is `String(8)` + `CheckConstraint("direction IN ('support','contradict','context')")`; `weight` is `Float` with `CheckConstraint("weight >= 0 AND weight <= 1")`.
- `signals` and `raw_payload` and `source_freshness` are `JSONB`. `source_freshness` shape is documented in Pydantic schema (Task 2).
- `snapshot_at` = data freshness anchor; `executed_at` = wall clock when analyzer ran. Both `DateTime(timezone=True)`.
- `research_summary` has **no** `UNIQUE(session_id)` (append-only re-summaries allowed).
- `user_research_notes` is created with minimal columns now (id, session_id FK, user_id FK → `users.id`, body Text, created_at, updated_at) — schema only, no service writes in this PR.

- [ ] **Step 1: Inspect existing model conventions**

Read `app/models/research_run.py` and `app/models/analysis.py` to confirm column comment style, server_default, index naming, relationship patterns, FK to `stock_info.id`. Use them as templates.

- [ ] **Step 2: Write failing model-shape test**

Create `tests/models/test_research_pipeline_models.py`:

```python
import pytest
from sqlalchemy import inspect

from app.models.research_pipeline import (
    ResearchSession,
    StageAnalysis,
    ResearchSummary,
    SummaryStageLink,
    UserResearchNote,
)


@pytest.mark.unit
def test_research_session_columns():
    cols = {c.name for c in inspect(ResearchSession).columns}
    assert {"id", "stock_info_id", "research_run_id", "status",
            "started_at", "finalized_at", "created_at", "updated_at"} <= cols


@pytest.mark.unit
def test_stage_analysis_columns_and_constraints():
    cols = {c.name for c in inspect(StageAnalysis).columns}
    assert {"id", "session_id", "stage_type", "verdict", "confidence",
            "signals", "raw_payload", "source_freshness", "model_name",
            "prompt_version", "snapshot_at", "executed_at"} <= cols
    constraint_names = {c.name for c in StageAnalysis.__table__.constraints if c.name}
    assert any("stage_type" in n for n in constraint_names)
    assert any("verdict" in n for n in constraint_names)


@pytest.mark.unit
def test_research_summary_no_unique_session_id():
    summary_table = ResearchSummary.__table__
    for uc in summary_table.constraints:
        cols = getattr(uc, "columns", None)
        if cols is None:
            continue
        col_names = {c.name for c in cols}
        assert col_names != {"session_id"}, "session_id must NOT be unique (append-only re-summaries allowed)"


@pytest.mark.unit
def test_summary_stage_link_columns():
    cols = {c.name for c in inspect(SummaryStageLink).columns}
    assert {"id", "summary_id", "stage_analysis_id", "weight", "direction",
            "rationale"} <= cols


@pytest.mark.unit
def test_user_research_note_columns():
    cols = {c.name for c in inspect(UserResearchNote).columns}
    assert {"id", "session_id", "user_id", "body", "created_at", "updated_at"} <= cols
```

- [ ] **Step 3: Run test to verify failure**

```
uv run pytest tests/models/test_research_pipeline_models.py -v
```
Expected: ImportError on `app.models.research_pipeline`.

- [ ] **Step 4: Implement `app/models/research_pipeline.py`**

```python
"""ROB-112 — Research pipeline ORM models.

5 tables: research_sessions, stage_analysis, research_summaries,
summary_stage_links, user_research_notes. All append-only except
research_sessions (status transitions allowed).
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.base import Base


class ResearchSession(Base):
    __tablename__ = "research_sessions"

    id = Column(Integer, primary_key=True, index=True)
    stock_info_id = Column(Integer, ForeignKey("stock_info.id"), nullable=False, index=True)
    research_run_id = Column(Integer, ForeignKey("research_runs.id"), nullable=True, index=True,
                             comment="optional link to upstream ResearchRun candidate")
    status = Column(String(16), nullable=False, default="open",
                    comment="open|finalized|failed|cancelled")
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finalized_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','finalized','failed','cancelled')",
            name="ck_research_sessions_status",
        ),
    )

    stock_info = relationship("StockInfo")
    stage_analyses = relationship("StageAnalysis", back_populates="session",
                                  cascade="all, delete-orphan")
    summaries = relationship("ResearchSummary", back_populates="session",
                             cascade="all, delete-orphan")
    notes = relationship("UserResearchNote", back_populates="session",
                         cascade="all, delete-orphan")


class StageAnalysis(Base):
    __tablename__ = "stage_analysis"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("research_sessions.id"), nullable=False, index=True)
    stage_type = Column(String(32), nullable=False)
    verdict = Column(String(16), nullable=False)
    confidence = Column(Integer, nullable=False, comment="0-100")
    signals = Column(JSONB, nullable=False, comment="validated by stage Pydantic schema")
    raw_payload = Column(JSONB, nullable=True, comment="provider/LLM raw output for debugging")
    source_freshness = Column(JSONB, nullable=True,
                              comment="{newest_age_minutes,oldest_age_minutes,missing_sources,stale_flags,source_count}")
    model_name = Column(String(100), nullable=True)
    prompt_version = Column(String(64), nullable=True)
    snapshot_at = Column(DateTime(timezone=True), nullable=True,
                         comment="latest data timestamp the stage observed")
    executed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False,
                         comment="wall-clock analyzer execution time")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "stage_type IN ('market','news','fundamentals','social')",
            name="ck_stage_analysis_stage_type",
        ),
        CheckConstraint(
            "verdict IN ('bull','bear','neutral','unavailable')",
            name="ck_stage_analysis_verdict",
        ),
        CheckConstraint("confidence BETWEEN 0 AND 100",
                        name="ck_stage_analysis_confidence_range"),
        Index("ix_stage_analysis_session_stage_executed",
              "session_id", "stage_type", "executed_at"),
    )

    session = relationship("ResearchSession", back_populates="stage_analyses")


class ResearchSummary(Base):
    __tablename__ = "research_summaries"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("research_sessions.id"), nullable=False, index=True)
    decision = Column(String(8), nullable=False, comment="buy|hold|sell")
    confidence = Column(Integer, nullable=False, comment="0-100")
    bull_arguments = Column(JSONB, nullable=False, default=list)
    bear_arguments = Column(JSONB, nullable=False, default=list)
    price_analysis = Column(JSONB, nullable=True,
                            comment="{appropriate_buy_min/max,appropriate_sell_min/max,buy_hope_min/max,sell_target_min/max}")
    reasons = Column(JSONB, nullable=True)
    detailed_text = Column(Text, nullable=True)
    warnings = Column(JSONB, nullable=True,
                      comment="missing/unavailable/stale stage warnings")
    model_name = Column(String(100), nullable=True)
    prompt_version = Column(String(64), nullable=True)
    raw_payload = Column(JSONB, nullable=True)
    token_input = Column(Integer, nullable=True)
    token_output = Column(Integer, nullable=True)
    executed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("decision IN ('buy','hold','sell')",
                        name="ck_research_summaries_decision"),
        CheckConstraint("confidence BETWEEN 0 AND 100",
                        name="ck_research_summaries_confidence_range"),
    )

    session = relationship("ResearchSession", back_populates="summaries")
    stage_links = relationship("SummaryStageLink", back_populates="summary",
                               cascade="all, delete-orphan")


class SummaryStageLink(Base):
    __tablename__ = "summary_stage_links"

    id = Column(Integer, primary_key=True, index=True)
    summary_id = Column(Integer, ForeignKey("research_summaries.id"), nullable=False, index=True)
    stage_analysis_id = Column(Integer, ForeignKey("stage_analysis.id"), nullable=False, index=True)
    weight = Column(Float, nullable=False, default=1.0)
    direction = Column(String(8), nullable=False)
    rationale = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("weight >= 0 AND weight <= 1",
                        name="ck_summary_stage_links_weight_range"),
        CheckConstraint("direction IN ('support','contradict','context')",
                        name="ck_summary_stage_links_direction"),
    )

    summary = relationship("ResearchSummary", back_populates="stage_links")


class UserResearchNote(Base):
    __tablename__ = "user_research_notes"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("research_sessions.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    session = relationship("ResearchSession", back_populates="notes")
```

- [ ] **Step 5: Re-export new models in `app/models/__init__.py`**

Append after the existing `from .research_run import (...)` block:

```python
from .research_pipeline import (
    ResearchSession,
    StageAnalysis,
    ResearchSummary,
    SummaryStageLink,
    UserResearchNote,
)
```

Add the symbols to `__all__` in alphabetical position if `__all__` is defined.

- [ ] **Step 6: Run model-shape test → PASS**

```
uv run pytest tests/models/test_research_pipeline_models.py -v
```
Expected: PASS.

- [ ] **Step 7: Generate Alembic migration**

```
uv run alembic revision --autogenerate -m "add research pipeline tables (ROB-112)"
```

- [ ] **Step 8: Inspect generated migration**

Open `alembic/versions/<hash>_add_research_pipeline_tables.py` and verify:
- 5 `op.create_table` calls (`research_sessions`, `stage_analysis`, `research_summaries`, `summary_stage_links`, `user_research_notes`).
- Indexes on `(session_id, stage_type, executed_at)` for `stage_analysis`.
- Check constraints for `stage_type`, `verdict`, `confidence`, `decision`, `weight`, `direction`, `status`.
- `down_revision` matches the latest head (`uv run alembic current`).
- A symmetric `downgrade()` that drops in reverse order.

If autogenerate missed the partial index or named constraints, edit the file to match the model `__table_args__` exactly.

- [ ] **Step 9: Apply migration locally**

```
uv run alembic upgrade head
uv run alembic current
uv run alembic downgrade -1   # smoke-test rollback
uv run alembic upgrade head
```
Expected: head matches new revision; downgrade succeeds (no orphan FKs); upgrade re-applies cleanly.

- [ ] **Step 10: Commit**

```
git add app/models/research_pipeline.py app/models/__init__.py \
        alembic/versions/*_add_research_pipeline_tables.py \
        tests/models/test_research_pipeline_models.py
git commit -m "feat(ROB-112): add research pipeline ORM models and migration"
```

---

## Task 2: Pydantic signal schemas

**Files:**
- Create: `app/schemas/research_pipeline.py`
- Test: `tests/schemas/test_research_pipeline_schemas.py`

Validation contracts gate every `signals` JSONB write. Unknown / debug provider output stays in `raw_payload`, not `signals`.

- [ ] **Step 1: Failing test for `MarketSignals`**

```python
import pytest
from pydantic import ValidationError

from app.schemas.research_pipeline import (
    MarketSignals,
    NewsSignals,
    FundamentalsSignals,
    SocialSignals,
    SourceFreshness,
    StageVerdict,
)


@pytest.mark.unit
def test_market_signals_valid():
    sig = MarketSignals(
        last_close=12345.0,
        change_pct=1.23,
        rsi_14=55.0,
        atr_14=410.5,
        volume_ratio_20d=1.8,
        trend="uptrend",
    )
    assert sig.last_close == 12345.0


@pytest.mark.unit
def test_market_signals_rejects_out_of_range_rsi():
    with pytest.raises(ValidationError):
        MarketSignals(last_close=1.0, change_pct=0.0, rsi_14=120.0,
                      atr_14=0.1, volume_ratio_20d=1.0, trend="flat")


@pytest.mark.unit
def test_social_signals_placeholder_shape():
    sig = SocialSignals(available=False, reason="not_implemented", phase="placeholder")
    assert sig.available is False
    assert sig.reason == "not_implemented"


@pytest.mark.unit
def test_source_freshness_required_keys():
    fresh = SourceFreshness(
        newest_age_minutes=5,
        oldest_age_minutes=120,
        missing_sources=[],
        stale_flags=[],
        source_count=3,
    )
    assert fresh.source_count == 3


@pytest.mark.unit
def test_stage_verdict_enum():
    assert {v.value for v in StageVerdict} == {"bull", "bear", "neutral", "unavailable"}
```

- [ ] **Step 2: Implement `app/schemas/research_pipeline.py`**

```python
"""ROB-112 — Pydantic schemas for the research pipeline."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict


class StageVerdict(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"
    UNAVAILABLE = "unavailable"


class SummaryDecision(str, Enum):
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class SourceFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    newest_age_minutes: int = Field(ge=0)
    oldest_age_minutes: int = Field(ge=0)
    missing_sources: list[str] = Field(default_factory=list)
    stale_flags: list[str] = Field(default_factory=list)
    source_count: int = Field(ge=0)


class MarketSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_close: float
    change_pct: float
    rsi_14: float = Field(ge=0, le=100)
    atr_14: float = Field(ge=0)
    volume_ratio_20d: float = Field(ge=0)
    trend: Literal["uptrend", "downtrend", "flat", "unknown"]


class NewsSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline_count: int = Field(ge=0)
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    top_themes: list[str] = Field(default_factory=list, max_length=10)
    urgent_flags: list[str] = Field(default_factory=list)


class FundamentalsSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per: float | None = None
    pbr: float | None = None
    market_cap: float | None = Field(default=None, ge=0)
    sector: str | None = None
    peer_count: int = Field(default=0, ge=0)
    relative_per_vs_peers: float | None = None


class SocialSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    reason: str
    phase: str = "placeholder"


class BullBearArgument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    cited_stage_ids: list[int] = Field(default_factory=list)
    direction: Literal["support", "contradict", "context"] = "support"
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class StageOutput(BaseModel):
    """Stage analyzer return type — pre-DB write contract."""
    model_config = ConfigDict(extra="forbid")

    stage_type: Literal["market", "news", "fundamentals", "social"]
    verdict: StageVerdict
    confidence: int = Field(ge=0, le=100)
    signals: MarketSignals | NewsSignals | FundamentalsSignals | SocialSignals
    raw_payload: dict | None = None
    source_freshness: SourceFreshness | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    snapshot_at: object | None = None  # datetime — left loose to avoid import cycles in this snippet


class PriceAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appropriate_buy_min: float | None = None
    appropriate_buy_max: float | None = None
    appropriate_sell_min: float | None = None
    appropriate_sell_max: float | None = None
    buy_hope_min: float | None = None
    buy_hope_max: float | None = None
    sell_target_min: float | None = None
    sell_target_max: float | None = None


class SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: SummaryDecision
    confidence: int = Field(ge=0, le=100)
    bull_arguments: list[BullBearArgument]
    bear_arguments: list[BullBearArgument]
    price_analysis: PriceAnalysis | None = None
    reasons: list[str] = Field(default_factory=list, max_length=10)
    detailed_text: str | None = None
    warnings: list[str] = Field(default_factory=list)
    model_name: str | None = None
    prompt_version: str | None = None
    raw_payload: dict | None = None
    token_input: int | None = None
    token_output: int | None = None
```

Replace the loose `snapshot_at: object | None` with `from datetime import datetime` + `datetime | None = None` — left loose in the plan to keep imports compact; engineer should fix.

- [ ] **Step 3: Run schema tests → PASS**

```
uv run pytest tests/schemas/test_research_pipeline_schemas.py -v
```

- [ ] **Step 4: Commit**

```
git add app/schemas/research_pipeline.py tests/schemas/test_research_pipeline_schemas.py
git commit -m "feat(ROB-112): add research pipeline Pydantic signal schemas"
```

---

## Task 3: Stage analyzer base class

**Files:**
- Create: `app/analysis/stages/__init__.py`
- Create: `app/analysis/stages/base.py`
- Test: `tests/analysis/stages/test_base.py`

`BaseStageAnalyzer` provides: stage_type assertion, freshness scaffolding, signal validation, DB row insert helper. **Stage analyzers must not see other stage outputs.** The base class enforces this by accepting only the symbol/session context as input.

- [ ] **Step 1: Failing base test**

```python
from datetime import datetime, timezone

import pytest

from app.analysis.stages.base import BaseStageAnalyzer, StageContext
from app.schemas.research_pipeline import (
    MarketSignals,
    SourceFreshness,
    StageOutput,
    StageVerdict,
)


class _DummyMarketStage(BaseStageAnalyzer):
    stage_type = "market"

    async def analyze(self, ctx: StageContext) -> StageOutput:
        return StageOutput(
            stage_type="market",
            verdict=StageVerdict.NEUTRAL,
            confidence=50,
            signals=MarketSignals(
                last_close=100.0, change_pct=0.0, rsi_14=50.0,
                atr_14=1.0, volume_ratio_20d=1.0, trend="flat",
            ),
            source_freshness=SourceFreshness(
                newest_age_minutes=1, oldest_age_minutes=1,
                missing_sources=[], stale_flags=[], source_count=1,
            ),
            snapshot_at=datetime.now(tz=timezone.utc),
        )


@pytest.mark.unit
async def test_dummy_stage_returns_validated_output():
    stage = _DummyMarketStage()
    out = await stage.analyze(StageContext(session_id=1, symbol="005930",
                                           instrument_type="equity_kr"))
    assert out.stage_type == "market"
    assert isinstance(out.signals, MarketSignals)


@pytest.mark.unit
def test_base_stage_rejects_wrong_stage_type():
    class _Bad(BaseStageAnalyzer):
        stage_type = "market"

        async def analyze(self, ctx):
            return StageOutput(
                stage_type="news",  # mismatch
                verdict=StageVerdict.NEUTRAL,
                confidence=10,
                signals=MarketSignals(
                    last_close=1.0, change_pct=0.0, rsi_14=10.0,
                    atr_14=0.1, volume_ratio_20d=1.0, trend="flat",
                ),
            )

    import asyncio
    stage = _Bad()
    with pytest.raises(ValueError, match="stage_type mismatch"):
        asyncio.run(stage.run(StageContext(session_id=1, symbol="X",
                                           instrument_type="equity_kr")))
```

- [ ] **Step 2: Implement base**

```python
# app/analysis/stages/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from app.schemas.research_pipeline import StageOutput


@dataclass(frozen=True)
class StageContext:
    session_id: int
    symbol: str
    instrument_type: str
    user_id: int | None = None


class BaseStageAnalyzer(ABC):
    stage_type: ClassVar[str]  # override in subclass

    @abstractmethod
    async def analyze(self, ctx: StageContext) -> StageOutput:
        ...

    async def run(self, ctx: StageContext) -> StageOutput:
        out = await self.analyze(ctx)
        if out.stage_type != self.stage_type:
            raise ValueError(
                f"stage_type mismatch: analyzer={self.stage_type} output={out.stage_type}"
            )
        return out
```

`__init__.py` re-exports `BaseStageAnalyzer` and `StageContext`.

- [ ] **Step 3: Run test → PASS**
- [ ] **Step 4: Commit**

```
git commit -m "feat(ROB-112): add stage analyzer base class"
```

---

## Task 4: Market stage analyzer

**Files:**
- Create: `app/analysis/stages/market_stage.py`
- Test: `tests/analysis/stages/test_market_stage.py`

Reuses existing OHLCV / quote / indicator services. Must not call `news_*`, `fundamentals_*`, or `social_*` providers.

- [ ] **Step 1: Failing test (mock OHLCV + quote)**

```python
import pytest
from unittest.mock import AsyncMock

from app.analysis.stages.base import StageContext
from app.analysis.stages.market_stage import MarketStageAnalyzer
from app.schemas.research_pipeline import MarketSignals, StageVerdict


@pytest.mark.unit
async def test_market_stage_basic_signals(monkeypatch):
    # Patch the *single* data source the stage uses. If the engineer
    # adds a second source they must add a second monkeypatch — this
    # keeps stage isolation visible at the test level.
    fake_ohlcv = AsyncMock(return_value={
        "last_close": 100.0,
        "change_pct": 1.5,
        "rsi_14": 60.0,
        "atr_14": 1.2,
        "volume_ratio_20d": 1.5,
        "trend": "uptrend",
        "snapshot_at_iso": "2026-05-05T08:00:00+00:00",
    })
    monkeypatch.setattr(
        "app.analysis.stages.market_stage._fetch_market_snapshot",
        fake_ohlcv,
    )

    stage = MarketStageAnalyzer()
    out = await stage.run(StageContext(session_id=1, symbol="005930",
                                       instrument_type="equity_kr"))
    assert isinstance(out.signals, MarketSignals)
    assert out.signals.last_close == 100.0
    assert out.verdict in {StageVerdict.BULL, StageVerdict.NEUTRAL, StageVerdict.BEAR}
    assert out.source_freshness is not None
```

- [ ] **Step 2: Implement market stage**

`_fetch_market_snapshot(symbol, instrument_type)` is a thin async wrapper around existing services (`app/services/yahoo.py`, `app/services/upbit.py`, `app/services/kis.py`) — pick the right one based on `instrument_type`. Centralize routing so the stage analyzer body is small and testable. Verdict mapping is a pure function of signals (e.g., `change_pct > 1 and rsi_14 < 70 and trend == 'uptrend'` → `bull`). Document the rule inline.

- [ ] **Step 3: Run test → PASS**
- [ ] **Step 4: Commit**

```
git commit -m "feat(ROB-112): add market stage analyzer"
```

---

## Task 5: News stage analyzer

**Files:**
- Create: `app/analysis/stages/news_stage.py`
- Test: `tests/analysis/stages/test_news_stage.py`

Reuses `app/services/llm_news_service.py` / `n8n_news_service.py` headline retrieval. Sentiment can be a small/cheap LLM call OR rule-based aggregation in v1 — engineer chooses; record `model_name` either way.

Mirror Task 4 step structure (red → minimal green → commit). Source freshness must surface the newest headline age in minutes.

- [ ] Step 1: Failing test with mocked `_fetch_recent_headlines`
- [ ] Step 2: Implementation
- [ ] Step 3: Run test → PASS
- [ ] Step 4: Commit `feat(ROB-112): add news stage analyzer`

---

## Task 6: Fundamentals stage analyzer

**Files:**
- Create: `app/analysis/stages/fundamentals_stage.py`
- Test: `tests/analysis/stages/test_fundamentals_stage.py`

Reuses `app/mcp_server/tooling/fundamentals_sources_*.py` helpers (Naver / yfinance / finnhub). Map raw PER/PBR/market_cap → `FundamentalsSignals`. Verdict is bull if PER below sector median by >20%, bear if above by >50%, neutral otherwise. Document the rule inline.

- [ ] Step 1: Failing test
- [ ] Step 2: Implementation
- [ ] Step 3: Run test → PASS
- [ ] Step 4: Commit `feat(ROB-112): add fundamentals stage analyzer`

---

## Task 7: Social stage placeholder

**Files:**
- Create: `app/analysis/stages/social_stage.py`
- Test: `tests/analysis/stages/test_social_stage.py`

Always inserts a placeholder row.

- [ ] **Step 1: Failing test**

```python
import pytest

from app.analysis.stages.base import StageContext
from app.analysis.stages.social_stage import SocialStageAnalyzer
from app.schemas.research_pipeline import SocialSignals, StageVerdict


@pytest.mark.unit
async def test_social_stage_placeholder():
    stage = SocialStageAnalyzer()
    out = await stage.run(StageContext(session_id=1, symbol="X",
                                       instrument_type="equity_kr"))
    assert out.verdict == StageVerdict.UNAVAILABLE
    assert out.confidence == 0
    assert isinstance(out.signals, SocialSignals)
    assert out.signals.available is False
    assert out.signals.reason == "not_implemented"
    assert out.signals.phase == "placeholder"
```

- [ ] **Step 2: Implement**

```python
# app/analysis/stages/social_stage.py
from app.analysis.stages.base import BaseStageAnalyzer, StageContext
from app.schemas.research_pipeline import (
    SocialSignals,
    StageOutput,
    StageVerdict,
)


class SocialStageAnalyzer(BaseStageAnalyzer):
    stage_type = "social"

    async def analyze(self, ctx: StageContext) -> StageOutput:
        return StageOutput(
            stage_type="social",
            verdict=StageVerdict.UNAVAILABLE,
            confidence=0,
            signals=SocialSignals(
                available=False, reason="not_implemented", phase="placeholder"
            ),
            source_freshness=None,
            model_name=None,
            prompt_version="social.placeholder.v1",
        )
```

- [ ] Step 3: Run test → PASS
- [ ] Step 4: Commit `feat(ROB-112): add social stage placeholder analyzer`

---

## Task 8: Debate / summary builder with citation links

**Files:**
- Create: `app/analysis/debate.py`
- Test: `tests/analysis/test_debate.py`

`build_summary(stage_outputs, *, model_runner)` accepts the latest `StageOutput` per stage_type, runs the bull/bear LLM debate (or deterministic v1 reducer if `model_runner` is `None`), and returns `(SummaryOutput, list[StageLinkSpec])`. Each `StageLinkSpec` references the *DB id* of a stage row — wired up after Pipeline (Task 9) inserts stage rows and passes their ids back.

Debate rules:
- If any stage is `unavailable`, append a warning ("social: not_implemented") but still produce a decision.
- If `>=2` stages are stale (per `source_freshness.stale_flags`), force `decision=hold` and append a warning.
- Bull/bear arguments must each cite at least one stage_analysis id (no orphan claims) — assert in test.
- Token counts and `raw_payload` (LLM output) are stored when `model_runner` is provided.

- [ ] Step 1: Failing test verifying citation invariant + stale → hold rule
- [ ] Step 2: Implement deterministic v1 reducer + LLM hook
- [ ] Step 3: Run test → PASS
- [ ] Step 4: Commit `feat(ROB-112): add summary debate builder with citation links`

---

## Task 9: Pipeline orchestrator

**Files:**
- Create: `app/analysis/pipeline.py`
- Create: `app/services/research_pipeline_service.py`
- Test: `tests/analysis/test_pipeline.py`

`research_pipeline_service.run_research_session(db, *, symbol, instrument_type, user_id=None, research_run_id=None) -> ResearchSession` orchestrates:

1. `create_stock_if_not_exists(symbol, ...)` (existing helper in `stock_info_service.py`).
2. Insert `ResearchSession(status='open')`.
3. Run 4 stage analyzers concurrently via `asyncio.gather` — **stages do not share state**.
4. Validate each `StageOutput`, insert `StageAnalysis` row, capture DB id.
5. Build summary with `app.analysis.debate.build_summary(stage_outputs)`.
6. Insert `ResearchSummary` row, then `SummaryStageLink` rows referencing the DB ids from step 4.
7. If `RESEARCH_PIPELINE_DUAL_WRITE_ENABLED`, call `legacy_stock_analysis_adapter.write(summary, stock_info_id, ...)` (Task 10).
8. Update `ResearchSession.status='finalized'`, set `finalized_at`. Single transaction commit at the end (or per-stage commits with explicit rollback policy — see decision below).

**Transaction policy:** stage rows commit individually so a failed stage does not lose other stage evidence. Summary + links + dual-write commit as one transaction. Session status flips after the final commit. Document in a 1-line comment.

`pipeline.run_research_session` is the **only** function allowed to write to these tables. `research_pipeline_service` re-exports it as the public surface (mirrors `alpaca_paper_ledger_service.py` pattern from CLAUDE.md).

- [ ] Step 1: Failing test mocking the four stage analyzers
- [ ] Step 2: Implement pipeline + service module
- [ ] Step 3: Run test → PASS
- [ ] Step 4: Commit `feat(ROB-112): add research pipeline orchestrator`

---

## Task 10: Legacy `StockAnalysisResult` dual-write adapter

**Files:**
- Create: `app/services/legacy_stock_analysis_adapter.py`
- Test: `tests/services/test_legacy_stock_analysis_adapter.py`

Maps `SummaryOutput` → `StockAnalysisResult`:

| Source | Target |
|---|---|
| `summary.decision` | `decision` |
| `summary.confidence` | `confidence` |
| `summary.price_analysis.appropriate_buy_min/max` | `appropriate_buy_min/max` |
| `summary.price_analysis.appropriate_sell_min/max` | `appropriate_sell_min/max` |
| `summary.price_analysis.buy_hope_min/max` | `buy_hope_min/max` |
| `summary.price_analysis.sell_target_min/max` | `sell_target_min/max` |
| `summary.reasons` | `reasons` (JSONB) |
| `summary.detailed_text` | `detailed_text` |
| `summary.model_name or "research_pipeline"` | `model_name` |
| `f"research_summary:{summary_id}/prompt_version:{summary.prompt_version}"` | `prompt` |

The adapter takes already-persisted `summary_id` (so the prompt string is reproducible) and the `stock_info_id`. It only writes — never reads from `StockAnalysisResult`.

- [ ] **Step 1: Failing mapping test using in-memory ORM session fixture**

Verify each mapping above + that `prompt` field encodes the summary id reference.

- [ ] **Step 2: Implement adapter**
- [ ] **Step 3: Run test → PASS**
- [ ] **Step 4: Commit**

```
git commit -m "feat(ROB-112): add legacy StockAnalysisResult dual-write adapter"
```

---

## Task 11: Feature flags

**Files:**
- Modify: `app/core/config.py`
- Modify: `env.example`
- Test: `tests/core/test_research_pipeline_flags.py`

Add to `Settings`:

```python
RESEARCH_PIPELINE_ENABLED: bool = False
RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED: bool = False
RESEARCH_PIPELINE_DUAL_WRITE_ENABLED: bool = False
```

Defaults are all `False` so a fresh `main` deploy is a strict no-op. Add corresponding entries to `env.example` with comments.

Document the matrix:

| Flag | Default | Effect |
|---|---|---|
| `RESEARCH_PIPELINE_ENABLED` | `False` | Master switch. Read APIs / MCP read tools return 503 / `error: pipeline_disabled` when off. |
| `RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED` | `False` | When `True` AND master is `True`, MCP `analyze_stock` calls the new pipeline. Response schema is unchanged. On any failure, fall back to legacy analyzer. |
| `RESEARCH_PIPELINE_DUAL_WRITE_ENABLED` | `False` | When `True`, summary finalize triggers `StockAnalysisResult` insert. Independent of `ANALYZE_STOCK_ENABLED` so dual-write can be tested before MCP cutover. |

- [ ] Step 1: Failing test asserting all three flags exist with `False` default
- [ ] Step 2: Implement
- [ ] Step 3: Run test → PASS
- [ ] Step 4: Commit `feat(ROB-112): add research pipeline feature flags`

---

## Task 12: MCP `analyze_stock` compatibility path

**Files:**
- Modify: `app/mcp_server/tooling/analysis_analyze.py`
- Create: `app/mcp_server/tooling/research_pipeline_read.py`
- Modify: `app/mcp_server/tooling/analysis_registration.py`
- Test: `tests/mcp_server/test_analyze_stock_pipeline_compat.py`
- Test: `tests/mcp_server/test_research_pipeline_read_tools.py`

### 12a — `analyze_stock` dispatch

In `analyze_stock_impl`, before calling the existing flow:

```python
if settings.RESEARCH_PIPELINE_ENABLED and settings.RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED:
    try:
        return await _analyze_stock_via_pipeline(symbol, market_type, ...)
    except PipelineError as exc:
        logger.warning("research_pipeline.analyze_stock fallback: %s", exc)
        # fall through to legacy
```

`_analyze_stock_via_pipeline` runs the new pipeline and **converts** the `SummaryOutput` to the existing `analyze_stock` response shape. **Response schema MUST NOT change** — same keys, same types, same defaults. The integration test must compare the response of the pipeline path against the legacy path on a fixture symbol and assert key-set equality.

### 12b — Read-only MCP tools

Add to `app/mcp_server/tooling/research_pipeline_read.py`:

| Tool | Description |
|---|---|
| `research_session_get` | Returns 1 session + 4 latest stage rows + latest summary + cited stage_analysis ids. |
| `research_session_list_recent` | Returns recent N sessions with status, decision, confidence. Read-only. |
| `stage_analysis_get` | Returns one stage row by id. |
| `research_summary_get` | Returns one summary + linked stage rows by summary id. |

All four tools return `dict[str, Any]` with `_error_payload(...)` on failure (matches existing MCP convention from MEMORY.md). All four refuse to write.

Register in `analysis_registration.py` next to the existing `analyze_stock` registration (or a new `research_pipeline_registration.py` if scope grows — engineer's call).

- [ ] **Step 1: Failing dispatch test**

```python
@pytest.mark.unit
async def test_analyze_stock_falls_back_when_pipeline_disabled(monkeypatch):
    # both flags False → legacy path
    ...

@pytest.mark.unit
async def test_analyze_stock_pipeline_response_keys_match_legacy(monkeypatch):
    # both flags True, pipeline mock returns valid SummaryOutput
    # legacy result and pipeline result must have identical top-level keys
    ...

@pytest.mark.unit
async def test_analyze_stock_pipeline_falls_back_on_pipeline_error(monkeypatch):
    # raise PipelineError inside _analyze_stock_via_pipeline → legacy result returned
    ...
```

- [ ] **Step 2: Failing read-tools test**

`DummyMCP` + `build_tools()` pattern (per MEMORY.md). Verify each tool name, default args, and read-only behavior (raises if attempted to write).

- [ ] **Step 3: Implement 12a then 12b**
- [ ] **Step 4: Run tests → PASS**
- [ ] **Step 5: Commit**

```
git commit -m "feat(ROB-112): wire research pipeline behind analyze_stock + add read MCP tools"
```

---

## Task 13: Read-only Research Session router (backend)

**Files:**
- Create: `app/routers/research_pipeline.py`
- Modify: `app/main.py` (or wherever routers are registered) to include the router
- Test: `tests/routers/test_research_pipeline_router.py`

Endpoints (all GET, read-only):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/research-pipeline/sessions` | recent sessions list |
| GET | `/api/research-pipeline/sessions/{session_id}` | session header + status |
| GET | `/api/research-pipeline/sessions/{session_id}/stages` | latest stage row per stage_type + freshness + warnings |
| GET | `/api/research-pipeline/sessions/{session_id}/summary` | latest summary + cited stage_analysis ids |

403 when `RESEARCH_PIPELINE_ENABLED=False`. No POST/PUT/DELETE in this PR.

- [ ] Step 1: Failing FastAPI test client tests
- [ ] Step 2: Implement
- [ ] Step 3: Run tests → PASS
- [ ] Step 4: Commit `feat(ROB-112): add read-only research pipeline router`

---

## Task 14: Side-effect-safety test

**Files:**
- Test: `tests/services/test_research_pipeline_safety.py`

Importing `app.services.research_pipeline_service`, `app.analysis.pipeline`, and the four stage analyzers must NOT import any of:
- `app.services.kis_trading_service`
- `app.services.kis_websocket`
- `app.services.upbit` order endpoints
- `app.services.brokers.alpaca`
- `app.services.order_service`
- `app.services.order_intent_*`
- watch alert services
- scheduler modules

Pattern: capture `sys.modules` before/after import, diff, assert intersection with the forbidden set is empty. Mirrors the ROB-9 safety test approach.

- [ ] Step 1: Write the assertion
- [ ] Step 2: Run → PASS (if it fails, the engineer accidentally pulled in a forbidden module)
- [ ] Step 3: Commit `test(ROB-112): assert pipeline imports do not load broker/watch/order modules`

---

## Task 15: React read-only Research Session page (DEFERRED-ALLOWED)

**Files:**
- Create: `frontend/trading-decision/src/api/researchPipeline.ts`
- Create: `frontend/trading-decision/src/pages/ResearchSessionPage.tsx`
- Create: `frontend/trading-decision/src/pages/ResearchSessionPage.module.css`
- Modify: `frontend/trading-decision/src/routes.tsx`
- Test: `frontend/trading-decision/src/__tests__/ResearchSessionPage.test.tsx`

5 tabs (Market / News / Fundamentals / Social / Summary). Stage freshness chips + missing/unavailable warnings. Summary tab shows bull/bear arguments with citation chips that scroll to the cited stage row. Raw/debug JSON only inside `<details>` collapsed.

**No mutation UI.** No order place / dry-run order / watch / order-intent / scheduler controls.

- [ ] Step 1: Typed API client + 1 React Testing Library test for the data-loading happy path
- [ ] Step 2: Implement page + tabs
- [ ] Step 3: `cd frontend/trading-decision && npm test -- --run && npm run build`
- [ ] Step 4: Commit `feat(ROB-112): add read-only research session page`

**Defer-criteria:** if Tasks 1-14 leave less than 1.5 hour of focus or any of (Tasks 1-14) are flaky, skip Task 15 entirely. Document the deferral explicitly in Task 16.

---

## Task 16: Runbook + handoff note

**Files:**
- Create: `docs/runbooks/research-pipeline.md`
- Modify: PR description (or final Linear comment)

Runbook MUST cover:

1. Feature flag matrix (copy from Task 11).
2. How to enable in staging (`RESEARCH_PIPELINE_ENABLED=true` + dual-write off → run 1 symbol via MCP `research_session_get` → inspect 4 stage rows + 1 summary + N links).
3. Rollback: set all 3 flags to `False`. No DB rollback needed (append-only).
4. How to query "latest stage row per stage_type":

```sql
SELECT DISTINCT ON (stage_type) *
FROM stage_analysis
WHERE session_id = :sid
ORDER BY stage_type, executed_at DESC;
```

5. Where dual-write writes (`stock_analysis_results`) and the `prompt` encoding (`research_summary:{id}/prompt_version:{v}`).
6. Known gaps: social stage placeholder, no `superseded_by`, `order_outcome` deferred.
7. Hermes server checklist (mirrors Linear acceptance criteria).

Handoff note (final PR comment / Linear comment) must list:
- branch name
- PR URL
- migrations added + applied locally only (yes / no)
- exact test commands run + results
- feature flag defaults (all `False`)
- whether React page was included or deferred
- server-only validation needed (apply migration, confirm flags off in production .env, dry-run via `research_session_get`)
- deployment cautions (migration is additive only; no destructive op)

- [ ] Step 1: Write runbook
- [ ] Step 2: Compose handoff note
- [ ] Step 3: Commit `docs(ROB-112): add research pipeline runbook + handoff note`

---

## Final verification gate (before opening PR)

Run from repo root:

```
uv run ruff check app tests
uv run pytest tests/models/test_research_pipeline_models.py \
              tests/schemas/test_research_pipeline_schemas.py \
              tests/analysis/stages/ \
              tests/analysis/test_debate.py \
              tests/analysis/test_pipeline.py \
              tests/services/test_legacy_stock_analysis_adapter.py \
              tests/services/test_research_pipeline_safety.py \
              tests/mcp_server/test_analyze_stock_pipeline_compat.py \
              tests/mcp_server/test_research_pipeline_read_tools.py \
              tests/routers/test_research_pipeline_router.py \
              tests/core/test_research_pipeline_flags.py -v
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

If frontend touched:

```
cd frontend/trading-decision && npm test -- --run && npm run build
```

Confirm all three flags default to `False` in `.env.example`. Confirm grep for `place_order|cancel_order|watch_alert|order_intent_create` inside `app/analysis/pipeline.py` and `app/analysis/stages/` returns zero matches.

---

## Open questions to resolve during implementation

These are flagged in the spec; resolve inline rather than blocking:

1. **`order_outcome` table**: spec says don't add it now. Confirm `TradingDecisionOutcome` already has the fields needed for retro analysis; if so, document the join path in the runbook and skip a new table. If not, file a follow-up Linear issue.
2. **Stage analyzer model selection**: cheap-vs-strong split is recommended in spec but not mandated for v1. Engineer can land deterministic v1 reducers for market/news and an LLM only for fundamentals + summary. Record the choice in commit messages.
3. **`ResearchSession.status` set when summary fails**: pipeline (Task 9) sets `status='failed'` and re-raises. Confirm with code review.

---

## Self-review (run by author before handoff)

- Spec coverage:
  - DB / migration → Task 1 ✅
  - Pydantic schemas → Task 2 ✅
  - 4 stage analyzers (independent) → Tasks 3-7 ✅
  - Summary / debate / citation → Task 8 ✅
  - Legacy dual-write → Tasks 9 + 10 ✅
  - MCP compatibility + feature flags → Tasks 11 + 12 ✅
  - React read-only page → Task 15 (deferrable) ✅
  - Tests for every acceptance criterion bullet → covered, including side-effect-safety (Task 14) ✅
  - Handoff requirements → Task 16 ✅
- Placeholder scan: no "TBD" / "implement later" / "similar to" — every step shows what to do.
- Type consistency: `StageOutput` / `SummaryOutput` / `SourceFreshness` / `BullBearArgument` referenced consistently across Tasks 2-12.
