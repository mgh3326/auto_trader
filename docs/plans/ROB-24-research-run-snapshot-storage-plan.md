# ROB-24 — Research Run Snapshot Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear issue:** ROB-24 — [Persistence] Add Research Run snapshot storage for KR/NXT preparation

**Goal:** Persist prepared market research snapshots (a "Research Run") separately from `trading_decision_sessions` so open / live-refresh decision flows can be generated quickly. The Research Run rows hold candidates and reconciliation results pre-computed by the pure ROB-22/ROB-23 services, plus source-freshness metadata and missing/stale-source warnings. They do **not** create orders, watches, paper trades, or order intents.

**Architecture:**
- Three new tables: `research_runs`, `research_run_candidates`, `research_run_pending_reconciliations`. Each is JSONB-friendly and mirrors the patterns in `trading_decision_sessions` / `trading_decision_proposals` (BigInteger PK + UUID natural key + JSONB payload + check-constrained Text enums).
- One new ORM module `app/models/research_run.py` with `ResearchRun`, `ResearchRunCandidate`, `ResearchRunPendingReconciliation` SQLAlchemy models, exported from `app/models/__init__.py`.
- One new persistence service `app/services/research_run_service.py` exposing async CRUD: `create_research_run`, `add_research_run_candidates`, `attach_pending_reconciliations`, `get_research_run_by_uuid`, `list_user_research_runs`. The service consumes plain DTOs (the `PendingReconciliationItem` and `NxtClassifierItem` already produced by ROB-22 / ROB-23) and never re-classifies.
- One new schemas module `app/schemas/research_run.py` for Pydantic request/response models used by future routers and tests.
- One new Alembic migration adding the three tables (down-revision: current head `ce5d470cc894`).
- Tests under `tests/services/test_research_run_service.py`, `tests/services/test_research_run_service_safety.py`, `tests/models/test_research_run_models.py`, and `tests/test_research_run_schemas.py`.
- TradingAgents advisory references are recorded as a JSONB column (`advisory_links`) on `research_runs`. The first PR does **not** add a separate `research_run_advisories` table — the JSONB column lets us link to existing TradingAgents `trading_decision_sessions` records by `session_uuid` without coupling schemas. A dedicated table can be added in a follow-up if a strict relational FK is needed.

**Tech stack:** Python 3.13, SQLAlchemy 2.x async, Alembic, Pydantic v2, `pytest`, `pytest-asyncio`. No new third-party dependencies.

**ROB-20 boundary (non-negotiable):** ROB-20 (live-refresh wiring, API/UI rendering, Prefect orchestration) is **out of scope**. This plan delivers only persistence contracts (model + schema + service + migration + tests). If a behavior here genuinely requires ROB-20-side wiring, **stop and report it as a blocker** — do not start ROB-20 work in this PR.

**Trading-safety guardrails (non-negotiable):**
- Read-only / decision-support only. No call to `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, paper-order, dry-run order, live order, fill notification, watch registration, or order-intent creation is added.
- No DB row written by this PR ever represents an executed order, an approved watch alert, or a paper trade. `research_run_pending_reconciliations` rows store *classifier output* (the same DTO shape ROB-22 already returns); they do not represent broker state.
- `research_run_service.py` must not transitively import broker / order-execution / watch-alert / paper-order / fill-notification / KIS-websocket / Upbit-websocket modules. Enforced by a subprocess `sys.modules` test (Task 9), modeled on `tests/models/test_trading_decision_service.py` lines 38–52 (the service may import `sqlalchemy` and `app.core.db`, unlike ROB-22/23 pure modules).
- TradingAgents references stored in `advisory_links` carry `advisory_only: true` / `execution_allowed: false` invariants; the service must reject inputs that violate these flags (Task 4).
- Decision Session creation, watch registration, and order placement are **not** triggered by Research Run creation. Routers / Prefect wiring that would do that belong to ROB-25 / ROB-20.
- No secrets, API keys, tokens, or account numbers are read or printed by this code. Tests use `users` rows created locally with non-sensitive fixtures.

---

## File Structure

| Path | Status | Responsibility |
|------|--------|----------------|
| `app/models/research_run.py` | create | SQLAlchemy ORM models for `research_runs`, `research_run_candidates`, `research_run_pending_reconciliations` (BigInteger PKs, UUID natural keys, JSONB payloads, check-constrained Text enums). |
| `app/models/__init__.py` | modify | Re-export the three new ORM classes; extend `__all__`. |
| `app/schemas/research_run.py` | create | Pydantic v2 request/response models: `ResearchRunCreate`, `ResearchRunCandidateCreate`, `ResearchRunPendingReconciliationCreate`, `ResearchRunDetail`, `ResearchRunSummary`, plus shared `MarketScopeLiteral`, `StageLiteral`, `RunStatusLiteral`, `CandidateKindLiteral`. |
| `app/services/research_run_service.py` | create | Async CRUD over the three tables. Pure persistence: consumes already-classified DTOs from ROB-22/23, never re-classifies. |
| `alembic/versions/<rev>_add_research_run_tables.py` | create | DDL for the three tables, with check constraints, indexes, FKs, and a clean `downgrade()` that drops them in reverse order. Down-revision = current head `ce5d470cc894`. |
| `tests/services/test_research_run_service.py` | create | Integration tests (`@pytest.mark.integration`) covering create-run, attach-candidates, attach-reconciliations, get-by-uuid, list-by-user, and warnings round-trip. |
| `tests/services/test_research_run_service_safety.py` | create | Subprocess `sys.modules` test asserting `app.services.research_run_service` does not transitively import broker / order-execution / watch-alert / paper-order / fill-notification / KIS-websocket / Upbit-websocket modules. Allows `sqlalchemy` and `app.core.db` (unlike ROB-22/23 pure-service safety tests). |
| `tests/models/test_research_run_models.py` | create | Integration tests on the ORM models: insert / read with all JSONB fields populated, check that DB-level CHECK constraints reject invalid `stage` / `market_scope` / `candidate_kind` / `classification`. |
| `tests/test_research_run_schemas.py` | create | Pure unit tests (`@pytest.mark.unit`) for Pydantic validators (`extra="forbid"`, charset checks for symbol, advisory invariants). |
| `docs/plans/ROB-24-research-run-snapshot-storage-plan.md` | create (this file) | Implementation plan. |

**No changes to:**
- `app/services/pending_reconciliation_service.py` (ROB-22) — consume only.
- `app/services/nxt_classifier_service.py` (ROB-23) — consume only.
- `app/services/trading_decision_service.py` / `app/services/operator_decision_session_service.py` / `app/services/tradingagents_research_service.py` — wiring (operator request → Research Run) is a follow-up issue (ROB-25 or later).
- `app/services/kr_symbol_universe_service.py`, `app/services/kis*`, `app/services/upbit*`, `app/services/market_data/*` — callers resolve their own context and pass DTOs in.
- Any router, Prefect flow, MCP tool, or UI template — wiring is out of scope.

---

## Domain Reference (read once before coding)

### Concept boundaries

- **Research Run** = a snapshot of the inputs we have *already prepared* for a market scope and stage at a point in time. It is **not** a Decision Session: no proposals, no `user_response`, no acceptance ledger. A future workflow may use a Research Run to *generate* a Decision Session, but that is ROB-25's concern.
- **Stage** = which preparation pass produced this run.
  - `preopen` — KR pre-opening preparation.
  - `intraday` — KR / US intraday refresh.
  - `nxt_aftermarket` — KR NXT after-hours preparation.
  - `us_open` — future US open-bell preparation. Reserved; first PR does not need to test it end-to-end but the schema must accept it.
- **Market scope** = `kr` / `us` / `crypto`. First PR must support `kr` end-to-end. Schema must accept `us` and `crypto` so future stages slot in without migrations.
- **Source freshness** = JSONB blob describing when each upstream input was sampled (`quote_as_of`, `orderbook_as_of`, `support_resistance_as_of`, `kr_universe_synced_at`, `recorded_at`, …). Stored opaquely; service validates it is a JSON object and that all values are ISO-8601 timestamps or null.
- **Source warnings** = list of `Literal` strings reusing the warning vocabulary already produced by `pending_reconciliation_service` (`missing_quote`, `stale_quote`, `missing_orderbook`, `missing_support_resistance`, `missing_kr_universe`, `non_nxt_venue`, `unknown_side`, …). Additional run-level warnings: `quote_universe_drift` (run-wide), `mixed_venue` (run-wide). Service stores whatever the caller passes; the schema validates each entry is a non-empty string ≤ 64 chars.

### Reuse from ROB-22 / ROB-23

- `Classification` (ROB-22) — `app/services/pending_reconciliation_service.py:17-26`: `maintain`, `near_fill`, `too_far`, `chasing_risk`, `data_mismatch`, `kr_pending_non_nxt`, `unknown_venue`, `unknown`. Stored verbatim in `research_run_pending_reconciliations.classification`.
- `NxtClassification` (ROB-23) — `app/services/nxt_classifier_service.py:31-42`: `buy_pending_at_support`, `buy_pending_too_far`, `buy_pending_actionable`, `sell_pending_near_resistance`, `sell_pending_too_optimistic`, `sell_pending_actionable`, `non_nxt_pending_ignore_for_nxt`, `holding_watch_only`, `data_mismatch_requires_review`, `unknown`. Stored verbatim in `research_run_pending_reconciliations.nxt_classification` (nullable for non-KR rows).
- `PendingReconciliationItem.decision_support` — JSONB-stored as-is.
- `NxtClassifierItem.summary` — Korean operator-facing summary string, stored verbatim in `summary` (nullable).

### Existing models to mirror

- `TradingDecisionSession` (`app/models/trading_decision.py:77`) is the closest analog: `id` BigInteger PK, `session_uuid` UUID natural key, `user_id` FK with `ondelete="CASCADE"`, `source_profile` Text, `strategy_name` Text nullable, `market_scope` Text nullable, `market_brief` JSONB nullable, `status` Text + check constraint, `notes` Text nullable, `generated_at` TIMESTAMPZ, `created_at` / `updated_at` server defaults, `(user_id, generated_at DESC)` composite index. ROB-24 uses the same shape with stage and freshness fields added.
- `TradingDecisionProposal` (`app/models/trading_decision.py:124`) shows the `(session_id ondelete=CASCADE, symbol, instrument_type enum reuse, side text+check, JSONB payload nullable=false)` pattern. `research_run_candidates` follows it minus the proposal/user-response semantics.
- `instrument_type` PostgreSQL enum — already exists; reuse via `postgresql.ENUM(..., name="instrument_type", create_type=False)` in the migration (see `alembic/versions/ce5d470cc894_create_trading_decision_tables.py:22-26`).

### Why a new table family rather than reusing `trading_decision_sessions`

- A Decision Session represents committed proposals + user responses + counterfactuals + outcomes. A Research Run is upstream of that — it is the pre-flight snapshot. They have different lifecycles (Research Runs may be created and discarded without any user action; Decision Sessions are created when an operator confirms intent).
- Sharing the table would force `original_payload` to overload two semantics and make `user_response` meaningless for Research Run rows. Storage and indexes can be tuned independently.
- The same `users` row owns both, with FKs to `users.id`. Down-revision the migration onto `ce5d470cc894` so the relationship sequencing stays consistent.

---

## Public API of the Service

```python
# app/services/research_run_service.py
from __future__ import annotations
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, TypedDict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunPendingReconciliation,
)
from app.models.trading import InstrumentType


class CandidateCreate(TypedDict, total=False):
    symbol: str
    instrument_type: InstrumentType
    side: str  # 'buy' | 'sell' | 'none'
    candidate_kind: str  # 'pending_order' | 'holding' | 'screener_hit' | 'proposed' | 'other'
    proposed_price: Decimal | None
    proposed_qty: Decimal | None
    confidence: int | None  # 0..100
    rationale: str | None
    currency: str | None
    source_freshness: dict[str, Any] | None
    warnings: list[str]
    payload: dict[str, Any]


class PendingReconciliationCreate(TypedDict, total=False):
    candidate_id: int | None  # FK research_run_candidates.id (optional)
    order_id: str
    symbol: str
    market: str  # 'kr' | 'us' | 'crypto'
    side: str    # 'buy' | 'sell'
    classification: str  # ROB-22 Classification literal
    nxt_classification: str | None  # ROB-23 NxtClassification literal (nullable)
    nxt_actionable: bool | None
    gap_pct: Decimal | None
    reasons: list[str]
    warnings: list[str]
    decision_support: dict[str, Any]
    summary: str | None


async def create_research_run(
    session: AsyncSession,
    *,
    user_id: int,
    market_scope: str,            # 'kr' | 'us' | 'crypto'
    stage: str,                   # 'preopen' | 'intraday' | 'nxt_aftermarket' | 'us_open'
    source_profile: str,
    strategy_name: str | None = None,
    notes: str | None = None,
    market_brief: dict[str, Any] | None = None,
    source_freshness: dict[str, Any] | None = None,
    source_warnings: Sequence[str] = (),
    advisory_links: Sequence[dict[str, Any]] = (),
    generated_at: datetime,
) -> ResearchRun: ...


async def add_research_run_candidates(
    session: AsyncSession,
    *,
    research_run_id: int,
    candidates: Sequence[CandidateCreate],
) -> list[ResearchRunCandidate]: ...


async def attach_pending_reconciliations(
    session: AsyncSession,
    *,
    research_run_id: int,
    items: Sequence[PendingReconciliationCreate],
) -> list[ResearchRunPendingReconciliation]: ...


async def get_research_run_by_uuid(
    session: AsyncSession,
    *,
    run_uuid: UUID,
    user_id: int,
) -> ResearchRun | None: ...


async def list_user_research_runs(
    session: AsyncSession,
    *,
    user_id: int,
    market_scope: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[ResearchRun, int, int]], int]:
    """Return (rows, total). Each row = (run, candidate_count, reconciliation_count)."""
```

The service performs **only** these mutations:
- `INSERT` into `research_runs`, `research_run_candidates`, `research_run_pending_reconciliations`.
- `SELECT` for read APIs.
- It never `UPDATE`s (`updated_at` is server-side `onupdate`). It never `DELETE`s. Cascade deletes when a parent run is removed are handled by FKs `ondelete="CASCADE"`, but the service exposes no delete API in this PR.

---

## Domain DTO Adapters (helper functions)

To keep call sites clean, expose two small adapters that convert the pure DTOs from ROB-22/23 into the `TypedDict` shapes the service expects. These adapters live in the same module and **only do shape translation** — no I/O, no classification.

```python
# app/services/research_run_service.py (continued)
from app.services.pending_reconciliation_service import PendingReconciliationItem
from app.services.nxt_classifier_service import NxtClassifierItem


def reconciliation_create_from_recon(
    item: PendingReconciliationItem,
    *,
    candidate_id: int | None = None,
    summary: str | None = None,
) -> PendingReconciliationCreate:
    return {
        "candidate_id": candidate_id,
        "order_id": item.order_id,
        "symbol": item.symbol,
        "market": item.market,
        "side": item.side,
        "classification": item.classification,
        "nxt_classification": None,
        "nxt_actionable": item.nxt_actionable,
        "gap_pct": item.gap_pct,
        "reasons": list(item.reasons),
        "warnings": list(item.warnings),
        "decision_support": dict(item.decision_support),
        "summary": summary,
    }


def reconciliation_create_from_nxt(
    item: NxtClassifierItem,
    *,
    candidate_id: int | None = None,
    market: str = "kr",
) -> PendingReconciliationCreate:
    if item.kind != "pending_order":
        raise ValueError(
            "reconciliation_create_from_nxt only accepts pending_order kind; "
            "candidates and holdings are persisted via add_research_run_candidates"
        )
    return {
        "candidate_id": candidate_id,
        "order_id": item.item_id,
        "symbol": item.symbol,
        "market": market,
        "side": item.side or "buy",
        "classification": "unknown",  # caller may overwrite if they also have ROB-22 result
        "nxt_classification": item.classification,
        "nxt_actionable": item.nxt_actionable,
        "gap_pct": None,
        "reasons": list(item.reasons),
        "warnings": list(item.warnings),
        "decision_support": dict(item.decision_support),
        "summary": item.summary,
    }
```

Why two adapters: ROB-22 produces a `Classification`, ROB-23 produces an `NxtClassification`. Most KR NXT live-refresh flows will run both and write a row with both populated; some flows (US, crypto) will only run ROB-22. Callers can also build the dict themselves — these are conveniences.

---

## ORM Skeleton

```python
# app/models/research_run.py
from __future__ import annotations
import enum
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.trading import InstrumentType


class ResearchRunStatus(enum.StrEnum):
    open = "open"
    closed = "closed"
    archived = "archived"


class ResearchRunStage(enum.StrEnum):
    preopen = "preopen"
    intraday = "intraday"
    nxt_aftermarket = "nxt_aftermarket"
    us_open = "us_open"


class ResearchRunMarketScope(enum.StrEnum):
    kr = "kr"
    us = "us"
    crypto = "crypto"


class ResearchRunCandidateKind(enum.StrEnum):
    pending_order = "pending_order"
    holding = "holding"
    screener_hit = "screener_hit"
    proposed = "proposed"
    other = "other"


_RECON_CLASSIFICATIONS = (
    "maintain",
    "near_fill",
    "too_far",
    "chasing_risk",
    "data_mismatch",
    "kr_pending_non_nxt",
    "unknown_venue",
    "unknown",
)
_NXT_CLASSIFICATIONS = (
    "buy_pending_at_support",
    "buy_pending_too_far",
    "buy_pending_actionable",
    "sell_pending_near_resistance",
    "sell_pending_too_optimistic",
    "sell_pending_actionable",
    "non_nxt_pending_ignore_for_nxt",
    "holding_watch_only",
    "data_mismatch_requires_review",
    "unknown",
)


class ResearchRun(Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'closed', 'archived')",
            name="research_runs_status_allowed",
        ),
        CheckConstraint(
            "stage IN ('preopen', 'intraday', 'nxt_aftermarket', 'us_open')",
            name="research_runs_stage_allowed",
        ),
        CheckConstraint(
            "market_scope IN ('kr', 'us', 'crypto')",
            name="research_runs_market_scope_allowed",
        ),
        Index(
            "ix_research_runs_user_generated_at",
            "user_id",
            "generated_at",
            postgresql_using="btree",
            postgresql_ops={"generated_at": "DESC"},
        ),
        Index(
            "ix_research_runs_market_stage_generated_at",
            "market_scope",
            "stage",
            "generated_at",
            postgresql_ops={"generated_at": "DESC"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, index=True, default=uuid4
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    market_scope: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    source_profile: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_name: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    market_brief: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_freshness: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    advisory_links: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
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

    candidates: Mapped[list["ResearchRunCandidate"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    reconciliations: Mapped[list["ResearchRunPendingReconciliation"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ResearchRunCandidate(Base):
    __tablename__ = "research_run_candidates"
    __table_args__ = (
        CheckConstraint(
            "side IN ('buy','sell','none')",
            name="research_run_candidates_side_allowed",
        ),
        CheckConstraint(
            "candidate_kind IN ('pending_order','holding','screener_hit','proposed','other')",
            name="research_run_candidates_kind_allowed",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence BETWEEN 0 AND 100)",
            name="research_run_candidates_confidence_range",
        ),
        Index(
            "ix_research_run_candidates_run_symbol",
            "research_run_id",
            "symbol",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    candidate_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, index=True, default=uuid4
    )
    research_run_id: Mapped[int] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )
    side: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    candidate_kind: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    proposed_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    confidence: Mapped[int | None] = mapped_column(SmallInteger)
    rationale: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(Text)
    source_freshness: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
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

    run: Mapped[ResearchRun] = relationship(back_populates="candidates")


class ResearchRunPendingReconciliation(Base):
    __tablename__ = "research_run_pending_reconciliations"
    __table_args__ = (
        CheckConstraint(
            "side IN ('buy','sell')",
            name="research_run_pending_reconciliations_side_allowed",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="research_run_pending_reconciliations_market_allowed",
        ),
        CheckConstraint(
            "classification IN ("
            "'maintain','near_fill','too_far','chasing_risk',"
            "'data_mismatch','kr_pending_non_nxt','unknown_venue','unknown')",
            name="research_run_pending_reconciliations_classification_allowed",
        ),
        CheckConstraint(
            "nxt_classification IS NULL OR nxt_classification IN ("
            "'buy_pending_at_support','buy_pending_too_far','buy_pending_actionable',"
            "'sell_pending_near_resistance','sell_pending_too_optimistic',"
            "'sell_pending_actionable','non_nxt_pending_ignore_for_nxt',"
            "'holding_watch_only','data_mismatch_requires_review','unknown')",
            name="research_run_pending_reconciliations_nxt_classification_allowed",
        ),
        Index(
            "ix_research_run_pending_reconciliations_run_symbol",
            "research_run_id",
            "symbol",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    research_run_id: Mapped[int] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_run_candidates.id", ondelete="SET NULL")
    )
    order_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)
    nxt_classification: Mapped[str | None] = mapped_column(Text)
    nxt_actionable: Mapped[bool | None] = mapped_column(Boolean)
    gap_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    decision_support: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[ResearchRun] = relationship(back_populates="reconciliations")
```

---

## Pydantic Schemas Skeleton

```python
# app/schemas/research_run.py
from __future__ import annotations
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.trading_decisions import InstrumentTypeLiteral, SideLiteral

MarketScopeLiteral = Literal["kr", "us", "crypto"]
StageLiteral = Literal["preopen", "intraday", "nxt_aftermarket", "us_open"]
RunStatusLiteral = Literal["open", "closed", "archived"]
CandidateKindLiteral = Literal[
    "pending_order", "holding", "screener_hit", "proposed", "other"
]
ReconClassificationLiteral = Literal[
    "maintain",
    "near_fill",
    "too_far",
    "chasing_risk",
    "data_mismatch",
    "kr_pending_non_nxt",
    "unknown_venue",
    "unknown",
]
NxtClassificationLiteral = Literal[
    "buy_pending_at_support",
    "buy_pending_too_far",
    "buy_pending_actionable",
    "sell_pending_near_resistance",
    "sell_pending_too_optimistic",
    "sell_pending_actionable",
    "non_nxt_pending_ignore_for_nxt",
    "holding_watch_only",
    "data_mismatch_requires_review",
    "unknown",
]

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/-]{1,32}$")
_WARNING_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _AdvisoryLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    advisory_only: Literal[True] = True
    execution_allowed: Literal[False] = False
    session_uuid: UUID | None = None
    note: str | None = Field(default=None, max_length=512)


class ResearchRunCandidateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=32)
    instrument_type: InstrumentTypeLiteral
    side: SideLiteral = "none"
    candidate_kind: CandidateKindLiteral
    proposed_price: Decimal | None = Field(default=None, ge=0)
    proposed_qty: Decimal | None = Field(default=None, ge=0)
    confidence: int | None = Field(default=None, ge=0, le=100)
    rationale: str | None = Field(default=None, max_length=4000)
    currency: str | None = Field(default=None, max_length=8)
    source_freshness: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _symbol_charset(cls, v: str) -> str:
        if not _SYMBOL_RE.fullmatch(v):
            raise ValueError("symbol contains unsupported characters")
        return v

    @field_validator("warnings")
    @classmethod
    def _warning_charset(cls, v: list[str]) -> list[str]:
        for token in v:
            if not _WARNING_RE.fullmatch(token):
                raise ValueError(f"warning token not allowed: {token}")
        return v


class ResearchRunPendingReconciliationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: int | None = None
    order_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    market: MarketScopeLiteral
    side: Literal["buy", "sell"]
    classification: ReconClassificationLiteral
    nxt_classification: NxtClassificationLiteral | None = None
    nxt_actionable: bool | None = None
    gap_pct: Decimal | None = None
    reasons: list[str] = Field(default_factory=list, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=64)
    decision_support: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = Field(default=None, max_length=512)


class ResearchRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market_scope: MarketScopeLiteral
    stage: StageLiteral
    source_profile: str = Field(min_length=1, max_length=64)
    strategy_name: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    market_brief: dict[str, Any] | None = None
    source_freshness: dict[str, Any] | None = None
    source_warnings: list[str] = Field(default_factory=list, max_length=64)
    advisory_links: list[_AdvisoryLink] = Field(default_factory=list, max_length=20)
    generated_at: datetime
    candidates: list[ResearchRunCandidateCreate] = Field(default_factory=list, max_length=200)


class ResearchRunSummary(BaseModel):
    run_uuid: UUID
    market_scope: MarketScopeLiteral
    stage: StageLiteral
    status: RunStatusLiteral
    source_profile: str
    strategy_name: str | None
    generated_at: datetime
    candidate_count: int
    reconciliation_count: int
    source_warnings: list[str]


class ResearchRunDetail(ResearchRunSummary):
    notes: str | None
    market_brief: dict[str, Any] | None
    source_freshness: dict[str, Any] | None
    advisory_links: list[dict[str, Any]]
    candidates: list[ResearchRunCandidateCreate]
    reconciliations: list[ResearchRunPendingReconciliationCreate]
```

---

## Tasks

### Task 1: Scaffolding — empty model module + `__init__` re-export

**Files:**
- Create: `app/models/research_run.py`
- Modify: `app/models/__init__.py`

- [ ] **Step 1: Create the empty model module** (placeholders that import only `Base` so the migration generator picks them up)

```python
# app/models/research_run.py
"""Research Run snapshot ORM models (ROB-24).

Read-only / decision-support persistence. These rows store candidates,
pending-reconciliation outputs, and source-freshness metadata for KR/NXT
preparation. They never represent broker order state.
"""

from __future__ import annotations
```

(Body filled in Task 3.)

- [ ] **Step 2: Add re-exports to `app/models/__init__.py`**

After the `from .trading_decision import (...)` block (`app/models/__init__.py:38-50`), insert:

```python
from .research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunCandidateKind,
    ResearchRunMarketScope,
    ResearchRunPendingReconciliation,
    ResearchRunStage,
    ResearchRunStatus,
)
```

And extend `__all__` (`app/models/__init__.py:59-122`) with:

```python
    "ResearchRun",
    "ResearchRunCandidate",
    "ResearchRunPendingReconciliation",
    "ResearchRunStatus",
    "ResearchRunStage",
    "ResearchRunMarketScope",
    "ResearchRunCandidateKind",
```

- [ ] **Step 3: Sanity-import — verify nothing else broke**

Run: `uv run python -c "from app.models import ResearchRun, ResearchRunCandidate, ResearchRunPendingReconciliation; print('ok')"`
Expected: prints `ok` (no `ImportError`).

NOTE: At this step the names will not yet be defined (Task 3 creates them). Re-run this command after Task 3.

- [ ] **Step 4: Commit**

```bash
git add app/models/research_run.py app/models/__init__.py
git commit -m "feat(rob-24): scaffold ResearchRun model module"
```

---

### Task 2: Pydantic schemas + unit tests

**Files:**
- Create: `app/schemas/research_run.py`
- Create: `tests/test_research_run_schemas.py`

- [ ] **Step 1: Write the failing schema tests first**

```python
# tests/test_research_run_schemas.py
"""Unit tests for app.schemas.research_run."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.research_run import (
    ResearchRunCandidateCreate,
    ResearchRunCreate,
    ResearchRunPendingReconciliationCreate,
)


@pytest.mark.unit
def test_run_create_minimum_fields() -> None:
    payload = ResearchRunCreate(
        market_scope="kr",
        stage="preopen",
        source_profile="kr_morning_brief",
        generated_at=datetime.now(UTC),
    )
    assert payload.market_scope == "kr"
    assert payload.stage == "preopen"
    assert payload.advisory_links == []
    assert payload.source_warnings == []


@pytest.mark.unit
def test_run_create_rejects_unknown_stage() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="kr",
            stage="not_a_stage",
            source_profile="x",
            generated_at=datetime.now(UTC),
        )


@pytest.mark.unit
def test_run_create_rejects_unknown_market_scope() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="forex",
            stage="preopen",
            source_profile="x",
            generated_at=datetime.now(UTC),
        )


@pytest.mark.unit
def test_run_create_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="kr",
            stage="preopen",
            source_profile="x",
            generated_at=datetime.now(UTC),
            unexpected="bad",
        )


@pytest.mark.unit
def test_advisory_link_must_be_advisory_only() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="kr",
            stage="preopen",
            source_profile="x",
            generated_at=datetime.now(UTC),
            advisory_links=[
                {"advisory_only": False, "execution_allowed": False, "session_uuid": str(uuid4())}
            ],
        )
    with pytest.raises(ValidationError):
        ResearchRunCreate(
            market_scope="kr",
            stage="preopen",
            source_profile="x",
            generated_at=datetime.now(UTC),
            advisory_links=[
                {"advisory_only": True, "execution_allowed": True, "session_uuid": str(uuid4())}
            ],
        )


@pytest.mark.unit
def test_candidate_create_symbol_charset() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCandidateCreate(
            symbol="bad symbol with spaces",
            instrument_type="equity_kr",
            candidate_kind="screener_hit",
        )


@pytest.mark.unit
def test_candidate_create_confidence_range() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCandidateCreate(
            symbol="005930",
            instrument_type="equity_kr",
            candidate_kind="screener_hit",
            confidence=150,
        )


@pytest.mark.unit
def test_candidate_create_warning_charset() -> None:
    with pytest.raises(ValidationError):
        ResearchRunCandidateCreate(
            symbol="005930",
            instrument_type="equity_kr",
            candidate_kind="screener_hit",
            warnings=["BAD-WARNING"],  # uppercase / hyphen not allowed
        )


@pytest.mark.unit
def test_pending_reconciliation_create_required_fields() -> None:
    item = ResearchRunPendingReconciliationCreate(
        order_id="O1",
        symbol="005930",
        market="kr",
        side="buy",
        classification="maintain",
        decision_support={"current_price": "70000.0", "gap_pct": "0.0"},
    )
    assert item.classification == "maintain"
    assert item.nxt_classification is None
    assert item.gap_pct is None


@pytest.mark.unit
def test_pending_reconciliation_create_with_nxt() -> None:
    item = ResearchRunPendingReconciliationCreate(
        order_id="O2",
        symbol="005930",
        market="kr",
        side="sell",
        classification="maintain",
        nxt_classification="sell_pending_near_resistance",
        nxt_actionable=True,
        gap_pct=Decimal("0.42"),
        summary="NXT 매도 대기 — 저항선 근접 (저항선 71000)",
    )
    assert item.nxt_classification == "sell_pending_near_resistance"
    assert item.nxt_actionable is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_research_run_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: app.schemas.research_run`

- [ ] **Step 3: Implement the schemas**

Create `app/schemas/research_run.py` with the body from the "Pydantic Schemas Skeleton" section above. Ensure `ConfigDict(extra="forbid")` on every BaseModel.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_research_run_schemas.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/research_run.py tests/test_research_run_schemas.py
git commit -m "feat(rob-24): add Pydantic schemas for Research Run snapshot persistence"
```

---

### Task 3: ORM models — bodies + tests

**Files:**
- Modify: `app/models/research_run.py`
- Create: `tests/models/test_research_run_models.py`

- [ ] **Step 1: Write the failing model integration test**

```python
# tests/models/test_research_run_models.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.research_run import (
    ResearchRun,
    ResearchRunCandidate,
    ResearchRunPendingReconciliation,
)
from app.models.trading import InstrumentType

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_research_run_tables() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(text("SELECT to_regclass('research_runs')"))
            if row.scalar_one_or_none() is None:
                pytest.skip("research_run tables are not migrated")
    except Exception:
        pytest.skip("database is not available for integration persistence checks")


async def _create_user() -> int:
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        user_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (username, email, role, tz, base_currency, is_active)
                    VALUES (:username, :email, 'viewer', 'Asia/Seoul', 'KRW', true)
                    RETURNING id
                    """
                ),
                {
                    "username": f"rob24_test_{suffix}",
                    "email": f"rob24_{suffix}@example.com",
                },
            )
        ).scalar_one()
        await session.commit()
        return user_id


async def _cleanup_user(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id}
        )
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_research_run_round_trip_with_candidate_and_reconciliation() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="kr",
                stage="nxt_aftermarket",
                source_profile="hermes",
                strategy_name="nxt_test",
                source_freshness={
                    "quote_as_of": "2026-04-28T05:00:00+00:00",
                    "kr_universe_synced_at": "2026-04-28T04:30:00+00:00",
                },
                source_warnings=["missing_orderbook"],
                advisory_links=[],
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()

            candidate = ResearchRunCandidate(
                research_run_id=run.id,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="buy",
                candidate_kind="screener_hit",
                proposed_price=Decimal("70000"),
                proposed_qty=Decimal("10"),
                confidence=72,
                rationale="dummy",
                currency="KRW",
                source_freshness=None,
                warnings=[],
                payload={"source": "test"},
            )
            session.add(candidate)
            await session.flush()

            recon = ResearchRunPendingReconciliation(
                research_run_id=run.id,
                candidate_id=candidate.id,
                order_id="ORDER-1",
                symbol="005930",
                market="kr",
                side="buy",
                classification="maintain",
                nxt_classification="buy_pending_actionable",
                nxt_actionable=True,
                gap_pct=Decimal("0.10"),
                reasons=["gap_within_near_fill_pct"],
                warnings=[],
                decision_support={"current_price": "70070.0", "gap_pct": "0.1"},
                summary="NXT 매수 대기 — 적정 (지속 모니터링)",
            )
            session.add(recon)
            await session.commit()

            assert run.run_uuid is not None
            assert candidate.candidate_uuid is not None
            assert recon.id is not None
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_research_run_stage_check_rejects_unknown_value() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="kr",
                stage="not_a_stage",  # invalid
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_research_run_market_scope_check_rejects_unknown_value() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="forex",  # not in (kr, us, crypto)
                stage="preopen",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recon_classification_check_rejects_unknown_value() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="kr",
                stage="intraday",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            recon = ResearchRunPendingReconciliation(
                research_run_id=run.id,
                order_id="O1",
                symbol="005930",
                market="kr",
                side="buy",
                classification="bogus",  # invalid
                decision_support={},
            )
            session.add(recon)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cascade_delete_run_removes_children() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        run_id: int
        async with SessionLocal() as session:
            run = ResearchRun(
                user_id=user_id,
                market_scope="kr",
                stage="preopen",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            run_id = run.id
            session.add(
                ResearchRunCandidate(
                    research_run_id=run.id,
                    symbol="005930",
                    instrument_type=InstrumentType.equity_kr,
                    candidate_kind="screener_hit",
                )
            )
            session.add(
                ResearchRunPendingReconciliation(
                    research_run_id=run.id,
                    order_id="O1",
                    symbol="005930",
                    market="kr",
                    side="buy",
                    classification="maintain",
                    decision_support={},
                )
            )
            await session.commit()

        async with SessionLocal() as session:
            await session.execute(
                text("DELETE FROM research_runs WHERE id = :id"), {"id": run_id}
            )
            await session.commit()

            cand_count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM research_run_candidates WHERE research_run_id = :id"),
                    {"id": run_id},
                )
            ).scalar_one()
            recon_count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM research_run_pending_reconciliations WHERE research_run_id = :id"),
                    {"id": run_id},
                )
            ).scalar_one()
            assert cand_count == 0
            assert recon_count == 0
    finally:
        await _cleanup_user(user_id)
```

- [ ] **Step 2: Run model tests to verify they fail (no migration yet, no model body yet)**

Run: `uv run pytest tests/models/test_research_run_models.py -v`
Expected: All tests skipped (`research_run tables are not migrated`) — that is the intended pre-migration state. They will turn green after Task 4 + 5 + 6.

- [ ] **Step 3: Fill in the ORM model bodies**

Replace the placeholder body of `app/models/research_run.py` with the contents of the "ORM Skeleton" section above. Ensure:
- `instrument_type` reuses the existing PG enum via `Enum(InstrumentType, name="instrument_type", create_type=False)`.
- All JSONB list/dict columns have `default=list` / `default=dict` and `server_default="[]"` / `server_default="{}"` so older rows never appear NULL through the ORM.
- Composite indexes use `postgresql_ops={"generated_at": "DESC"}`.
- `relationship(..., cascade="all, delete-orphan")` matches the FK `ondelete="CASCADE"`.

- [ ] **Step 4: Sanity-import the new symbols**

Run: `uv run python -c "from app.models import ResearchRun, ResearchRunCandidate, ResearchRunPendingReconciliation; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 5: Commit (do not run model integration tests yet — they need the migration)**

```bash
git add app/models/research_run.py tests/models/test_research_run_models.py
git commit -m "feat(rob-24): add ResearchRun ORM models with check constraints and JSONB defaults"
```

---

### Task 4: Alembic migration — create three tables

**Files:**
- Create: `alembic/versions/<rev>_add_research_run_tables.py`

- [ ] **Step 1: Confirm current head before generating**

Run: `uv run alembic heads`
Expected output line: `ce5d470cc894 (head)`. (If the head differs, stop and ask — the migration must descend from the current head.)

- [ ] **Step 2: Generate the migration scaffold**

Run: `uv run alembic revision -m "add research run tables"`
Expected: a new file appears under `alembic/versions/<rev>_add_research_run_tables.py` with `down_revision = 'ce5d470cc894'` (or the current head).

NOTE: do **not** use `--autogenerate`. The migration is hand-rolled to keep check constraints and JSONB server defaults explicit and reviewable. Autogenerate's diff also tends to drop / recreate the existing `instrument_type` enum, which we do not want.

- [ ] **Step 3: Replace the migration body**

Replace `upgrade()` and `downgrade()` with the following (preserving the auto-generated `revision` / `down_revision` lines):

```python
"""add research run tables

Revision ID: <rev>
Revises: ce5d470cc894
Create Date: <auto>
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "<rev>"
down_revision: str | Sequence[str] | None = "ce5d470cc894"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

instrument_type_enum = postgresql.ENUM(
    "equity_kr", "equity_us", "crypto", "forex", "index",
    name="instrument_type",
    create_type=False,  # already exists; do not recreate
)


def upgrade() -> None:
    # 1. research_runs
    op.create_table(
        "research_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("market_scope", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("source_profile", sa.Text(), nullable=False),
        sa.Column("strategy_name", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("market_brief", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_freshness", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "source_warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "advisory_links",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed', 'archived')", name="research_runs_status_allowed"),
        sa.CheckConstraint(
            "stage IN ('preopen', 'intraday', 'nxt_aftermarket', 'us_open')",
            name="research_runs_stage_allowed",
        ),
        sa.CheckConstraint(
            "market_scope IN ('kr', 'us', 'crypto')",
            name="research_runs_market_scope_allowed",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_uuid"),
    )
    op.create_index(
        "ix_research_runs_user_generated_at",
        "research_runs",
        ["user_id", sa.text("generated_at DESC")],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_research_runs_market_stage_generated_at",
        "research_runs",
        ["market_scope", "stage", sa.text("generated_at DESC")],
    )
    op.create_index(op.f("ix_research_runs_run_uuid"), "research_runs", ["run_uuid"], unique=True)
    op.create_index(op.f("ix_research_runs_user_id"), "research_runs", ["user_id"], unique=False)
    op.create_foreign_key(
        None, "research_runs", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )

    # 2. research_run_candidates
    op.create_table(
        "research_run_candidates",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("research_run_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.Text(), nullable=False, server_default="none"),
        sa.Column("candidate_kind", sa.Text(), nullable=False),
        sa.Column("proposed_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("proposed_qty", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("confidence", sa.SmallInteger(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("source_freshness", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("side IN ('buy','sell','none')", name="research_run_candidates_side_allowed"),
        sa.CheckConstraint(
            "candidate_kind IN ('pending_order','holding','screener_hit','proposed','other')",
            name="research_run_candidates_kind_allowed",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence BETWEEN 0 AND 100)",
            name="research_run_candidates_confidence_range",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_uuid"),
    )
    op.create_index(
        op.f("ix_research_run_candidates_candidate_uuid"),
        "research_run_candidates",
        ["candidate_uuid"],
        unique=True,
    )
    op.create_index(
        op.f("ix_research_run_candidates_research_run_id"),
        "research_run_candidates",
        ["research_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_run_candidates_symbol"),
        "research_run_candidates",
        ["symbol"],
        unique=False,
    )
    op.create_index(
        "ix_research_run_candidates_run_symbol",
        "research_run_candidates",
        ["research_run_id", "symbol"],
        unique=False,
    )
    op.create_foreign_key(
        None,
        "research_run_candidates",
        "research_runs",
        ["research_run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 3. research_run_pending_reconciliations
    op.create_table(
        "research_run_pending_reconciliations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("research_run_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=True),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("nxt_classification", sa.Text(), nullable=True),
        sa.Column("nxt_actionable", sa.Boolean(), nullable=True),
        sa.Column("gap_pct", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "decision_support",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("side IN ('buy','sell')", name="research_run_pending_reconciliations_side_allowed"),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="research_run_pending_reconciliations_market_allowed",
        ),
        sa.CheckConstraint(
            "classification IN ("
            "'maintain','near_fill','too_far','chasing_risk',"
            "'data_mismatch','kr_pending_non_nxt','unknown_venue','unknown')",
            name="research_run_pending_reconciliations_classification_allowed",
        ),
        sa.CheckConstraint(
            "nxt_classification IS NULL OR nxt_classification IN ("
            "'buy_pending_at_support','buy_pending_too_far','buy_pending_actionable',"
            "'sell_pending_near_resistance','sell_pending_too_optimistic',"
            "'sell_pending_actionable','non_nxt_pending_ignore_for_nxt',"
            "'holding_watch_only','data_mismatch_requires_review','unknown')",
            name="research_run_pending_reconciliations_nxt_classification_allowed",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_research_run_pending_reconciliations_research_run_id"),
        "research_run_pending_reconciliations",
        ["research_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_run_pending_reconciliations_order_id"),
        "research_run_pending_reconciliations",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_run_pending_reconciliations_symbol"),
        "research_run_pending_reconciliations",
        ["symbol"],
        unique=False,
    )
    op.create_index(
        "ix_research_run_pending_reconciliations_run_symbol",
        "research_run_pending_reconciliations",
        ["research_run_id", "symbol"],
        unique=False,
    )
    op.create_foreign_key(
        None,
        "research_run_pending_reconciliations",
        "research_runs",
        ["research_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "research_run_pending_reconciliations",
        "research_run_candidates",
        ["candidate_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_table("research_run_pending_reconciliations")
    op.drop_table("research_run_candidates")
    op.drop_table("research_runs")
```

- [ ] **Step 4: Apply the migration locally**

Run:
- `docker compose up -d postgres` (if not already running)
- `uv run alembic upgrade head`

Expected: `Running upgrade ce5d470cc894 -> <rev>, add research run tables`. No errors.

- [ ] **Step 5: Verify rollback safety**

Run:
- `uv run alembic downgrade -1`
- `uv run alembic upgrade head`

Expected: both succeed. `\d research_runs` after upgrade shows the table; `\d research_runs` after downgrade returns "Did not find any relation".

Optional: `psql ... -c "\d research_runs"` to spot-check column types and check constraints.

- [ ] **Step 6: Run model integration tests (now that tables exist)**

Run: `uv run pytest tests/models/test_research_run_models.py -v`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/<rev>_add_research_run_tables.py
git commit -m "feat(rob-24): add Alembic migration for research_runs tables"
```

**Migration safety / rollback notes:**
- The migration only `CREATE TABLE`s; it never alters or drops existing data. `downgrade()` `DROP TABLE`s in reverse FK order. Down-migrating is safe at any time before any production row is written.
- The `instrument_type` enum is reused with `create_type=False`. The migration must not attempt to create or drop this enum — doing so would break `trading_decision_proposals`, `paper_trades`, etc.
- JSONB columns marked `nullable=False` get a `server_default` of `'[]'::jsonb` or `'{}'::jsonb` so that any future migration adding rows via raw SQL (without going through SQLAlchemy) does not crash on missing fields.
- Once a row exists in `research_runs` in production and we want to remove the tables, the safer approach is a follow-up migration that backs the rows up to a JSONB archive table first; the bare `downgrade()` here is intended for local dev iteration, not production rollback after data has been written.

---

### Task 5: Persistence service — `create_research_run`, `add_research_run_candidates`, `attach_pending_reconciliations`, helpers

**Files:**
- Create: `app/services/research_run_service.py`
- Create: `tests/services/test_research_run_service.py`

- [ ] **Step 1: Write the failing service integration test**

```python
# tests/services/test_research_run_service.py
"""Integration tests for app.services.research_run_service."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.trading import InstrumentType
from app.services.nxt_classifier_service import (
    NxtClassifierItem,
)
from app.services.pending_reconciliation_service import (
    PendingReconciliationItem,
)
from app.services.research_run_service import (
    add_research_run_candidates,
    attach_pending_reconciliations,
    create_research_run,
    get_research_run_by_uuid,
    list_user_research_runs,
    reconciliation_create_from_nxt,
    reconciliation_create_from_recon,
)

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_research_run_tables() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(text("SELECT to_regclass('research_runs')"))
            if row.scalar_one_or_none() is None:
                pytest.skip("research_run tables are not migrated")
    except Exception:
        pytest.skip("database is not available for integration persistence checks")


async def _create_user() -> int:
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        user_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (username, email, role, tz, base_currency, is_active)
                    VALUES (:username, :email, 'viewer', 'Asia/Seoul', 'KRW', true)
                    RETURNING id
                    """
                ),
                {
                    "username": f"rob24_svc_{suffix}",
                    "email": f"rob24_svc_{suffix}@example.com",
                },
            )
        ).scalar_one()
        await session.commit()
        return user_id


async def _cleanup_user(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id}
        )
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_research_run_with_candidates_and_reconciliations() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = await create_research_run(
                session,
                user_id=user_id,
                market_scope="kr",
                stage="nxt_aftermarket",
                source_profile="hermes",
                source_freshness={"quote_as_of": "2026-04-28T05:00:00+00:00"},
                source_warnings=["missing_orderbook"],
                advisory_links=[
                    {
                        "advisory_only": True,
                        "execution_allowed": False,
                        "session_uuid": str(uuid.uuid4()),
                    }
                ],
                generated_at=datetime.now(UTC),
            )
            cands = await add_research_run_candidates(
                session,
                research_run_id=run.id,
                candidates=[
                    {
                        "symbol": "005930",
                        "instrument_type": InstrumentType.equity_kr,
                        "side": "buy",
                        "candidate_kind": "screener_hit",
                        "proposed_price": Decimal("70000"),
                        "proposed_qty": Decimal("10"),
                        "confidence": 72,
                        "currency": "KRW",
                        "warnings": [],
                        "payload": {"src": "test"},
                    }
                ],
            )
            recons = await attach_pending_reconciliations(
                session,
                research_run_id=run.id,
                items=[
                    {
                        "candidate_id": cands[0].id,
                        "order_id": "ORDER-1",
                        "symbol": "005930",
                        "market": "kr",
                        "side": "buy",
                        "classification": "maintain",
                        "nxt_classification": "buy_pending_actionable",
                        "nxt_actionable": True,
                        "gap_pct": Decimal("0.10"),
                        "reasons": ["gap_within_near_fill_pct"],
                        "warnings": [],
                        "decision_support": {"current_price": "70070.0"},
                        "summary": "NXT 매수 대기 — 적정 (지속 모니터링)",
                    }
                ],
            )
            await session.commit()

            assert run.run_uuid is not None
            assert run.source_warnings == ["missing_orderbook"]
            assert len(cands) == 1
            assert len(recons) == 1
            assert recons[0].candidate_id == cands[0].id
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_research_run_by_uuid_enforces_ownership() -> None:
    await _ensure_research_run_tables()
    owner_id = await _create_user()
    other_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = await create_research_run(
                session,
                user_id=owner_id,
                market_scope="kr",
                stage="preopen",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            await session.commit()

        async with SessionLocal() as session:
            owned = await get_research_run_by_uuid(
                session, run_uuid=run.run_uuid, user_id=owner_id
            )
            other = await get_research_run_by_uuid(
                session, run_uuid=run.run_uuid, user_id=other_id
            )
            assert owned is not None
            assert owned.id == run.id
            assert other is None
    finally:
        await _cleanup_user(owner_id)
        await _cleanup_user(other_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_user_research_runs_filters_and_counts() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run_kr = await create_research_run(
                session,
                user_id=user_id,
                market_scope="kr",
                stage="nxt_aftermarket",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            run_us = await create_research_run(
                session,
                user_id=user_id,
                market_scope="us",
                stage="us_open",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            await add_research_run_candidates(
                session,
                research_run_id=run_kr.id,
                candidates=[
                    {
                        "symbol": "005930",
                        "instrument_type": InstrumentType.equity_kr,
                        "candidate_kind": "screener_hit",
                        "warnings": [],
                        "payload": {},
                    }
                ],
            )
            await session.commit()

        async with SessionLocal() as session:
            rows_all, total_all = await list_user_research_runs(
                session, user_id=user_id
            )
            rows_kr, total_kr = await list_user_research_runs(
                session, user_id=user_id, market_scope="kr"
            )
            rows_us, total_us = await list_user_research_runs(
                session, user_id=user_id, market_scope="us"
            )

            assert total_all == 2
            assert total_kr == 1
            assert total_us == 1
            kr_row = next(r for r in rows_kr if r[0].id == run_kr.id)
            us_row = next(r for r in rows_us if r[0].id == run_us.id)
            assert kr_row[1] == 1  # candidate_count
            assert kr_row[2] == 0  # reconciliation_count
            assert us_row[1] == 0
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adapter_from_recon_round_trip() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = await create_research_run(
                session,
                user_id=user_id,
                market_scope="kr",
                stage="intraday",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            recon_item = PendingReconciliationItem(
                order_id="O42",
                symbol="005930",
                market="kr",
                side="buy",
                classification="near_fill",
                nxt_actionable=True,
                gap_pct=Decimal("0.20"),
                reasons=("gap_within_near_fill_pct",),
                warnings=("missing_orderbook",),
                decision_support={
                    "current_price": Decimal("70140"),
                    "gap_pct": Decimal("0.20"),
                    "signed_distance_to_fill": Decimal("-0.20"),
                    "nearest_support_price": None,
                    "nearest_support_distance_pct": None,
                    "nearest_resistance_price": None,
                    "nearest_resistance_distance_pct": None,
                    "bid_ask_spread_pct": None,
                },
            )
            payload = reconciliation_create_from_recon(recon_item)
            attached = await attach_pending_reconciliations(
                session,
                research_run_id=run.id,
                items=[payload],
            )
            await session.commit()

            assert attached[0].classification == "near_fill"
            assert attached[0].nxt_classification is None
            assert attached[0].warnings == ["missing_orderbook"]
            assert "current_price" in attached[0].decision_support
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adapter_from_nxt_round_trip() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            run = await create_research_run(
                session,
                user_id=user_id,
                market_scope="kr",
                stage="nxt_aftermarket",
                source_profile="hermes",
                generated_at=datetime.now(UTC),
            )
            nxt_item = NxtClassifierItem(
                item_id="O99",
                symbol="005930",
                kind="pending_order",
                side="sell",
                classification="sell_pending_near_resistance",
                nxt_actionable=True,
                summary="NXT 매도 대기 — 저항선 근접 (저항선 71000)",
                reasons=("order_within_near_resistance_pct",),
                warnings=(),
                decision_support={
                    "current_price": Decimal("70900"),
                    "gap_pct": None,
                    "signed_distance_to_fill": None,
                    "nearest_support_price": None,
                    "nearest_support_distance_pct": None,
                    "nearest_resistance_price": Decimal("71000"),
                    "nearest_resistance_distance_pct": Decimal("0.14"),
                    "bid_ask_spread_pct": None,
                },
            )
            payload = reconciliation_create_from_nxt(nxt_item)
            attached = await attach_pending_reconciliations(
                session,
                research_run_id=run.id,
                items=[payload],
            )
            await session.commit()

            assert attached[0].nxt_classification == "sell_pending_near_resistance"
            assert attached[0].summary.startswith("NXT 매도 대기")
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_research_run_rejects_non_advisory_link() -> None:
    await _ensure_research_run_tables()
    user_id = await _create_user()
    try:
        async with SessionLocal() as session:
            with pytest.raises(ValueError):
                await create_research_run(
                    session,
                    user_id=user_id,
                    market_scope="kr",
                    stage="preopen",
                    source_profile="hermes",
                    advisory_links=[
                        {
                            "advisory_only": True,
                            "execution_allowed": True,  # invariant violation
                        }
                    ],
                    generated_at=datetime.now(UTC),
                )
    finally:
        await _cleanup_user(user_id)
```

- [ ] **Step 2: Run service tests to verify they fail**

Run: `uv run pytest tests/services/test_research_run_service.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.research_run_service`.

- [ ] **Step 3: Implement the service**

Create `app/services/research_run_service.py` with the public API from the "Public API of the Service" section above and the adapter helpers from "Domain DTO Adapters".

Key implementation rules — must hold:
- Reject `advisory_links` entries that are not `{"advisory_only": True, "execution_allowed": False, ...}` by raising `ValueError("advisory_links must be advisory-only with execution_allowed=False")`. Tested by `test_create_research_run_rejects_non_advisory_link`.
- All inserts go through `session.add` / `session.flush` / `session.refresh`. The service does **not** call `session.commit()` — callers own the transaction (matches `trading_decision_service` style).
- `list_user_research_runs` uses `LEFT OUTER JOIN` + grouped counts to compute `candidate_count` and `reconciliation_count` in one query (no N+1).
- `get_research_run_by_uuid` enforces ownership by joining on `user_id` in the WHERE clause; returns `None` when not owned. Eager-loads candidates and reconciliations via `selectinload`.
- The service module imports only: `app.models.research_run`, `app.models.trading` (`InstrumentType`), `app.services.pending_reconciliation_service` (DTO types), `app.services.nxt_classifier_service` (DTO types), and `sqlalchemy.*`.

Skeleton signature reference (already shown above). Below is one notable invariant check:

```python
def _validate_advisory_links(links: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for link in links:
        if link.get("advisory_only") is not True or link.get("execution_allowed") is not False:
            raise ValueError(
                "advisory_links must be advisory-only with execution_allowed=False"
            )
        validated.append(dict(link))
    return validated
```

`create_research_run` must call `_validate_advisory_links(advisory_links)` before the `INSERT`.

- [ ] **Step 4: Run service tests to verify they pass**

Run: `uv run pytest tests/services/test_research_run_service.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/research_run_service.py tests/services/test_research_run_service.py
git commit -m "feat(rob-24): add Research Run persistence service with adapter helpers"
```

---

### Task 6: Service-import safety test

**Files:**
- Create: `tests/services/test_research_run_service_safety.py`

The pure-service safety helper (`tests/services/pure_service_safety.py`) forbids `sqlalchemy` and `app.core.db`, which we need. Reuse most of the helper but with a narrower forbidden list focused on broker / order / watch / paper / fill / websocket / Redis modules.

- [ ] **Step 1: Write the failing safety test**

```python
# tests/services/test_research_run_service_safety.py
"""Safety: research_run_service must not import broker/order/watch/paper/fill modules."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.upbit",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.orders",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_holdings_service",
    "app.services.upbit_websocket",
    "app.services.redis_token_manager",
    "app.services.n8n_pending_orders_service",
    "app.services.n8n_pending_review_service",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.orders_history",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.watch_alerts_registration",
    "app.tasks",
    "redis",
]


@pytest.mark.unit
def test_research_run_service_does_not_transitively_import_forbidden() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = """
import importlib
import json
import sys

importlib.import_module('app.services.research_run_service')
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        name
        for name in loaded
        for forbidden in FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert not violations, f"forbidden modules transitively imported: {violations}"
```

- [ ] **Step 2: Run safety test**

Run: `uv run pytest tests/services/test_research_run_service_safety.py -v`
Expected: PASS. (If it fails, the import chain has leaked a forbidden module — investigate before proceeding; do NOT silence by adding to an allowlist.)

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_research_run_service_safety.py
git commit -m "test(rob-24): assert research_run_service does not import broker/order modules"
```

---

### Task 7: Self-review pass

**Files:** none (review-only)

- [ ] **Step 1: Run the full new test set**

Run:
```
uv run pytest \
  tests/test_research_run_schemas.py \
  tests/models/test_research_run_models.py \
  tests/services/test_research_run_service.py \
  tests/services/test_research_run_service_safety.py \
  -v
```
Expected: all green.

- [ ] **Step 2: Run lint, type, and security checks**

Run:
- `make lint`
- `make typecheck`
- `make security` (only fail-on-introduced-issue; baseline noise is acceptable if pre-existing)

Expected: no new findings introduced by ROB-24 files.

- [ ] **Step 3: Verify no order/watch/paper API surface was added**

Run:
- `git diff main -- app/services/research_run_service.py | grep -E '(place_order|modify_order|cancel_order|manage_watch_alerts|paper_order|watch_alert|order_intent|fill_notification|register_watch)'` — expected: empty output.
- Same grep over `app/models/research_run.py` and the migration file — expected: empty output.

- [ ] **Step 4: Verify acceptance criteria from the issue (mark each)**

- [ ] AC1: Can create/load a Research Run with candidates and source freshness metadata.
  → covered by `test_create_research_run_with_candidates_and_reconciliations`, `test_get_research_run_by_uuid_enforces_ownership`.
- [ ] AC2: Can attach pending reconciliation outputs.
  → covered by `test_create_research_run_with_candidates_and_reconciliations`, `test_adapter_from_recon_round_trip`, `test_adapter_from_nxt_round_trip`.
- [ ] AC3: Records missing/stale source warnings.
  → covered by `source_warnings` round-trip in `test_create_research_run_with_candidates_and_reconciliations` and per-candidate `warnings` field; verifiable in `test_research_run_round_trip_with_candidate_and_reconciliation`.
- [ ] AC4: Does not create orders, watches, or order intents.
  → covered by `test_research_run_service_does_not_transitively_import_forbidden` and the `git diff` grep in Step 3.
- [ ] AC5: Stage values cover `preopen`, `intraday`, `nxt_aftermarket`, and future `us_open`.
  → covered by check-constraint test `test_research_run_stage_check_rejects_unknown_value`; valid stages exercised in service / model tests.
- [ ] AC6: Market scope supports `kr` end-to-end and accepts `us` / `crypto`.
  → covered by `test_research_run_market_scope_check_rejects_unknown_value` plus `test_list_user_research_runs_filters_and_counts` exercising both `kr` and `us`.

- [ ] **Step 5: Commit (if any review findings produced edits)**

```bash
git add -p
git commit -m "chore(rob-24): self-review fixes"
```

---

### Task 8: PR scope + smoke notes

**Files:** none (PR description only)

- [ ] **Step 1: Open PR with the following scope**

PR title: `feat(rob-24): persist Research Run snapshots for KR/NXT preparation`

PR description (template):

```
Closes ROB-24.

## Scope

Persistence-only first PR. No live-refresh wiring, no API endpoints, no UI, no Prefect.

- adds `research_runs`, `research_run_candidates`, `research_run_pending_reconciliations`
- adds `app/services/research_run_service.py` (async CRUD)
- adds `app/schemas/research_run.py` (Pydantic v2)
- adds Alembic migration descending from `ce5d470cc894`
- consumes ROB-22 / ROB-23 DTOs via two adapter helpers; never re-classifies

## Out of scope (explicit deferrals)

- ROB-20: live refresh wiring + UI rendering — separate PR.
- ROB-25: operator request → Research Run wiring — separate PR.
- `research_run_advisories` table: deferred; `research_runs.advisory_links` JSONB column carries TradingAgents references for now.

## Trading-safety guardrails

- Read-only persistence. No `place_order` / `modify_order` / `cancel_order` /
  `manage_watch_alerts` / paper-order / dry-run / watch registration / order
  intent introduced.
- `app.services.research_run_service` does not transitively import broker /
  order-execution / watch-alert / paper / fill-notification / KIS-websocket /
  Upbit-websocket / Redis modules. Enforced by
  `tests/services/test_research_run_service_safety.py`.
- `advisory_links` entries are validated to have
  `advisory_only=true / execution_allowed=false`.

## Migration

- Up-revision adds three tables; down-revision drops in reverse FK order.
- Reuses existing `instrument_type` enum (`create_type=False`).
- Local rollback verified with `alembic downgrade -1 && alembic upgrade head`.
- No data backfill. Safe to deploy because no existing flow writes to these
  tables yet.

## Tests

- `tests/test_research_run_schemas.py` — Pydantic unit tests.
- `tests/models/test_research_run_models.py` — DB-level CHECK / cascade tests.
- `tests/services/test_research_run_service.py` — CRUD, ownership, listing,
  adapter round-trips, advisory invariant.
- `tests/services/test_research_run_service_safety.py` — import boundary.

## Smoke

- Apply migration to staging: `uv run alembic upgrade head`.
- Verify `\dt research_run*` shows three tables.
- Insert a single test row via Python REPL with the service to confirm
  schema acceptance:
  - `create_research_run(... market_scope='kr', stage='nxt_aftermarket' ...)`.
- Roll forward only; do **not** roll back after a row exists.
```

- [ ] **Step 2: Tag reviewer**

No automated review hooks here. Surface in Hermes channel for review.

---

## Self-Review Checklist (planner)

**1. Spec coverage:**
- AC: create/load Research Run with candidates + source freshness — Tasks 2, 3, 5 (`source_freshness`, `add_research_run_candidates`).
- AC: attach pending reconciliations — Tasks 5 (`attach_pending_reconciliations`, adapter helpers).
- AC: missing/stale source warnings — Task 5 (`source_warnings` field, per-candidate `warnings`).
- AC: does not create orders/watches/intents — Task 6 (subprocess sys.modules safety test) + Task 7 grep.
- AC: stage values include `preopen`, `intraday`, `nxt_aftermarket`, future `us_open` — Tasks 3, 4 (CHECK constraint), Task 2 (Pydantic literal).
- AC: market scope supports `kr` (and design covers `us` / `crypto`) — Tasks 3, 4 (CHECK constraint), Task 5 list test.

**2. Placeholder scan:** All steps include actual code or actual command + expected output. No `TODO`s.

**3. Type consistency:**
- `Classification` literals match `app/services/pending_reconciliation_service.py:17-26`.
- `NxtClassification` literals match `app/services/nxt_classifier_service.py:31-42`.
- `InstrumentType` enum reused via `name="instrument_type", create_type=False`.
- Pydantic literals (`MarketScopeLiteral`, `StageLiteral`, `RunStatusLiteral`, `CandidateKindLiteral`, `ReconClassificationLiteral`, `NxtClassificationLiteral`) match the SQL CHECK constraints exactly.

**4. Naming:** `create_research_run`, `add_research_run_candidates`, `attach_pending_reconciliations`, `get_research_run_by_uuid`, `list_user_research_runs` — used consistently across schemas, service, and tests.

---

## Out of Scope (explicit deferrals)

| Item | Owner | Why deferred |
|------|-------|--------------|
| Live-refresh wiring (Research Run consumed by KR/NXT live refresh flow) | ROB-20 | Issue explicitly excluded ROB-20. |
| Router (`/api/research_runs`, `/api/research_runs/{uuid}`) | ROB-25 or follow-up | First PR delivers persistence only. |
| Operator-request → Research Run wiring | ROB-25 | Pairs with router. |
| Prefect flow integration | ROB-25 | Same. |
| UI templates (`research-run` SPA / dashboard) | ROB-25+ | Same. |
| `research_run_advisories` relational table | Follow-up | `advisory_links` JSONB covers the first need. |
| `accepted_paper` / `outcome` aggregation analogous to Decision Sessions | Follow-up | Different lifecycle; not in ROB-24 ACs. |
| Ingestion of ROB-22/23 DTOs from real broker context (live KR pendings, real KIS holdings) | ROB-25 | Wiring glue. |

If during implementation any of these are required to make a test pass, **stop and report it as a blocker**. Do not silently expand scope into ROB-20 territory.
