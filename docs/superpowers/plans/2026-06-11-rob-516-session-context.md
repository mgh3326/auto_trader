# ROB-516 Session Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an append-only operator session context store and expose MCP tools so a new trading session can restore recent deferred decisions, rejected candidates, constraints, open questions, and next actions in one call.

**Architecture:** Add `review.operator_session_context` as a narrow append-only table separate from `research_session_*`, `investment_reports`, journals, and watches. Keep persistence behind a small service, validate MCP inputs through Pydantic schemas, and register two public tools: `session_context_append` and `session_context_get_recent`.

**Tech Stack:** Python 3.13, SQLAlchemy async ORM, PostgreSQL JSONB/UUID, Alembic, Pydantic v2, FastMCP, pytest/pytest-asyncio, `uv`.

---

## File Structure

- Create: `app/models/session_context.py`
  - Defines the `OperatorSessionContext` ORM model for `review.operator_session_context`.
- Modify: `app/models/__init__.py`
  - Imports and exports `OperatorSessionContext` so `Base.metadata.create_all` sees the model in tests.
- Create: `alembic/versions/20260611_rob516_session_context.py`
  - Adds the production table, constraints, and indexes.
- Create: `app/schemas/session_context.py`
  - Defines request/response schemas and literals for entry types, refs, actor, market, and account scope.
- Create: `app/services/session_context.py`
  - Implements append and recent-query behavior without committing; callers own transactions.
- Create: `app/mcp_server/tooling/session_context_tools.py`
  - Implements `session_context_append` and `session_context_get_recent`.
- Create: `app/mcp_server/tooling/session_context_registration.py`
  - Registers the two MCP tools.
- Modify: `app/mcp_server/tooling/registry.py`
  - Calls `register_session_context_tools(mcp)` in the default read/write MCP surface.
- Modify: `app/mcp_server/tooling/__init__.py`
  - Adds lazy exports for `register_session_context_tools`, matching the existing package export pattern.
- Modify: `app/mcp_server/README.md`
  - Documents the new tools and clarifies that they are operator-context state, not investment reports or research sessions.
- Create: `tests/models/test_operator_session_context_model.py`
- Create: `tests/schemas/test_session_context_schemas.py`
- Create: `tests/services/test_session_context_service.py`
- Create: `tests/test_session_context_mcp.py`

## Product Decisions Locked In

- The table lives in the `review` schema because the linked report, watch, journal, and trade review surfaces already live there.
- `market` is required on every entry and limited to `kr`, `us`, and `crypto`.
- `account_scope` is optional and reuses the investment report vocabulary: `kis_live`, `kis_mock`, `alpaca_paper`, `upbit_live`.
- `refs` is a JSON object with first-class keys for `report_uuid`, `item_uuid`, `alert_uuid`, `order_id`, `journal_id`, and `symbols`.
- No update/delete MCP tool is added. Corrections are follow-up entries.
- No UI, `/invest/` timeline, `get_operating_briefing`, or active-watch listing is included in this plan.

---

### Task 1: Add ORM Model And Alembic Migration

**Files:**
- Create: `tests/models/test_operator_session_context_model.py`
- Create: `app/models/session_context.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/20260611_rob516_session_context.py`

- [ ] **Step 1: Write the failing model contract test**

Create `tests/models/test_operator_session_context_model.py`:

```python
from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.models.session_context import OperatorSessionContext


def test_operator_session_context_model_contract() -> None:
    assert OperatorSessionContext.__tablename__ == "operator_session_context"
    assert OperatorSessionContext.__table__.schema == "review"

    column_names = {column.name for column in OperatorSessionContext.__table__.columns}
    assert column_names == {
        "id",
        "entry_uuid",
        "kst_date",
        "market",
        "account_scope",
        "entry_type",
        "title",
        "body",
        "refs",
        "created_by",
        "session_label",
        "created_at",
    }

    constraints = OperatorSessionContext.__table__.constraints
    constraint_names = {constraint.name for constraint in constraints}
    assert "uq_operator_session_context_entry_uuid" in constraint_names
    assert "ck_operator_session_context_market" in constraint_names
    assert "ck_operator_session_context_account_scope" in constraint_names
    assert "ck_operator_session_context_entry_type" in constraint_names
    assert "ck_operator_session_context_created_by" in constraint_names
    assert "ck_operator_session_context_refs_object" in constraint_names

    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_operator_session_context_entry_uuid"
        for constraint in constraints
    )
    assert any(isinstance(constraint, CheckConstraint) for constraint in constraints)

    indexes = OperatorSessionContext.__table__.indexes
    index_names = {index.name for index in indexes}
    assert {
        "ix_operator_session_context_market_date_created",
        "ix_operator_session_context_entry_type_date",
        "ix_operator_session_context_refs_gin",
    }.issubset(index_names)
    assert any(
        isinstance(index, Index)
        and index.name == "ix_operator_session_context_refs_gin"
        and index.dialect_options["postgresql"]["using"] == "gin"
        for index in indexes
    )
```

- [ ] **Step 2: Run the model test and verify it fails**

Run:

```bash
uv run pytest tests/models/test_operator_session_context_model.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.session_context'`.

- [ ] **Step 3: Create the ORM model**

Create `app/models/session_context.py`:

```python
"""Operator session context persistence (ROB-516)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base


class OperatorSessionContext(Base):
    """Append-only operator handoff entries for trading-session continuity."""

    __tablename__ = "operator_session_context"
    __table_args__ = (
        UniqueConstraint(
            "entry_uuid",
            name="uq_operator_session_context_entry_uuid",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="market",
        ),
        CheckConstraint(
            "account_scope IS NULL OR account_scope IN "
            "('kis_live','kis_mock','alpaca_paper','upbit_live')",
            name="account_scope",
        ),
        CheckConstraint(
            "entry_type IN ("
            "'plan','decision','deferred','rejected_candidate','constraint',"
            "'open_question','next_action','handoff_note'"
            ")",
            name="entry_type",
        ),
        CheckConstraint(
            "created_by IN ('claude','operator','system')",
            name="created_by",
        ),
        CheckConstraint(
            "jsonb_typeof(refs) = 'object'",
            name="refs_object",
        ),
        Index(
            "ix_operator_session_context_market_date_created",
            "market",
            "kst_date",
            "created_at",
        ),
        Index(
            "ix_operator_session_context_entry_type_date",
            "entry_type",
            "kst_date",
        ),
        Index(
            "ix_operator_session_context_refs_gin",
            "refs",
            postgresql_using="gin",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    kst_date: Mapped[date] = mapped_column(Date, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text)
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    refs: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_by: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="claude",
        server_default=text("'claude'"),
    )
    session_label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
```

- [ ] **Step 4: Import the model in the central model package**

Modify `app/models/__init__.py`:

```python
from .session_context import OperatorSessionContext
```

Add `"OperatorSessionContext"` to `__all__`.

- [ ] **Step 5: Create the Alembic migration**

Create `alembic/versions/20260611_rob516_session_context.py`:

```python
"""ROB-516 operator session context append-only store

Revision ID: 20260611_rob516
Revises: 20260610_rob491
Create Date: 2026-06-11 13:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260611_rob516"
down_revision: str | None = "20260610_rob491"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_session_context",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "entry_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kst_date", sa.Date(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_by",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'claude'"),
        ),
        sa.Column("session_label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entry_uuid",
            name="uq_operator_session_context_entry_uuid",
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_operator_session_context_market",
        ),
        sa.CheckConstraint(
            "account_scope IS NULL OR account_scope IN "
            "('kis_live','kis_mock','alpaca_paper','upbit_live')",
            name="ck_operator_session_context_account_scope",
        ),
        sa.CheckConstraint(
            "entry_type IN ("
            "'plan','decision','deferred','rejected_candidate','constraint',"
            "'open_question','next_action','handoff_note'"
            ")",
            name="ck_operator_session_context_entry_type",
        ),
        sa.CheckConstraint(
            "created_by IN ('claude','operator','system')",
            name="ck_operator_session_context_created_by",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(refs) = 'object'",
            name="ck_operator_session_context_refs_object",
        ),
        schema="review",
    )
    op.create_index(
        "ix_operator_session_context_market_date_created",
        "operator_session_context",
        ["market", "kst_date", "created_at"],
        schema="review",
    )
    op.create_index(
        "ix_operator_session_context_entry_type_date",
        "operator_session_context",
        ["entry_type", "kst_date"],
        schema="review",
    )
    op.create_index(
        "ix_operator_session_context_refs_gin",
        "operator_session_context",
        ["refs"],
        unique=False,
        schema="review",
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operator_session_context_refs_gin",
        table_name="operator_session_context",
        schema="review",
    )
    op.drop_index(
        "ix_operator_session_context_entry_type_date",
        table_name="operator_session_context",
        schema="review",
    )
    op.drop_index(
        "ix_operator_session_context_market_date_created",
        table_name="operator_session_context",
        schema="review",
    )
    op.drop_table("operator_session_context", schema="review")
```

- [ ] **Step 6: Run the model test again**

Run:

```bash
uv run pytest tests/models/test_operator_session_context_model.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add app/models/session_context.py app/models/__init__.py alembic/versions/20260611_rob516_session_context.py tests/models/test_operator_session_context_model.py
git commit -m "feat(ROB-516): add operator session context model"
```

---

### Task 2: Add Pydantic Schemas

**Files:**
- Create: `tests/schemas/test_session_context_schemas.py`
- Create: `app/schemas/session_context.py`

- [ ] **Step 1: Write schema tests**

Create `tests/schemas/test_session_context_schemas.py`:

```python
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.session_context import (
    SessionContextAppendEntry,
    SessionContextRecentRequest,
    SessionContextRefs,
    SessionContextResponse,
)


def test_append_entry_accepts_refs_and_strips_title_body() -> None:
    entry = SessionContextAppendEntry.model_validate(
        {
            "kst_date": "2026-06-11",
            "market": "kr",
            "account_scope": "kis_live",
            "entry_type": "deferred",
            "title": "  DB 매도 보류  ",
            "body": "  익절 조건만 허용되어 매도 제외  ",
            "refs": {
                "report_uuid": "11111111-1111-1111-1111-111111111111",
                "item_uuid": "22222222-2222-2222-2222-222222222222",
                "alert_uuid": "33333333-3333-3333-3333-333333333333",
                "order_id": "KIS-1",
                "journal_id": 7,
                "symbols": ["  DB  ", "005930", ""],
            },
            "created_by": "claude",
            "session_label": "kr-2026-06-11-close",
        }
    )

    assert entry.kst_date == date(2026, 6, 11)
    assert entry.title == "DB 매도 보류"
    assert entry.body == "익절 조건만 허용되어 매도 제외"
    assert entry.refs.report_uuid == UUID("11111111-1111-1111-1111-111111111111")
    assert entry.refs.symbols == ["DB", "005930"]


def test_append_entry_rejects_unknown_type_and_extra_ref() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SessionContextAppendEntry.model_validate(
            {
                "market": "kr",
                "entry_type": "memo",
                "title": "x",
                "body": "y",
                "refs": {"unknown": "value"},
            }
        )

    rendered = str(exc_info.value)
    assert "entry_type" in rendered
    assert "unknown" in rendered


def test_recent_request_clamps_limit_and_parses_date() -> None:
    request = SessionContextRecentRequest.model_validate(
        {
            "market": "kr",
            "account_scope": "kis_mock",
            "entry_type": "next_action",
            "kst_date_from": "2026-06-10",
            "limit": 500,
        }
    )

    assert request.limit == 100
    assert request.kst_date_from == date(2026, 6, 10)


def test_response_serializes_refs_from_attributes() -> None:
    class Row:
        entry_uuid = UUID("44444444-4444-4444-4444-444444444444")
        kst_date = date(2026, 6, 11)
        market = "kr"
        account_scope = "kis_live"
        entry_type = "handoff_note"
        title = "handoff"
        body = "continue tournament"
        refs = {"symbols": ["005930"]}
        created_by = "operator"
        session_label = None
        created_at = datetime(2026, 6, 11, 1, 2, 3, tzinfo=timezone.utc)

    response = SessionContextResponse.model_validate(Row())

    assert response.refs == SessionContextRefs(symbols=["005930"])
    dumped = response.model_dump(mode="json")
    assert dumped["entry_uuid"] == "44444444-4444-4444-4444-444444444444"
    assert dumped["refs"]["symbols"] == ["005930"]
```

- [ ] **Step 2: Run schema tests and verify they fail**

Run:

```bash
uv run pytest tests/schemas/test_session_context_schemas.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas.session_context'`.

- [ ] **Step 3: Create session context schemas**

Create `app/schemas/session_context.py`:

```python
"""ROB-516 session context DTOs for MCP and service boundaries."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.investment_reports import AccountScopeLiteral, MarketLiteral

SessionContextEntryTypeLiteral = Literal[
    "plan",
    "decision",
    "deferred",
    "rejected_candidate",
    "constraint",
    "open_question",
    "next_action",
    "handoff_note",
]
SessionContextCreatedByLiteral = Literal["claude", "operator", "system"]


class SessionContextRefs(BaseModel):
    report_uuid: UUID | None = None
    item_uuid: UUID | None = None
    alert_uuid: UUID | None = None
    order_id: str | None = None
    journal_id: int | None = None
    symbols: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("symbols", mode="before")
    @classmethod
    def _clean_symbols(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("symbols must be a list")
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return cleaned


class SessionContextAppendEntry(BaseModel):
    kst_date: date | None = None
    market: MarketLiteral
    account_scope: AccountScopeLiteral | None = None
    entry_type: SessionContextEntryTypeLiteral
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    refs: SessionContextRefs = Field(default_factory=SessionContextRefs)
    created_by: SessionContextCreatedByLiteral = "claude"
    session_label: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("title", "body", "session_label", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped
        return value


class SessionContextRecentRequest(BaseModel):
    market: MarketLiteral | None = None
    account_scope: AccountScopeLiteral | None = None
    kst_date_from: date | None = None
    entry_type: SessionContextEntryTypeLiteral | None = None
    limit: int = Field(default=20, ge=1)

    model_config = ConfigDict(extra="forbid")

    @field_validator("limit", mode="before")
    @classmethod
    def _clamp_limit(cls, value: object) -> int:
        limit = 20 if value is None else int(value)
        return max(1, min(limit, 100))


class SessionContextResponse(BaseModel):
    entry_uuid: UUID
    kst_date: date
    market: MarketLiteral
    account_scope: AccountScopeLiteral | None
    entry_type: SessionContextEntryTypeLiteral
    title: str
    body: str
    refs: SessionContextRefs
    created_by: SessionContextCreatedByLiteral
    session_label: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SessionContextAppendResponse(BaseModel):
    success: Literal[True] = True
    count: int
    entries: list[SessionContextResponse]


class SessionContextRecentResponse(BaseModel):
    success: Literal[True] = True
    count: int
    filters: SessionContextRecentRequest
    entries: list[SessionContextResponse]
```

- [ ] **Step 4: Run schema tests**

Run:

```bash
uv run pytest tests/schemas/test_session_context_schemas.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add app/schemas/session_context.py tests/schemas/test_session_context_schemas.py
git commit -m "feat(ROB-516): add session context schemas"
```

---

### Task 3: Add Service Layer

**Files:**
- Create: `tests/services/test_session_context_service.py`
- Create: `app/services/session_context.py`

- [ ] **Step 1: Write service tests**

Create `tests/services/test_session_context_service.py`:

```python
from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.session_context import SessionContextAppendEntry
from app.services.session_context import SessionContextService


@pytest_asyncio.fixture(autouse=True)
async def _clean_session_context(db_session: AsyncSession):
    await db_session.execute(
        sa.text(
            'TRUNCATE TABLE review."operator_session_context" '
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()
    yield
    await db_session.execute(
        sa.text(
            'TRUNCATE TABLE review."operator_session_context" '
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_append_entries_defaults_kst_date_and_preserves_refs(
    db_session: AsyncSession,
) -> None:
    service = SessionContextService(db_session)
    entries = [
        SessionContextAppendEntry.model_validate(
            {
                "market": "kr",
                "account_scope": "kis_live",
                "entry_type": "deferred",
                "title": "DB 매도 보류",
                "body": "익절 조건만 허용되어 매도 제외",
                "refs": {"symbols": ["DB"], "journal_id": 11},
                "created_by": "claude",
            }
        )
    ]

    rows = await service.append_entries(entries)

    assert len(rows) == 1
    assert rows[0].kst_date is not None
    assert rows[0].refs == {"symbols": ["DB"], "journal_id": 11}


@pytest.mark.asyncio
async def test_get_recent_filters_and_orders_newest_first(
    db_session: AsyncSession,
) -> None:
    service = SessionContextService(db_session)
    await service.append_entries(
        [
            SessionContextAppendEntry.model_validate(
                {
                    "kst_date": "2026-06-10",
                    "market": "kr",
                    "entry_type": "next_action",
                    "title": "old",
                    "body": "older",
                }
            ),
            SessionContextAppendEntry.model_validate(
                {
                    "kst_date": "2026-06-11",
                    "market": "kr",
                    "account_scope": "kis_live",
                    "entry_type": "next_action",
                    "title": "new",
                    "body": "newer",
                }
            ),
            SessionContextAppendEntry.model_validate(
                {
                    "kst_date": "2026-06-11",
                    "market": "us",
                    "entry_type": "next_action",
                    "title": "us",
                    "body": "ignored",
                }
            ),
        ]
    )

    rows = await service.get_recent(
        market="kr",
        account_scope="kis_live",
        kst_date_from=date(2026, 6, 11),
        entry_type="next_action",
        limit=10,
    )

    assert [row.title for row in rows] == ["new"]


@pytest.mark.asyncio
async def test_get_recent_clamps_limit(db_session: AsyncSession) -> None:
    service = SessionContextService(db_session)
    await service.append_entries(
        [
            SessionContextAppendEntry.model_validate(
                {
                    "kst_date": "2026-06-11",
                    "market": "kr",
                    "entry_type": "handoff_note",
                    "title": f"note-{index}",
                    "body": "body",
                }
            )
            for index in range(3)
        ]
    )

    rows = await service.get_recent(limit=1_000)

    assert len(rows) == 3
```

- [ ] **Step 2: Run service tests and verify they fail**

Run:

```bash
uv run pytest tests/services/test_session_context_service.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.session_context'`.

- [ ] **Step 3: Create the service**

Create `app/services/session_context.py`:

```python
"""Service layer for ROB-516 operator session context entries."""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.session_context import OperatorSessionContext
from app.schemas.session_context import (
    SessionContextAppendEntry,
    SessionContextEntryTypeLiteral,
)
from app.schemas.investment_reports import AccountScopeLiteral, MarketLiteral


class SessionContextService:
    """Append-only writer and recent-query reader for operator context."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_entries(
        self,
        entries: list[SessionContextAppendEntry],
    ) -> list[OperatorSessionContext]:
        rows: list[OperatorSessionContext] = []
        default_kst_date = now_kst().date()
        for entry in entries:
            row = OperatorSessionContext(
                kst_date=entry.kst_date or default_kst_date,
                market=entry.market,
                account_scope=entry.account_scope,
                entry_type=entry.entry_type,
                title=entry.title,
                body=entry.body,
                refs=entry.refs.model_dump(mode="json", exclude_none=True),
                created_by=entry.created_by,
                session_label=entry.session_label,
            )
            self._session.add(row)
            rows.append(row)
        await self._session.flush()
        for row in rows:
            await self._session.refresh(row)
        return rows

    async def get_recent(
        self,
        *,
        market: MarketLiteral | None = None,
        account_scope: AccountScopeLiteral | None = None,
        kst_date_from: date | None = None,
        entry_type: SessionContextEntryTypeLiteral | None = None,
        limit: int = 20,
    ) -> list[OperatorSessionContext]:
        capped_limit = max(1, min(int(limit), 100))
        stmt = sa.select(OperatorSessionContext).order_by(
            OperatorSessionContext.created_at.desc(),
            OperatorSessionContext.id.desc(),
        )
        if market is not None:
            stmt = stmt.where(OperatorSessionContext.market == market)
        if account_scope is not None:
            stmt = stmt.where(OperatorSessionContext.account_scope == account_scope)
        if kst_date_from is not None:
            stmt = stmt.where(OperatorSessionContext.kst_date >= kst_date_from)
        if entry_type is not None:
            stmt = stmt.where(OperatorSessionContext.entry_type == entry_type)
        result = await self._session.scalars(stmt.limit(capped_limit))
        return list(result.all())
```

- [ ] **Step 4: Run service tests**

Run:

```bash
uv run pytest tests/services/test_session_context_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add app/services/session_context.py tests/services/test_session_context_service.py
git commit -m "feat(ROB-516): add session context service"
```

---

### Task 4: Add MCP Tools And Registration

**Files:**
- Create: `tests/test_session_context_mcp.py`
- Create: `app/mcp_server/tooling/session_context_tools.py`
- Create: `app/mcp_server/tooling/session_context_registration.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Modify: `app/mcp_server/tooling/__init__.py`

- [ ] **Step 1: Write MCP tests**

Create `tests/test_session_context_mcp.py`:

```python
from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.session_context_registration import (
    SESSION_CONTEXT_TOOL_NAMES,
    register_session_context_tools,
)
from app.mcp_server.tooling.session_context_tools import (
    session_context_append,
    session_context_get_recent,
)


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        assert description

        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


@pytest_asyncio.fixture(autouse=True)
async def _clean_session_context(db_session: AsyncSession):
    await db_session.execute(
        sa.text(
            'TRUNCATE TABLE review."operator_session_context" '
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()
    yield
    await db_session.execute(
        sa.text(
            'TRUNCATE TABLE review."operator_session_context" '
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


def test_session_context_tool_names_register() -> None:
    mcp = FakeMCP()

    register_session_context_tools(mcp)  # type: ignore[arg-type]

    assert SESSION_CONTEXT_TOOL_NAMES == {
        "session_context_append",
        "session_context_get_recent",
    }
    assert set(mcp.tools) == SESSION_CONTEXT_TOOL_NAMES


@pytest.mark.asyncio
async def test_append_and_get_recent_round_trip(db_session: AsyncSession) -> None:
    append_response = await session_context_append(
        entries=[
            {
                "kst_date": "2026-06-11",
                "market": "kr",
                "account_scope": "kis_live",
                "entry_type": "rejected_candidate",
                "title": "반도체 후보 제외",
                "body": "target_exceeded 조건으로 신규 추가 없음",
                "refs": {"symbols": ["005930", "000660"]},
                "created_by": "claude",
                "session_label": "kr-live-2026-06-11",
            }
        ]
    )

    assert append_response["success"] is True
    assert append_response["count"] == 1
    assert append_response["entries"][0]["refs"]["symbols"] == ["005930", "000660"]

    recent_response = await session_context_get_recent(
        market="kr",
        account_scope="kis_live",
        kst_date_from="2026-06-11",
        entry_type="rejected_candidate",
        limit=20,
    )

    assert recent_response["success"] is True
    assert recent_response["count"] == 1
    assert recent_response["entries"][0]["title"] == "반도체 후보 제외"
    assert recent_response["filters"]["market"] == "kr"


@pytest.mark.asyncio
async def test_append_rejects_empty_entries(db_session: AsyncSession) -> None:
    response = await session_context_append(entries=[])

    assert response == {
        "success": False,
        "error": "empty_entries",
        "hint": "Pass one or more session context entries.",
    }


@pytest.mark.asyncio
async def test_get_recent_returns_empty_list_when_no_match(
    db_session: AsyncSession,
) -> None:
    await session_context_append(
        entries=[
            {
                "kst_date": "2026-06-11",
                "market": "us",
                "entry_type": "handoff_note",
                "title": "US note",
                "body": "not KR",
            }
        ]
    )

    response = await session_context_get_recent(market="kr", limit=20)

    assert response["success"] is True
    assert response["count"] == 0
    assert response["entries"] == []
```

- [ ] **Step 2: Run MCP tests and verify they fail**

Run:

```bash
uv run pytest tests/test_session_context_mcp.py -v
```

Expected: FAIL with `ModuleNotFoundError` for the new MCP modules.

- [ ] **Step 3: Create MCP implementation functions**

Create `app/mcp_server/tooling/session_context_tools.py`:

```python
"""MCP tools for ROB-516 operator session context handoff."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.core.db import AsyncSessionLocal
from app.schemas.session_context import (
    SessionContextAppendEntry,
    SessionContextAppendResponse,
    SessionContextRecentRequest,
    SessionContextRecentResponse,
    SessionContextResponse,
)
from app.services.session_context import SessionContextService


def _validation_error(exc: ValidationError) -> dict[str, Any]:
    return {
        "success": False,
        "error": "invalid_request",
        "detail": exc.errors(),
    }


async def session_context_append(entries: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Append one or more operator session context entries."""
    if not entries:
        return {
            "success": False,
            "error": "empty_entries",
            "hint": "Pass one or more session context entries.",
        }
    try:
        validated = [
            SessionContextAppendEntry.model_validate(entry) for entry in entries
        ]
    except ValidationError as exc:
        return _validation_error(exc)

    async with AsyncSessionLocal() as db:
        service = SessionContextService(db)
        rows = await service.append_entries(validated)
        await db.commit()
        response = SessionContextAppendResponse(
            count=len(rows),
            entries=[SessionContextResponse.model_validate(row) for row in rows],
        )
    return response.model_dump(mode="json")


async def session_context_get_recent(
    market: str | None = None,
    account_scope: str | None = None,
    kst_date_from: str | None = None,
    entry_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return recent operator session context entries, newest first."""
    try:
        request = SessionContextRecentRequest.model_validate(
            {
                "market": market,
                "account_scope": account_scope,
                "kst_date_from": kst_date_from,
                "entry_type": entry_type,
                "limit": limit,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)

    async with AsyncSessionLocal() as db:
        service = SessionContextService(db)
        rows = await service.get_recent(
            market=request.market,
            account_scope=request.account_scope,
            kst_date_from=request.kst_date_from,
            entry_type=request.entry_type,
            limit=request.limit,
        )
        response = SessionContextRecentResponse(
            count=len(rows),
            filters=request,
            entries=[SessionContextResponse.model_validate(row) for row in rows],
        )
    return response.model_dump(mode="json")
```

- [ ] **Step 4: Create MCP registration module**

Create `app/mcp_server/tooling/session_context_registration.py`:

```python
"""MCP registration for ROB-516 session context tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.session_context_tools import (
    session_context_append,
    session_context_get_recent,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


SESSION_CONTEXT_TOOL_NAMES: set[str] = {
    "session_context_append",
    "session_context_get_recent",
}


def register_session_context_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="session_context_append",
        description=(
            "Append operator session context entries for cross-session handoff. "
            "Use for plans, decisions, deferred items, rejected candidates, "
            "constraints, open questions, next actions, and handoff notes. "
            "This is append-only operational context, not an investment report."
        ),
    )(session_context_append)
    _ = mcp.tool(
        name="session_context_get_recent",
        description=(
            "Read recent operator session context entries, newest first. "
            "Optional filters: market, account_scope, kst_date_from, entry_type, "
            "limit clamped to 1..100. Call this at new-session startup before "
            "running the next trading tournament."
        ),
    )(session_context_get_recent)


__all__ = [
    "SESSION_CONTEXT_TOOL_NAMES",
    "register_session_context_tools",
]
```

- [ ] **Step 5: Wire registration into the registry**

Modify `app/mcp_server/tooling/registry.py`:

```python
from app.mcp_server.tooling.session_context_registration import (
    register_session_context_tools,
)
```

Inside `register_all_tools`, place this after `register_investment_report_tools(...)`:

```python
    register_session_context_tools(mcp)
```

- [ ] **Step 6: Update lazy exports**

Modify `app/mcp_server/tooling/__init__.py` so callers can import the registration function through the package. Add the mapping entry:

```python
    "register_session_context_tools": (
        "app.mcp_server.tooling.session_context_registration",
        "register_session_context_tools",
    ),
```

Add `"register_session_context_tools"` to `__all__`.

- [ ] **Step 7: Run MCP tests**

Run:

```bash
uv run pytest tests/test_session_context_mcp.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit Task 4**

```bash
git add app/mcp_server/tooling/session_context_tools.py app/mcp_server/tooling/session_context_registration.py app/mcp_server/tooling/registry.py app/mcp_server/tooling/__init__.py tests/test_session_context_mcp.py
git commit -m "feat(ROB-516): expose session context MCP tools"
```

---

### Task 5: Document MCP Contract

**Files:**
- Modify: `app/mcp_server/README.md`

- [ ] **Step 1: Add README section**

In `app/mcp_server/README.md`, add this section near the investment report tools:

```markdown
### Session Context Tools

`session_context_append(entries)` persists append-only operator context for
cross-session handoff. It is for "where did we leave off?" state: plans,
decisions, deferred items, rejected candidates, constraints, open questions,
next actions, and handoff notes. It is not an investment report, research
session, trade journal, watch alert, or order ledger.

Each entry accepts:

- `kst_date` optional `YYYY-MM-DD`; defaults to current KST date.
- `market` required: `kr`, `us`, or `crypto`.
- `account_scope` optional: `kis_live`, `kis_mock`, `alpaca_paper`, `upbit_live`.
- `entry_type` required: `plan`, `decision`, `deferred`,
  `rejected_candidate`, `constraint`, `open_question`, `next_action`,
  `handoff_note`.
- `title` required short title.
- `body` required markdown body.
- `refs` optional object: `report_uuid`, `item_uuid`, `alert_uuid`, `order_id`,
  `journal_id`, `symbols`.
- `created_by` optional: `claude`, `operator`, `system`; defaults to `claude`.
- `session_label` optional grouping label.

`session_context_get_recent(market?, account_scope?, kst_date_from?, entry_type?, limit)`
returns recent entries newest first. `limit` is clamped to 1..100 and defaults
to 20. New trading sessions should call this before comparing yesterday's plan
with today's candidate tournament.
```

- [ ] **Step 2: Commit Task 5**

```bash
git add app/mcp_server/README.md
git commit -m "docs(ROB-516): document session context MCP tools"
```

---

### Task 6: Verification And Review Gates

**Files:**
- No source files created in this task.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/models/test_operator_session_context_model.py tests/schemas/test_session_context_schemas.py tests/services/test_session_context_service.py tests/test_session_context_mcp.py -v
```

Expected: PASS.

- [ ] **Step 2: Run MCP regression test around existing investment report tool names**

Run:

```bash
uv run pytest tests/test_investment_reports_mcp.py::test_tool_names_match_registered_set -v
```

Expected: PASS. The existing investment report tool set should not change.

- [ ] **Step 3: Run type checking on changed app modules**

Run:

```bash
uv run ty check app/models/session_context.py app/schemas/session_context.py app/services/session_context.py app/mcp_server/tooling/session_context_tools.py app/mcp_server/tooling/session_context_registration.py
```

Expected: PASS.

- [ ] **Step 4: Verify Alembic head locally**

Run:

```bash
uv run alembic heads
```

Expected output includes exactly one head for this branch:

```text
20260611_rob516 (head)
```

- [ ] **Step 5: Run migration upgrade on local test database**

Run:

```bash
uv run alembic upgrade head
```

Expected: completes without errors and creates `review.operator_session_context`.

- [ ] **Step 6: Confirm no accidental order/live-trading surface changes**

Run:

```bash
git diff -- app/services app/mcp_server/tooling app/models app/schemas alembic/versions app/mcp_server/README.md tests | rg -n "place_order|submit_order|cancel_order|modify_order|kis_live_|upbit_live|broker"
```

Expected: no new order-execution calls. Matches in docs, literals, or existing account-scope strings are acceptable after manual inspection.

- [ ] **Step 7: Record verification in the PR body**

No verification file is required for this repository. Record commands and results in the PR body.

---

## Self-Review

- Spec coverage: The plan implements `session_context_append(entries[])`, `session_context_get_recent(...)`, append-only persistence, refs to report/item/alert/order/journal/symbols, market/date/type filters, and avoids `research_session_*` naming.
- Scope control: UI timeline, `get_operating_briefing`, active watch listing, report-context integration, and live order behavior are excluded.
- High-risk guardrail: The issue already carries `high_risk_change` and `needs_stronger_model_review`; do not merge, deploy, or operationally rely on this migration until stronger-model/CTO review clears it.
- Type consistency: Public names are `OperatorSessionContext`, `SessionContextService`, `session_context_append`, and `session_context_get_recent` across schemas, services, MCP tools, tests, and docs.
