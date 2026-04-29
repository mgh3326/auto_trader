# ROB-41 Operator-Provided Market Events — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear Issue:** ROB-41 — Add operator-provided market events to strategy revision ledger
**Parent:** ROB-40 — Evolve preopen dashboard into intraday strategy decision ledger
**Branch / worktree:** `feature/ROB-41-operator-market-events` at `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-41-operator-market-events`

**Goal:** Add a durable, append-only operator-provided market event store with an authenticated create/read/query API, optionally linkable to an open `trading_decision_sessions` row, with strict no-side-effect guarantees.

**Architecture:** New SQLAlchemy model `TradingDecisionStrategyEvent` (lives in `app/models/trading_decision.py` next to the existing ledger models), a new Alembic migration creating `trading_decision_strategy_events` (append-only — no UPDATE/DELETE endpoints in this slice), Pydantic request/response schemas in `app/schemas/strategy_events.py`, a thin async service `app/services/strategy_event_service.py` (DB-only — no broker / order / watch / paper / TradingAgents-runner imports), and an authenticated FastAPI router `app/routers/strategy_events.py` mounted under `/trading/api/strategy-events`. A forbidden-import safety test (mirroring `tests/test_research_run_refresh_import_safety.py`) asserts the new modules cannot reach broker/order/watch/paper/live-execution code.

**Tech Stack:** Python 3.13+, SQLAlchemy 2.x async, PostgreSQL, Alembic, Pydantic v2, FastAPI, pytest, ruff, ty.

**Hard out-of-scope guardrails (DO NOT cross):**
- No live orders, no dry-run orders, no paper order execution.
- No watch alert registration / mutation.
- No order intent creation, no proposal mutation, no proposal action recording.
- No TradingAgents automatic rerun, no scheduler automation.
- No broker / KIS / Upbit calls — direct or transitive.
- No outcome analytics calculation.
- The new modules MUST NOT import any of: `app.services.kis_trading_service`, `app.services.kis_trading_contracts`, `app.services.order_service`, `app.services.orders`, `app.services.paper_trading_service`, `app.services.fill_notification`, `app.services.execution_event`, `app.services.crypto_trade_cooldown_service`, `app.services.kis_websocket`, `app.services.kis_websocket_internal`, `app.services.upbit_websocket`, `app.services.upbit_market_websocket`, `app.services.watch_alerts`, `app.services.screener_service`, `app.services.tradingagents_research_service`, `app.mcp_server.tooling.orders_registration`, `app.mcp_server.tooling.orders_modify_cancel`, `app.mcp_server.tooling.orders_history`, `app.mcp_server.tooling.paper_order_handler`, `app.mcp_server.tooling.watch_alerts_registration`, `prefect`.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `app/models/trading_decision.py` | modify | Append `TradingDecisionStrategyEvent` model + two `enum.StrEnum` classes (`StrategyEventSource`, `StrategyEventType`). |
| `alembic/versions/<new_rev>_add_trading_decision_strategy_events.py` | create | Create `trading_decision_strategy_events` table; `down_revision = 'd3703007a676'`. |
| `app/schemas/strategy_events.py` | create | Pydantic v2 `StrategyEventCreateRequest`, `StrategyEventDetail`, `StrategyEventListResponse`, literal types. |
| `app/services/strategy_event_service.py` | create | Async DB-only service: `create_strategy_event`, `get_strategy_event_by_uuid`, `list_strategy_events`. |
| `app/routers/strategy_events.py` | create | FastAPI router mounted at `/trading/api/strategy-events`; `POST /`, `GET /`, `GET /{event_uuid}`. |
| `app/main.py` | modify | `app.include_router(strategy_events.router)` next to `preopen.router`. |
| `tests/test_strategy_events_import_safety.py` | create | Forbidden-import safety test (mirrors ROB-26 pattern). |
| `tests/services/test_strategy_event_service.py` | create | Unit tests for the service (mock AsyncSession). |
| `tests/routers/test_strategy_events_router.py` | create | Router unit tests: auth, validation, payload round-trip (TestClient + dependency_overrides). |
| `tests/integration/test_strategy_event_db_roundtrip.py` | create | Optional `@pytest.mark.integration` round-trip against real DB session. |

UI/timeline frontend changes are explicitly **deferred** (spec marks them optional). This plan does not modify `frontend/trading-decision/`.

---

## Data Model — Final Field List

Table: `trading_decision_strategy_events` (append-only — no `updated_at`).

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | `BigInteger` | NO | PK, autoincrement. |
| `event_uuid` | `UUID` | NO | unique, indexed, default `uuid4()`. External identifier. |
| `session_id` | `BigInteger` | YES | FK `trading_decision_sessions.id` `ON DELETE SET NULL`, index. Nullable per spec — events may be standalone. |
| `source` | `Text` (CHECK) | NO | One of `user`, `hermes`, `tradingagents`, `news`, `market_data`, `scheduler`. CHECK constraint by name. |
| `event_type` | `Text` (CHECK) | NO | One of `operator_market_event`, `earnings_event`, `macro_event`, `sector_rotation`, `technical_break`, `risk_veto`, `cash_budget_change`, `position_change`. CHECK constraint by name. |
| `source_text` | `Text` | NO | Raw operator-supplied input. Length validated at API layer (max 8000 chars). |
| `normalized_summary` | `Text` | YES | Trimmed/normalized human summary; `<=2000` chars. |
| `affected_markets` | `JSONB` | NO | List of strings, default `[]`. |
| `affected_sectors` | `JSONB` | NO | List of strings, default `[]`. |
| `affected_themes` | `JSONB` | NO | List of strings, default `[]`. |
| `affected_symbols` | `JSONB` | NO | List of strings, default `[]`. |
| `severity` | `SmallInteger` (CHECK 1..5) | NO | 1=info, 2=low, 3=medium, 4=high, 5=critical. |
| `confidence` | `SmallInteger` (CHECK 0..100) | NO | Reuses operator-candidate scale. |
| `created_by_user_id` | `BigInteger` | YES | FK `users.id` `ON DELETE SET NULL`. |
| `created_at` | `TIMESTAMP(tz)` | NO | server_default `now()`. |
| `event_metadata` | `JSONB` | YES | Free-form. **Avoid the column name `metadata`** — it conflicts with SQLAlchemy `DeclarativeBase.metadata`. Pydantic field stays `metadata`; SQLAlchemy attribute uses an aliased mapping. |

Indexes:
- Unique on `event_uuid`.
- Btree on `session_id` (nullable, partial: `WHERE session_id IS NOT NULL`).
- Btree composite on `(created_by_user_id, created_at DESC)` for per-user timeline queries.
- Btree on `created_at DESC` for global timeline.

---

## Task 0 — Pre-flight

**Files:** none.

- [ ] **Step 1: Confirm worktree and branch**

```bash
cd /Users/mgh3326/work/auto_trader-worktrees/feature-ROB-41-operator-market-events
git status
git rev-parse --abbrev-ref HEAD
```
Expected: clean worktree, `feature/ROB-41-operator-market-events`.

- [ ] **Step 2: Confirm Alembic head is `d3703007a676`**

```bash
uv run alembic heads
```
Expected output ends with `d3703007a676 (head)`. If not, stop and ask the user.

- [ ] **Step 3: Confirm services running (Postgres + Redis) for integration tests**

```bash
docker compose ps
```
Expected: `postgres` and `redis` services up. Run `docker compose up -d` if not.

---

## Task 1 — Forbidden-import safety test (write FIRST, will fail until modules exist)

**Files:**
- Create: `tests/test_strategy_events_import_safety.py`

- [ ] **Step 1: Write the failing safety test**

```python
# tests/test_strategy_events_import_safety.py
"""ROB-41 forbidden-import safety test (mirrors ROB-26 pattern)."""

import importlib

import pytest

FORBIDDEN_PREFIXES = (
    "prefect",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.order_service",
    "app.services.orders",
    "app.services.paper_trading_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.crypto_trade_cooldown_service",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit_websocket",
    "app.services.upbit_market_websocket",
    "app.services.watch_alerts",
    "app.services.screener_service",
    "app.services.tradingagents_research_service",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_history",
    "app.mcp_server.tooling.paper_order_handler",
    "app.mcp_server.tooling.watch_alerts_registration",
)

MODULES_UNDER_TEST = (
    "app.services.strategy_event_service",
    "app.routers.strategy_events",
    "app.schemas.strategy_events",
)


@pytest.mark.parametrize("module_name", MODULES_UNDER_TEST)
def test_module_does_not_import_forbidden(module_name: str) -> None:
    module = importlib.import_module(module_name)
    src = open(module.__file__).read()
    for forbidden in FORBIDDEN_PREFIXES:
        assert f"import {forbidden}" not in src, f"{module_name} imports {forbidden}"
        assert f"from {forbidden}" not in src, f"{module_name} imports from {forbidden}"
```

- [ ] **Step 2: Run it (will fail — module not yet created)**

```bash
uv run pytest tests/test_strategy_events_import_safety.py -v
```
Expected: `ModuleNotFoundError` for `app.services.strategy_event_service`. This is the failing-state we want before Task 2.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_strategy_events_import_safety.py
git commit -m "test(ROB-41): forbidden-import safety test for strategy events (failing)"
```

---

## Task 2 — SQLAlchemy model

**Files:**
- Modify: `app/models/trading_decision.py` (append after `TradingDecisionOutcome`)

- [ ] **Step 1: Add enums + model**

Append to `app/models/trading_decision.py` (after the last existing class, before EOF):

```python
class StrategyEventSource(enum.StrEnum):
    user = "user"
    hermes = "hermes"
    tradingagents = "tradingagents"
    news = "news"
    market_data = "market_data"
    scheduler = "scheduler"


class StrategyEventType(enum.StrEnum):
    operator_market_event = "operator_market_event"
    earnings_event = "earnings_event"
    macro_event = "macro_event"
    sector_rotation = "sector_rotation"
    technical_break = "technical_break"
    risk_veto = "risk_veto"
    cash_budget_change = "cash_budget_change"
    position_change = "position_change"


class TradingDecisionStrategyEvent(Base):
    __tablename__ = "trading_decision_strategy_events"
    __table_args__ = (
        CheckConstraint(
            "source IN ('user','hermes','tradingagents','news','market_data','scheduler')",
            name="trading_decision_strategy_events_source_allowed",
        ),
        CheckConstraint(
            "event_type IN ('operator_market_event','earnings_event','macro_event',"
            "'sector_rotation','technical_break','risk_veto',"
            "'cash_budget_change','position_change')",
            name="trading_decision_strategy_events_type_allowed",
        ),
        CheckConstraint(
            "severity BETWEEN 1 AND 5",
            name="trading_decision_strategy_events_severity_range",
        ),
        CheckConstraint(
            "confidence BETWEEN 0 AND 100",
            name="trading_decision_strategy_events_confidence_range",
        ),
        Index(
            "ix_trading_decision_strategy_events_session_id_partial",
            "session_id",
            postgresql_where="(session_id IS NOT NULL)",
        ),
        Index(
            "ix_trading_decision_strategy_events_user_created_at",
            "created_by_user_id",
            "created_at",
            postgresql_using="btree",
            postgresql_ops={"created_at": "DESC"},
        ),
        Index(
            "ix_trading_decision_strategy_events_created_at",
            "created_at",
            postgresql_using="btree",
            postgresql_ops={"created_at": "DESC"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, index=True, default=uuid4, nullable=False
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading_decision_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_summary: Mapped[str | None] = mapped_column(Text)
    affected_markets: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    affected_sectors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    affected_themes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    affected_symbols: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    severity: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    confidence: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=50)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_metadata: Mapped[dict | None] = mapped_column("event_metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

Also update the import line at the top of the file from:
```python
from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    Text,
    func,
)
```
to add `SmallInteger`:
```python
from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    Text,
    func,
)
```

- [ ] **Step 2: Verify model imports cleanly (no DB roundtrip yet)**

```bash
uv run python -c "from app.models.trading_decision import TradingDecisionStrategyEvent, StrategyEventSource, StrategyEventType; print(TradingDecisionStrategyEvent.__tablename__)"
```
Expected: `trading_decision_strategy_events`.

- [ ] **Step 3: Commit**

```bash
git add app/models/trading_decision.py
git commit -m "feat(ROB-41): add TradingDecisionStrategyEvent model + enums"
```

---

## Task 3 — Alembic migration

**Files:**
- Create: `alembic/versions/<auto>_add_trading_decision_strategy_events.py`

- [ ] **Step 1: Autogenerate migration**

```bash
uv run alembic revision --autogenerate -m "add trading_decision_strategy_events"
```
Output: a new file under `alembic/versions/`. Note its revision id.

- [ ] **Step 2: Hand-edit the generated file to enforce expected shape**

Replace the body of `upgrade()` / `downgrade()` so it matches the canonical form below (the autogen output may attempt to drop unrelated columns, change indexes, etc. — strip everything except this table). Set `down_revision = 'd3703007a676'`.

```python
"""add trading_decision_strategy_events

Revision ID: <generated>
Revises: d3703007a676
Create Date: <generated>
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "<generated>"
down_revision: str | Sequence[str] | None = "d3703007a676"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trading_decision_strategy_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("event_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("normalized_summary", sa.Text(), nullable=True),
        sa.Column(
            "affected_markets",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "affected_sectors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "affected_themes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "affected_symbols",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("severity", sa.SmallInteger(), nullable=False, server_default=sa.text("2")),
        sa.Column("confidence", sa.SmallInteger(), nullable=False, server_default=sa.text("50")),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("event_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('user','hermes','tradingagents','news','market_data','scheduler')",
            name="trading_decision_strategy_events_source_allowed",
        ),
        sa.CheckConstraint(
            "event_type IN ('operator_market_event','earnings_event','macro_event',"
            "'sector_rotation','technical_break','risk_veto',"
            "'cash_budget_change','position_change')",
            name="trading_decision_strategy_events_type_allowed",
        ),
        sa.CheckConstraint(
            "severity BETWEEN 1 AND 5",
            name="trading_decision_strategy_events_severity_range",
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 100",
            name="trading_decision_strategy_events_confidence_range",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_uuid"),
    )
    op.create_foreign_key(
        None,
        "trading_decision_strategy_events",
        "trading_decision_sessions",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        None,
        "trading_decision_strategy_events",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_trading_decision_strategy_events_event_uuid"),
        "trading_decision_strategy_events",
        ["event_uuid"],
        unique=True,
    )
    op.create_index(
        op.f("ix_trading_decision_strategy_events_created_by_user_id"),
        "trading_decision_strategy_events",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_trading_decision_strategy_events_session_id_partial",
        "trading_decision_strategy_events",
        ["session_id"],
        unique=False,
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )
    op.create_index(
        "ix_trading_decision_strategy_events_user_created_at",
        "trading_decision_strategy_events",
        ["created_by_user_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_trading_decision_strategy_events_created_at",
        "trading_decision_strategy_events",
        [sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trading_decision_strategy_events_created_at",
        table_name="trading_decision_strategy_events",
    )
    op.drop_index(
        "ix_trading_decision_strategy_events_user_created_at",
        table_name="trading_decision_strategy_events",
    )
    op.drop_index(
        "ix_trading_decision_strategy_events_session_id_partial",
        table_name="trading_decision_strategy_events",
    )
    op.drop_index(
        op.f("ix_trading_decision_strategy_events_created_by_user_id"),
        table_name="trading_decision_strategy_events",
    )
    op.drop_index(
        op.f("ix_trading_decision_strategy_events_event_uuid"),
        table_name="trading_decision_strategy_events",
    )
    op.drop_table("trading_decision_strategy_events")
```

- [ ] **Step 3: Apply, verify, downgrade, re-apply**

```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic downgrade -1
uv run alembic upgrade head
```
Expected: `current` shows the new revision after each upgrade; downgrade succeeds without error.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/*_add_trading_decision_strategy_events.py
git commit -m "feat(ROB-41): migration for trading_decision_strategy_events"
```

---

## Task 4 — Pydantic schemas

**Files:**
- Create: `app/schemas/strategy_events.py`

- [ ] **Step 1: Write schemas**

```python
# app/schemas/strategy_events.py
"""ROB-41 strategy event request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

StrategyEventSourceLiteral = Literal[
    "user", "hermes", "tradingagents", "news", "market_data", "scheduler"
]
StrategyEventTypeLiteral = Literal[
    "operator_market_event",
    "earnings_event",
    "macro_event",
    "sector_rotation",
    "technical_break",
    "risk_veto",
    "cash_budget_change",
    "position_change",
]


def _strip_short(items: list[str], *, max_len: int) -> list[str]:
    cleaned: list[str] = []
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError("list entries must be strings")
        v = raw.strip()
        if not v:
            continue
        if len(v) > max_len:
            raise ValueError(f"entry exceeds {max_len} chars")
        cleaned.append(v)
    return cleaned


class StrategyEventCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: StrategyEventSourceLiteral = "user"
    event_type: StrategyEventTypeLiteral
    source_text: str = Field(min_length=1, max_length=8000)
    normalized_summary: str | None = Field(default=None, max_length=2000)
    session_uuid: UUID | None = None
    affected_markets: list[str] = Field(default_factory=list, max_length=32)
    affected_sectors: list[str] = Field(default_factory=list, max_length=32)
    affected_themes: list[str] = Field(default_factory=list, max_length=32)
    affected_symbols: list[str] = Field(default_factory=list, max_length=64)
    severity: int = Field(default=2, ge=1, le=5)
    confidence: int = Field(default=50, ge=0, le=100)
    metadata: dict | None = None  # stored in DB column `event_metadata`

    @field_validator("affected_markets", "affected_sectors", "affected_themes")
    @classmethod
    def _short_list(cls, v: list[str]) -> list[str]:
        return _strip_short(v, max_len=64)

    @field_validator("affected_symbols")
    @classmethod
    def _symbol_list(cls, v: list[str]) -> list[str]:
        return _strip_short(v, max_len=32)


class StrategyEventDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    event_uuid: UUID
    session_uuid: UUID | None
    source: StrategyEventSourceLiteral
    event_type: StrategyEventTypeLiteral
    source_text: str
    normalized_summary: str | None
    affected_markets: list[str]
    affected_sectors: list[str]
    affected_themes: list[str]
    affected_symbols: list[str]
    severity: int
    confidence: int
    created_by_user_id: int | None
    metadata: dict | None
    created_at: datetime


class StrategyEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[StrategyEventDetail]
    total: int
    limit: int
    offset: int
```

- [ ] **Step 2: Sanity import**

```bash
uv run python -c "from app.schemas.strategy_events import StrategyEventCreateRequest, StrategyEventDetail, StrategyEventListResponse; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add app/schemas/strategy_events.py
git commit -m "feat(ROB-41): add strategy event Pydantic schemas"
```

---

## Task 5 — Service: `create_strategy_event` (TDD)

**Files:**
- Create: `tests/services/test_strategy_event_service.py`
- Create: `app/services/strategy_event_service.py`

- [ ] **Step 1: Write the failing service unit test (themes/symbols round-trip + session linkage)**

```python
# tests/services/test_strategy_event_service.py
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest


@pytest.mark.unit
async def test_create_strategy_event_persists_and_returns_detail(monkeypatch):
    from app.schemas.strategy_events import StrategyEventCreateRequest
    from app.services import strategy_event_service

    db = SimpleNamespace()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock()

    # No session linkage path (session_uuid is None)
    req = StrategyEventCreateRequest(
        event_type="operator_market_event",
        source_text="Fed surprise hike 25bps",
        affected_markets=["us"],
        affected_themes=["rates", "macro"],
        affected_symbols=["AAPL", "QQQ"],
        severity=4,
        confidence=80,
        metadata={"note": "wire"},
    )

    detail = await strategy_event_service.create_strategy_event(
        db,
        request=req,
        user_id=7,
    )

    assert db.add.call_count == 1
    added = db.add.call_args.args[0]
    assert added.source_text == "Fed surprise hike 25bps"
    assert added.affected_themes == ["rates", "macro"]
    assert added.affected_symbols == ["AAPL", "QQQ"]
    assert added.session_id is None
    assert added.created_by_user_id == 7
    assert added.event_metadata == {"note": "wire"}

    # detail mirrors request fields, includes uuid + None session_uuid
    assert detail.session_uuid is None
    assert detail.affected_themes == ["rates", "macro"]
    assert detail.event_type == "operator_market_event"


@pytest.mark.unit
async def test_create_strategy_event_links_session_by_uuid(monkeypatch):
    from app.schemas.strategy_events import StrategyEventCreateRequest
    from app.services import strategy_event_service

    target_uuid = uuid4()

    db = SimpleNamespace()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()

    # `_resolve_session_id_for_uuid` should hit db.execute and unwrap scalar.
    scalar_one = MagicMock(return_value=42)
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: 42)
    )

    req = StrategyEventCreateRequest(
        event_type="risk_veto",
        source_text="halt new buys due to gap risk",
        session_uuid=target_uuid,
    )
    detail = await strategy_event_service.create_strategy_event(
        db, request=req, user_id=7
    )
    assert db.execute.await_count == 1
    added = db.add.call_args.args[0]
    assert added.session_id == 42
    assert detail.session_uuid == target_uuid


@pytest.mark.unit
async def test_create_strategy_event_unknown_session_uuid_raises(monkeypatch):
    from app.schemas.strategy_events import StrategyEventCreateRequest
    from app.services import strategy_event_service

    db = SimpleNamespace()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )

    req = StrategyEventCreateRequest(
        event_type="operator_market_event",
        source_text="x",
        session_uuid=uuid4(),
    )
    with pytest.raises(strategy_event_service.UnknownSessionUUIDError):
        await strategy_event_service.create_strategy_event(
            db, request=req, user_id=7
        )
    db.add.assert_not_called()
```

- [ ] **Step 2: Run — should fail (module not yet present)**

```bash
uv run pytest tests/services/test_strategy_event_service.py -v
```
Expected: ImportError on `app.services.strategy_event_service`.

- [ ] **Step 3: Implement the service**

```python
# app/services/strategy_event_service.py
"""ROB-41 strategy event service.

DB-only. NO broker / order / watch / paper / live execution imports.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading_decision import (
    TradingDecisionSession,
    TradingDecisionStrategyEvent,
)
from app.schemas.strategy_events import (
    StrategyEventCreateRequest,
    StrategyEventDetail,
    StrategyEventListResponse,
)


class UnknownSessionUUIDError(LookupError):
    """Raised when session_uuid is provided but no session matches."""


async def _resolve_session_id(
    db: AsyncSession, *, session_uuid: UUID | None
) -> int | None:
    if session_uuid is None:
        return None
    stmt = select(TradingDecisionSession.id).where(
        TradingDecisionSession.session_uuid == session_uuid
    )
    result = await db.execute(stmt)
    session_id = result.scalar_one_or_none()
    if session_id is None:
        raise UnknownSessionUUIDError(str(session_uuid))
    return session_id


def _to_detail(
    row: TradingDecisionStrategyEvent, *, session_uuid: UUID | None
) -> StrategyEventDetail:
    return StrategyEventDetail(
        id=row.id,
        event_uuid=row.event_uuid,
        session_uuid=session_uuid,
        source=row.source,
        event_type=row.event_type,
        source_text=row.source_text,
        normalized_summary=row.normalized_summary,
        affected_markets=list(row.affected_markets or []),
        affected_sectors=list(row.affected_sectors or []),
        affected_themes=list(row.affected_themes or []),
        affected_symbols=list(row.affected_symbols or []),
        severity=row.severity,
        confidence=row.confidence,
        created_by_user_id=row.created_by_user_id,
        metadata=row.event_metadata,
        created_at=row.created_at,
    )


async def create_strategy_event(
    db: AsyncSession,
    *,
    request: StrategyEventCreateRequest,
    user_id: int,
) -> StrategyEventDetail:
    session_id = await _resolve_session_id(db, session_uuid=request.session_uuid)

    row = TradingDecisionStrategyEvent(
        session_id=session_id,
        source=request.source,
        event_type=request.event_type,
        source_text=request.source_text,
        normalized_summary=request.normalized_summary,
        affected_markets=request.affected_markets,
        affected_sectors=request.affected_sectors,
        affected_themes=request.affected_themes,
        affected_symbols=request.affected_symbols,
        severity=request.severity,
        confidence=request.confidence,
        created_by_user_id=user_id,
        event_metadata=request.metadata,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _to_detail(row, session_uuid=request.session_uuid)


async def get_strategy_event_by_uuid(
    db: AsyncSession, *, event_uuid: UUID
) -> StrategyEventDetail | None:
    stmt = (
        select(TradingDecisionStrategyEvent, TradingDecisionSession.session_uuid)
        .outerjoin(
            TradingDecisionSession,
            TradingDecisionStrategyEvent.session_id == TradingDecisionSession.id,
        )
        .where(TradingDecisionStrategyEvent.event_uuid == event_uuid)
    )
    result = await db.execute(stmt)
    pair = result.first()
    if pair is None:
        return None
    row, sess_uuid = pair
    return _to_detail(row, session_uuid=sess_uuid)


async def list_strategy_events(
    db: AsyncSession,
    *,
    session_uuid: UUID | None = None,
    user_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> StrategyEventListResponse:
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    base = select(
        TradingDecisionStrategyEvent, TradingDecisionSession.session_uuid
    ).outerjoin(
        TradingDecisionSession,
        TradingDecisionStrategyEvent.session_id == TradingDecisionSession.id,
    )
    count_base = select(func.count(TradingDecisionStrategyEvent.id))

    if session_uuid is not None:
        sess_id = await _resolve_session_id(db, session_uuid=session_uuid)
        base = base.where(TradingDecisionStrategyEvent.session_id == sess_id)
        count_base = count_base.where(
            TradingDecisionStrategyEvent.session_id == sess_id
        )
    if user_id is not None:
        base = base.where(TradingDecisionStrategyEvent.created_by_user_id == user_id)
        count_base = count_base.where(
            TradingDecisionStrategyEvent.created_by_user_id == user_id
        )

    base = base.order_by(TradingDecisionStrategyEvent.created_at.desc()).limit(
        limit
    ).offset(offset)

    total = (await db.execute(count_base)).scalar_one()
    rows = (await db.execute(base)).all()
    events = [_to_detail(row, session_uuid=sess_uuid) for row, sess_uuid in rows]
    return StrategyEventListResponse(
        events=events, total=total, limit=limit, offset=offset
    )
```

- [ ] **Step 4: Run service tests — expect PASS**

```bash
uv run pytest tests/services/test_strategy_event_service.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Run forbidden-import test — expect PASS now that the module exists and imports nothing forbidden**

```bash
uv run pytest tests/test_strategy_events_import_safety.py -v
```
Expected: parametrized test passes for the service module (the schema module passes trivially; router still missing — that param will fail with ImportError, which is acceptable until Task 7. If the test fixture ImportError fails the param entirely, that's fine — proceed and re-run after Task 7).

- [ ] **Step 6: Commit**

```bash
git add app/services/strategy_event_service.py tests/services/test_strategy_event_service.py
git commit -m "feat(ROB-41): strategy event service (DB-only, append-only)"
```

---

## Task 6 — Schema validation tests (no DB / no router)

**Files:**
- Create: `tests/services/test_strategy_event_schema_validation.py`

- [ ] **Step 1: Write tests for malformed payloads**

```python
# tests/services/test_strategy_event_schema_validation.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.strategy_events import StrategyEventCreateRequest


@pytest.mark.unit
def test_missing_event_type_rejected():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(source_text="x")  # type: ignore[arg-type]


@pytest.mark.unit
def test_unknown_event_type_rejected():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(
            event_type="not_a_type",  # type: ignore[arg-type]
            source_text="x",
        )


@pytest.mark.unit
def test_source_text_max_length_enforced():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(
            event_type="operator_market_event",
            source_text="x" * 8001,
        )


@pytest.mark.unit
def test_severity_range_enforced():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(
            event_type="operator_market_event",
            source_text="x",
            severity=6,
        )


@pytest.mark.unit
def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(
            event_type="operator_market_event",
            source_text="x",
            place_order=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_affected_symbols_round_trip():
    req = StrategyEventCreateRequest(
        event_type="operator_market_event",
        source_text="x",
        affected_symbols=["  AAPL  ", "MSFT"],
        affected_themes=["AI", " growth "],
    )
    assert req.affected_symbols == ["AAPL", "MSFT"]
    assert req.affected_themes == ["AI", "growth"]
```

- [ ] **Step 2: Run — expect PASS**

```bash
uv run pytest tests/services/test_strategy_event_schema_validation.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_strategy_event_schema_validation.py
git commit -m "test(ROB-41): schema validation for strategy events"
```

---

## Task 7 — Router (TDD: auth, validation, round-trip)

**Files:**
- Create: `tests/routers/test_strategy_events_router.py`
- Create: `app/routers/strategy_events.py`
- Modify: `app/main.py` (one line: `app.include_router(strategy_events.router)`)

- [ ] **Step 1: Write router unit tests (will fail — router not yet present)**

```python
# tests/routers/test_strategy_events_router.py
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _make_client():
    from app.routers import strategy_events
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(strategy_events.router)
    fake_user = SimpleNamespace(id=7)
    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    return TestClient(app), app


class _FakeDB:
    def __init__(self) -> None:
        self.commit = AsyncMock()


def _detail_stub(*, session_uuid=None):
    from app.schemas.strategy_events import StrategyEventDetail

    return StrategyEventDetail(
        id=1,
        event_uuid=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        session_uuid=session_uuid,
        source="user",
        event_type="operator_market_event",
        source_text="Fed hike",
        normalized_summary=None,
        affected_markets=["us"],
        affected_sectors=[],
        affected_themes=["macro"],
        affected_symbols=["AAPL"],
        severity=4,
        confidence=80,
        created_by_user_id=7,
        metadata=None,
        created_at=datetime(2026, 4, 29, tzinfo=UTC),
    )


@pytest.mark.unit
def test_unauthenticated_post_returns_401():
    from app.core.db import get_db
    from app.routers import strategy_events
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(strategy_events.router)
    app.dependency_overrides[get_authenticated_user] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="auth required")
    )
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    client = TestClient(app)
    resp = client.post(
        "/trading/api/strategy-events",
        json={"event_type": "operator_market_event", "source_text": "x"},
    )
    assert resp.status_code == 401


@pytest.mark.unit
def test_authenticated_post_returns_201_and_round_trips_lists(monkeypatch):
    from app.core.db import get_db
    from app.services import strategy_event_service

    create_mock = AsyncMock(return_value=_detail_stub())
    monkeypatch.setattr(
        strategy_event_service, "create_strategy_event", create_mock
    )

    fake_db = _FakeDB()
    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: fake_db

    payload = {
        "event_type": "operator_market_event",
        "source_text": "Fed hike",
        "affected_markets": ["us"],
        "affected_themes": ["macro"],
        "affected_symbols": ["AAPL"],
        "severity": 4,
        "confidence": 80,
    }
    resp = client.post("/trading/api/strategy-events", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["affected_markets"] == ["us"]
    assert body["affected_themes"] == ["macro"]
    assert body["affected_symbols"] == ["AAPL"]
    assert body["session_uuid"] is None
    assert body["event_uuid"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fake_db.commit.assert_awaited_once()
    assert create_mock.await_args.kwargs["user_id"] == 7


@pytest.mark.unit
def test_authenticated_post_links_session_uuid(monkeypatch):
    from app.core.db import get_db
    from app.services import strategy_event_service

    sess_uuid = uuid4()
    create_mock = AsyncMock(return_value=_detail_stub(session_uuid=sess_uuid))
    monkeypatch.setattr(
        strategy_event_service, "create_strategy_event", create_mock
    )

    fake_db = _FakeDB()
    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: fake_db

    resp = client.post(
        "/trading/api/strategy-events",
        json={
            "event_type": "operator_market_event",
            "source_text": "linked",
            "session_uuid": str(sess_uuid),
        },
    )
    assert resp.status_code == 201
    assert resp.json()["session_uuid"] == str(sess_uuid)
    assert create_mock.await_args.kwargs["request"].session_uuid == sess_uuid


@pytest.mark.unit
def test_unknown_session_uuid_returns_404(monkeypatch):
    from app.core.db import get_db
    from app.services import strategy_event_service

    create_mock = AsyncMock(
        side_effect=strategy_event_service.UnknownSessionUUIDError("x")
    )
    monkeypatch.setattr(
        strategy_event_service, "create_strategy_event", create_mock
    )

    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: _FakeDB()
    resp = client.post(
        "/trading/api/strategy-events",
        json={
            "event_type": "operator_market_event",
            "source_text": "x",
            "session_uuid": str(uuid4()),
        },
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "session_uuid_not_found"


@pytest.mark.unit
def test_extra_fields_rejected_with_422():
    from app.core.db import get_db

    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    resp = client.post(
        "/trading/api/strategy-events",
        json={
            "event_type": "operator_market_event",
            "source_text": "x",
            "place_order": True,
        },
    )
    assert resp.status_code == 422


@pytest.mark.unit
def test_too_long_source_text_returns_422():
    from app.core.db import get_db

    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    resp = client.post(
        "/trading/api/strategy-events",
        json={"event_type": "operator_market_event", "source_text": "x" * 8001},
    )
    assert resp.status_code == 422


@pytest.mark.unit
def test_list_endpoint_filters_by_session_uuid(monkeypatch):
    from app.core.db import get_db
    from app.schemas.strategy_events import StrategyEventListResponse
    from app.services import strategy_event_service

    list_mock = AsyncMock(
        return_value=StrategyEventListResponse(
            events=[_detail_stub()], total=1, limit=50, offset=0
        )
    )
    monkeypatch.setattr(strategy_event_service, "list_strategy_events", list_mock)

    sess_uuid = uuid4()
    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    resp = client.get(
        f"/trading/api/strategy-events?session_uuid={sess_uuid}&limit=10"
    )
    assert resp.status_code == 200
    assert list_mock.await_args.kwargs["session_uuid"] == sess_uuid
    assert list_mock.await_args.kwargs["limit"] == 10


@pytest.mark.unit
def test_get_by_uuid_returns_404_when_absent(monkeypatch):
    from app.core.db import get_db
    from app.services import strategy_event_service

    monkeypatch.setattr(
        strategy_event_service,
        "get_strategy_event_by_uuid",
        AsyncMock(return_value=None),
    )

    client, app = _make_client()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    resp = client.get(f"/trading/api/strategy-events/{uuid4()}")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run — expect ImportError (router not yet present)**

```bash
uv run pytest tests/routers/test_strategy_events_router.py -v
```

- [ ] **Step 3: Implement the router**

```python
# app/routers/strategy_events.py
"""ROB-41 strategy events API.

Read/write metadata only. NO broker / order / watch / paper / live execution
imports. NO mutation of trading_decision_proposals or actions.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.strategy_events import (
    StrategyEventCreateRequest,
    StrategyEventDetail,
    StrategyEventListResponse,
)
from app.services import strategy_event_service

router = APIRouter(prefix="/trading", tags=["strategy-events"])


@router.post(
    "/api/strategy-events",
    response_model=StrategyEventDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_strategy_event(
    request: StrategyEventCreateRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> StrategyEventDetail:
    try:
        detail = await strategy_event_service.create_strategy_event(
            db, request=request, user_id=current_user.id
        )
    except strategy_event_service.UnknownSessionUUIDError:
        raise HTTPException(status_code=404, detail="session_uuid_not_found")
    await db.commit()
    response.headers["Location"] = f"/trading/api/strategy-events/{detail.event_uuid}"
    return detail


@router.get(
    "/api/strategy-events", response_model=StrategyEventListResponse
)
async def list_strategy_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    session_uuid: UUID | None = Query(default=None),
    mine: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> StrategyEventListResponse:
    try:
        return await strategy_event_service.list_strategy_events(
            db,
            session_uuid=session_uuid,
            user_id=current_user.id if mine else None,
            limit=limit,
            offset=offset,
        )
    except strategy_event_service.UnknownSessionUUIDError:
        raise HTTPException(status_code=404, detail="session_uuid_not_found")


@router.get(
    "/api/strategy-events/{event_uuid}", response_model=StrategyEventDetail
)
async def get_strategy_event(
    event_uuid: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> StrategyEventDetail:
    detail = await strategy_event_service.get_strategy_event_by_uuid(
        db, event_uuid=event_uuid
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="strategy_event_not_found")
    return detail
```

- [ ] **Step 4: Register router in `app/main.py`**

In `app/main.py`, add `strategy_events` to the routers import block (currently lines 37–44 ish) and add `app.include_router(strategy_events.router)` immediately after the `app.include_router(preopen.router)` line (~line 170).

```python
# top of file — extend existing routers import
from app.routers import (
    ...,
    preopen,
    strategy_events,
    ...,
)

# in factory, after `app.include_router(preopen.router)`:
app.include_router(strategy_events.router)
```

- [ ] **Step 5: Run router tests — expect PASS**

```bash
uv run pytest tests/routers/test_strategy_events_router.py -v
```

- [ ] **Step 6: Re-run forbidden-import test — expect PASS for all 3 modules**

```bash
uv run pytest tests/test_strategy_events_import_safety.py -v
```

- [ ] **Step 7: Commit**

```bash
git add app/routers/strategy_events.py app/main.py tests/routers/test_strategy_events_router.py
git commit -m "feat(ROB-41): strategy events router (POST/GET, auth-required)"
```

---

## Task 8 — DB round-trip integration test

**Files:**
- Create: `tests/integration/test_strategy_event_db_roundtrip.py`

This task requires Postgres up. If `tests/integration/` already has fixtures for an async DB session, reuse them. Otherwise mark `@pytest.mark.integration` and use the existing async session fixture (search `tests/integration/conftest.py` for it).

- [ ] **Step 1: Write integration test**

```python
# tests/integration/test_strategy_event_db_roundtrip.py
from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


async def test_strategy_event_round_trip(async_db_session, integration_user_id):
    """Themes/symbols/markets/sectors persist as structured JSON; FK linkage works."""
    from app.models.trading_decision import (
        SessionStatus,
        TradingDecisionSession,
    )
    from app.schemas.strategy_events import StrategyEventCreateRequest
    from app.services import strategy_event_service

    # 1) create a session row to link against
    session_row = TradingDecisionSession(
        session_uuid=uuid4(),
        user_id=integration_user_id,
        source_profile="rob41-itest",
        status=SessionStatus.open.value,
        generated_at=__import__("datetime").datetime.now(
            __import__("datetime").UTC
        ),
    )
    async_db_session.add(session_row)
    await async_db_session.flush()

    req = StrategyEventCreateRequest(
        event_type="operator_market_event",
        source_text="round trip",
        session_uuid=session_row.session_uuid,
        affected_markets=["kr", "us"],
        affected_sectors=["semis"],
        affected_themes=["ai", "rates"],
        affected_symbols=["005930", "AAPL"],
        severity=3,
        confidence=70,
        metadata={"x": 1},
    )
    detail = await strategy_event_service.create_strategy_event(
        async_db_session, request=req, user_id=integration_user_id
    )
    await async_db_session.commit()

    fetched = await strategy_event_service.get_strategy_event_by_uuid(
        async_db_session, event_uuid=detail.event_uuid
    )
    assert fetched is not None
    assert fetched.session_uuid == session_row.session_uuid
    assert fetched.affected_markets == ["kr", "us"]
    assert fetched.affected_themes == ["ai", "rates"]
    assert fetched.affected_symbols == ["005930", "AAPL"]
    assert fetched.metadata == {"x": 1}

    listing = await strategy_event_service.list_strategy_events(
        async_db_session, session_uuid=session_row.session_uuid
    )
    assert listing.total == 1
    assert listing.events[0].event_uuid == detail.event_uuid
```

> **Note for the implementer:** if `async_db_session` / `integration_user_id` fixtures don't already exist in `tests/integration/conftest.py`, search there for the canonical names and adapt. Do not invent new infra. If integration tests are infeasible in the local environment, mark this task `xfail` and document it in the PR description rather than fabricating fixtures.

- [ ] **Step 2: Run integration test (Postgres must be up)**

```bash
docker compose up -d postgres redis
uv run pytest tests/integration/test_strategy_event_db_roundtrip.py -v -m integration
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_strategy_event_db_roundtrip.py
git commit -m "test(ROB-41): strategy event DB round-trip integration test"
```

---

## Task 9 — Final verification & lint

- [ ] **Step 1: Run only the touched test surface**

```bash
uv run pytest \
  tests/test_strategy_events_import_safety.py \
  tests/services/test_strategy_event_service.py \
  tests/services/test_strategy_event_schema_validation.py \
  tests/routers/test_strategy_events_router.py \
  -v
```
Expected: all PASS.

- [ ] **Step 2: Run integration tests (requires services)**

```bash
docker compose up -d postgres redis
uv run pytest tests/integration/test_strategy_event_db_roundtrip.py -v -m integration
```

- [ ] **Step 3: Lint + format**

```bash
uv run ruff format app/models/trading_decision.py app/schemas/strategy_events.py app/services/strategy_event_service.py app/routers/strategy_events.py app/main.py tests/test_strategy_events_import_safety.py tests/services/test_strategy_event_service.py tests/services/test_strategy_event_schema_validation.py tests/routers/test_strategy_events_router.py tests/integration/test_strategy_event_db_roundtrip.py alembic/versions/*_add_trading_decision_strategy_events.py
uv run ruff check app/models/trading_decision.py app/schemas/strategy_events.py app/services/strategy_event_service.py app/routers/strategy_events.py app/main.py tests/
```
Expected: no diagnostics.

- [ ] **Step 4: Type-check**

```bash
make typecheck
```
Expected: clean (or no new diagnostics on the new files).

- [ ] **Step 5: Re-confirm Alembic head still applies cleanly**

```bash
uv run alembic downgrade -1
uv run alembic upgrade head
uv run alembic current
```

- [ ] **Step 6: Confirm trading_decisions existing tests still pass (regression guard)**

```bash
uv run pytest tests/routers/test_trading_decisions_operator_request.py -v
```
Expected: PASS — the new router does NOT touch any existing endpoint.

- [ ] **Step 7: Manual safety audit of the new files**

Open each new file and verify by inspection that NONE of the forbidden module names appear in any `import` or `from ... import` statement. The automated test in Task 1 catches this, but a human read is cheap insurance.

- [ ] **Step 8: Push and open PR**

```bash
git push -u origin feature/ROB-41-operator-market-events
gh pr create --base main --title "feat(ROB-41): operator-provided market events ledger" \
  --body "$(cat <<'EOF'
## Summary
- New append-only `trading_decision_strategy_events` table with optional FK to `trading_decision_sessions`.
- Authenticated `POST/GET /trading/api/strategy-events` router; service is DB-only.
- Forbidden-import safety test guarantees no broker/order/watch/paper/live-execution coupling.

## Out of scope
- No live/dry-run/paper orders, no watch alerts, no proposal mutation, no TradingAgents rerun, no automation.

## Test plan
- [ ] `uv run pytest tests/test_strategy_events_import_safety.py tests/services/test_strategy_event_service.py tests/services/test_strategy_event_schema_validation.py tests/routers/test_strategy_events_router.py -v`
- [ ] `uv run pytest tests/integration/test_strategy_event_db_roundtrip.py -v -m integration`
- [ ] `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
- [ ] `uv run ruff check` / `make typecheck`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes (for the implementer)

- **Spec coverage check:** id/uuid ✓ (Task 2), nullable session_uuid linkage ✓ (Task 5 + Task 7), source enum ✓, event_type enum ✓, source_text ✓, normalized_summary ✓, affected_* lists ✓, severity ✓, confidence ✓, created_by_user_id ✓, created_at ✓, metadata JSON ✓, POST endpoint ✓, GET timeline ✓, GET by uuid ✓, auth required ✓ (Task 7 step 1 + step 3 unauth case), unauth rejected ✓, explicit session_uuid linkage ✓, themes/symbols round-trip ✓ (Task 6 + Task 8), malformed/too-long → 422 ✓ (Task 6 + Task 7), forbidden-import test ✓ (Task 1).
- **Out-of-scope respected:** the service does not touch `TradingDecisionProposal` or `TradingDecisionAction`, never imports execution/order/watch modules, never calls `place_order` / `manage_watch_alerts`, and never triggers TradingAgents.
- **Append-only invariant:** no PATCH/PUT/DELETE endpoints in this slice; the model has no `updated_at`. Subsequent revisions of an event are out of scope (would be a separate Linear issue).
- **Linkage softness:** if a session is deleted, events stay with `session_id = NULL` (audit trail preserved) — this matches "evidence/revision input only".
- **`metadata` naming:** SQLAlchemy column is `event_metadata` to avoid clashing with `Base.metadata`; Pydantic field is `metadata`; the service translates between them. Keep this mapping consistent if extending later.
- **What's deliberately deferred:** UI/timeline frontend changes, automatic linkage to "matching proposals", revision-suggestion mutation, scheduler ingestion, news/macro ingestion adapters. Each is a follow-up issue under ROB-40.
