# ROB-118 & ROB-119 — 주문 Preview/승인 + 미체결/체결 관리 페이지 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ADA 3분할 매도 trial로 검증된 안전 흐름 (`action 후보 → 분할 주문안 생성 → dry-run preview → 사용자 승인 → 실제 주문 제출 → pending 주문 확인`) 을 operator-facing SPA 페이지 두 개로 만든다. ROB-118 은 multi-leg preview/승인/제출 페이지, ROB-119 은 미체결/체결 read-only 관리 페이지를 한 묶음으로 구현한다.

**Architecture:** 기존 `OrderIntentPreviewService` (pure transform), `app.services.orders.service.place_order` (broker submit), `n8n_pending_orders_service` / `n8n_filled_orders_service` (pending/fills) 를 재사용한다. ROB-118 은 audit trail 을 위해 `order_preview_session` / `order_preview_leg` / `order_execution_request` 3개 테이블을 추가하고, 모든 mutation 은 새 `OrderPreviewSessionService` 한 곳을 통해서만 일어난다. 승인 게이트는 `weekend_crypto_paper_cycle_runner` 의 `operator_token + per-candidate approval_token` 패턴을 그대로 차용한다. SPA 측은 React Router 의 `/trading/decisions/order-preview/:previewId` 와 `/trading/decisions/orders` 두 라우트를 추가하고 기존 `apiFetch` 클라이언트를 통해 새 router 와 통신한다.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + Alembic, React 18 + React Router (Vite) + TypeScript, pytest (`tests/routers`, `tests/services`), httpx ASGITransport. Database: PostgreSQL.

**관련 코드 경로 (착수 전 반드시 읽기):**
- `app/routers/portfolio_actions.py` — 새 router 의 골격 참고용
- `app/routers/portfolio.py:200-260` — 기존 OrderIntent preview endpoint 시그니처
- `app/services/order_intent_preview_service.py` — `build_preview` 재사용 대상
- `app/services/orders/service.py:92` — `place_order` (broker submit)
- `app/services/n8n_pending_orders_service.py:278` — `fetch_pending_orders`
- `app/services/n8n_filled_orders_service.py:294` — `fetch_filled_orders`
- `app/services/weekend_crypto_paper_cycle_runner.py:200-285` — operator/approval token 게이트
- `frontend/trading-decision/src/routes.tsx` — SPA 라우트 등록
- `frontend/trading-decision/src/api/client.ts` — `apiFetch` (`/trading/api` prefix)
- `frontend/trading-decision/src/api/portfolioActions.ts` — API 모듈 컨벤션
- `tests/routers/test_portfolio_actions.py` — router 테스트 패턴

**Project Memory & 안전 경계 (CLAUDE.md):**
- 직접 SQL `INSERT/UPDATE/DELETE` 금지. 모든 쓰기는 `OrderPreviewSessionService` 경유.
- 1차 ROB-119 는 read-only. cancel/modify 는 disabled (explanation 표시) 만.
- broker side effect 는 모든 unit/integration 테스트에서 mock.
- ROB-119 schema 추가/변경 없음 (read-only 라 신규 모델 없음).

---

## File Structure

### Backend (ROB-118)
- **Create** `alembic/versions/2026_05_06_rob118_order_preview_session.py` — 3-table migration.
- **Create** `app/models/order_preview_session.py` — `OrderPreviewSession`, `OrderPreviewLeg`, `OrderExecutionRequest` ORM.
- **Create** `app/schemas/order_preview_session.py` — Pydantic v2 request/response models.
- **Create** `app/services/order_preview_session_service.py` — preview 생성/refresh/submit, 모든 DB 쓰기 단일 진입점.
- **Create** `app/routers/order_previews.py` — `POST/GET` endpoints (`/trading/api/order-previews/...`).
- **Modify** `app/main.py` — `order_previews.router` include.

### Backend (ROB-119)
- **Create** `app/services/operator_orders_service.py` — `fetch_open_orders / fetch_history / fetch_fills` thin wrapper (filter, stale-warning enrich).
- **Create** `app/routers/operator_orders.py` — `GET /trading/api/orders/open|history|fills`.
- **Modify** `app/main.py` — `operator_orders.router` include.

### Frontend (ROB-118)
- **Create** `frontend/trading-decision/src/api/orderPreviews.ts` — `createPreview / refreshPreview / submitPreview / getPreview`.
- **Create** `frontend/trading-decision/src/pages/OrderPreviewPage.tsx` (+ `.module.css`) — 3 단계 UI: preview status, legs table, approval gate.
- **Modify** `frontend/trading-decision/src/routes.tsx` — `/order-preview/:previewId` 라우트 등록.
- **Modify** `frontend/trading-decision/src/api/types.ts` — preview/leg/execution typing 추가.

### Frontend (ROB-119)
- **Create** `frontend/trading-decision/src/api/operatorOrders.ts` — `getOpenOrders / getHistory / getFills`.
- **Create** `frontend/trading-decision/src/pages/OperatorOrdersPage.tsx` (+ `.module.css`).
- **Modify** `frontend/trading-decision/src/routes.tsx` — `/orders` 라우트 등록.

### Tests
- **Create** `tests/services/test_order_preview_session_service.py`
- **Create** `tests/routers/test_order_previews.py`
- **Create** `tests/services/test_operator_orders_service.py`
- **Create** `tests/routers/test_operator_orders.py`
- **Create** `frontend/trading-decision/src/__tests__/OrderPreviewPage.test.tsx`
- **Create** `frontend/trading-decision/src/__tests__/OperatorOrdersPage.test.tsx`
- **Create** `tests/fixtures/ada_three_leg_preview.json` — ADA 3분할 매도 fixture (multi-leg sample).

---

# Phase A — ROB-118 Backend (Preview Session 영구화 + API)

## Task A1: Alembic migration — 3 tables

**Files:**
- Create: `alembic/versions/2026_05_06_rob118_order_preview_session.py`

- [ ] **Step 1: 새 마이그레이션 파일 생성**

```bash
uv run alembic revision -m "rob118_order_preview_session" --rev-id 2026_05_06_rob118
```

- [ ] **Step 2: migration 본문 작성**

```python
"""rob118_order_preview_session

Revision ID: 2026_05_06_rob118
Revises: <FILL_FROM_alembic_current>
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2026_05_06_rob118"
down_revision = "<FILL_FROM_alembic_current>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_preview_session",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("preview_uuid", sa.String(36), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),  # portfolio_action | candidate | research_run
        sa.Column("source_ref", sa.String(255), nullable=True),
        sa.Column("research_session_id", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),  # equity_kr | equity_us | crypto
        sa.Column("venue", sa.String(32), nullable=False),  # live | paper | crypto_live | ...
        sa.Column("side", sa.String(8), nullable=False),  # buy | sell
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        # created | preview_passed | preview_failed | submitted | submit_failed | canceled
        sa.Column("dry_run_payload", sa.JSON, nullable=True),
        sa.Column("dry_run_error", sa.JSON, nullable=True),
        sa.Column("approval_token", sa.String(64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_order_preview_session_user", "order_preview_session", ["user_id", "created_at"])

    op.create_table(
        "order_preview_leg",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.BigInteger, sa.ForeignKey("order_preview_session.id", ondelete="CASCADE"), nullable=False),
        sa.Column("leg_index", sa.Integer, nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False, server_default="limit"),
        sa.Column("estimated_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("estimated_fee", sa.Numeric(20, 8), nullable=True),
        sa.Column("expected_pnl", sa.Numeric(20, 8), nullable=True),
        sa.Column("dry_run_status", sa.String(32), nullable=True),  # passed | failed | skipped
        sa.Column("dry_run_error", sa.JSON, nullable=True),
        sa.UniqueConstraint("session_id", "leg_index", name="uq_preview_leg_session_idx"),
    )

    op.create_table(
        "order_execution_request",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.BigInteger, sa.ForeignKey("order_preview_session.id", ondelete="CASCADE"), nullable=False),
        sa.Column("leg_id", sa.BigInteger, sa.ForeignKey("order_preview_leg.id", ondelete="CASCADE"), nullable=False),
        sa.Column("broker_order_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),  # submitted | rejected | failed
        sa.Column("error_payload", sa.JSON, nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("order_execution_request")
    op.drop_table("order_preview_leg")
    op.drop_index("ix_order_preview_session_user", table_name="order_preview_session")
    op.drop_table("order_preview_session")
```

`down_revision` 값은 `uv run alembic current` 결과로 채울 것.

- [ ] **Step 3: 마이그레이션 적용 + 롤백 dry-run**

Run:
```bash
uv run alembic current
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```
Expected: 모두 에러 없이 성공.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/2026_05_06_rob118_order_preview_session.py
git commit -m "feat(ROB-118): add order_preview_session/leg/execution_request tables"
```

---

## Task A2: ORM models

**Files:**
- Create: `app/models/order_preview_session.py`
- Test: `tests/services/test_order_preview_session_service.py` (model import smoke 포함)

- [ ] **Step 1: 실패 테스트 작성 (model import + 기본 컬럼 존재 확인)**

`tests/services/test_order_preview_session_service.py`:

```python
import pytest

@pytest.mark.unit
def test_order_preview_session_model_columns_exist():
    from app.models.order_preview_session import (
        OrderExecutionRequest,
        OrderPreviewLeg,
        OrderPreviewSession,
    )

    assert "preview_uuid" in OrderPreviewSession.__table__.columns
    assert "status" in OrderPreviewSession.__table__.columns
    assert "leg_index" in OrderPreviewLeg.__table__.columns
    assert "broker_order_id" in OrderExecutionRequest.__table__.columns
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/services/test_order_preview_session_service.py::test_order_preview_session_model_columns_exist -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.order_preview_session'`.

- [ ] **Step 3: 모델 작성**

`app/models/order_preview_session.py`:

```python
"""ROB-118 — Order preview session ORM models.

All writes must go through OrderPreviewSessionService.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class OrderPreviewSession(Base):
    __tablename__ = "order_preview_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    preview_uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    research_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    dry_run_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dry_run_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approval_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    legs: Mapped[list["OrderPreviewLeg"]] = relationship(
        "OrderPreviewLeg",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="OrderPreviewLeg.leg_index",
    )
    executions: Mapped[list["OrderExecutionRequest"]] = relationship(
        "OrderExecutionRequest",
        back_populates="session",
        cascade="all, delete-orphan",
    )


class OrderPreviewLeg(Base):
    __tablename__ = "order_preview_leg"
    __table_args__ = (
        UniqueConstraint("session_id", "leg_index", name="uq_preview_leg_session_idx"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("order_preview_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="limit")
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    estimated_fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    expected_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    dry_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dry_run_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    session: Mapped[OrderPreviewSession] = relationship(
        "OrderPreviewSession", back_populates="legs"
    )


class OrderExecutionRequest(Base):
    __tablename__ = "order_execution_request"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("order_preview_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    leg_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("order_preview_leg.id", ondelete="CASCADE"),
        nullable=False,
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[OrderPreviewSession] = relationship(
        "OrderPreviewSession", back_populates="executions"
    )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/services/test_order_preview_session_service.py::test_order_preview_session_model_columns_exist -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models/order_preview_session.py tests/services/test_order_preview_session_service.py
git commit -m "feat(ROB-118): add OrderPreviewSession/Leg/ExecutionRequest ORM"
```

---

## Task A3: Pydantic schemas

**Files:**
- Create: `app/schemas/order_preview_session.py`

- [ ] **Step 1: 실패 테스트 (schema import + 검증)**

Append to `tests/services/test_order_preview_session_service.py`:

```python
@pytest.mark.unit
def test_create_preview_request_validates_required_fields():
    from pydantic import ValidationError

    from app.schemas.order_preview_session import CreatePreviewRequest, PreviewLegInput

    valid = CreatePreviewRequest(
        source_kind="portfolio_action",
        source_ref="action-uuid-1",
        symbol="KRW-ADA",
        market="crypto",
        venue="crypto_live",
        side="sell",
        legs=[
            PreviewLegInput(leg_index=0, quantity="33.33", price="650.0"),
            PreviewLegInput(leg_index=1, quantity="33.33", price="660.0"),
            PreviewLegInput(leg_index=2, quantity="33.34", price="670.0"),
        ],
    )
    assert len(valid.legs) == 3

    with pytest.raises(ValidationError):
        CreatePreviewRequest(
            source_kind="portfolio_action",
            symbol="KRW-ADA",
            market="crypto",
            venue="crypto_live",
            side="sell",
            legs=[],  # empty rejected
        )
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/services/test_order_preview_session_service.py::test_create_preview_request_validates_required_fields -v`
Expected: FAIL (`No module named 'app.schemas.order_preview_session'`).

- [ ] **Step 3: schema 작성**

`app/schemas/order_preview_session.py`:

```python
"""ROB-118 — Order preview session schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Market = Literal["equity_kr", "equity_us", "crypto"]
Side = Literal["buy", "sell"]
Status = Literal[
    "created",
    "preview_passed",
    "preview_failed",
    "submitted",
    "submit_failed",
    "canceled",
]


class PreviewLegInput(BaseModel):
    leg_index: int = Field(ge=0)
    quantity: Decimal = Field(gt=Decimal(0))
    price: Decimal | None = None
    order_type: Literal["limit", "market"] = "limit"

    @field_validator("price")
    @classmethod
    def _price_for_limit(cls, v: Decimal | None, info) -> Decimal | None:
        ot = info.data.get("order_type", "limit")
        if ot == "limit" and v is None:
            raise ValueError("limit order requires price")
        return v


class CreatePreviewRequest(BaseModel):
    source_kind: Literal["portfolio_action", "candidate", "research_run"]
    source_ref: str | None = None
    research_session_id: str | None = None
    symbol: str = Field(min_length=1)
    market: Market
    venue: str = Field(min_length=1)
    side: Side
    legs: list[PreviewLegInput] = Field(min_length=1)


class PreviewLegOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    leg_index: int
    quantity: Decimal
    price: Decimal | None
    order_type: str
    estimated_value: Decimal | None
    estimated_fee: Decimal | None
    expected_pnl: Decimal | None
    dry_run_status: str | None
    dry_run_error: dict | None


class ExecutionRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    leg_index: int
    broker_order_id: str | None
    status: str
    error_payload: dict | None
    submitted_at: datetime


class PreviewSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    preview_uuid: str
    source_kind: str
    source_ref: str | None
    research_session_id: str | None
    symbol: str
    market: Market
    venue: str
    side: Side
    status: Status
    legs: list[PreviewLegOut]
    executions: list[ExecutionRequestOut] = []
    dry_run_error: dict | None = None
    approved_at: datetime | None
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SubmitPreviewRequest(BaseModel):
    approval_token: str = Field(min_length=8)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/services/test_order_preview_session_service.py -v`
Expected: 두 테스트 모두 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/order_preview_session.py tests/services/test_order_preview_session_service.py
git commit -m "feat(ROB-118): add order preview session pydantic schemas"
```

---

## Task A4: `OrderPreviewSessionService.create_preview` (failing test)

**Files:**
- Test: `tests/services/test_order_preview_session_service.py`

- [ ] **Step 1: 실패 테스트 작성 — 3분할 ADA 매도 preview 생성**

Append:

```python
import asyncio

import pytest
from unittest.mock import AsyncMock

from app.schemas.order_preview_session import CreatePreviewRequest, PreviewLegInput


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_preview_persists_three_legs_for_ada_sell(db_session) -> None:
    from app.services.order_preview_session_service import OrderPreviewSessionService

    fake_dry_run = AsyncMock()
    fake_dry_run.run.return_value = {
        "ok": True,
        "legs": [
            {"leg_index": 0, "estimated_value": "21666.5", "estimated_fee": "10.83"},
            {"leg_index": 1, "estimated_value": "22000.0", "estimated_fee": "11.0"},
            {"leg_index": 2, "estimated_value": "22338.5", "estimated_fee": "11.16"},
        ],
    }
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)

    req = CreatePreviewRequest(
        source_kind="portfolio_action",
        source_ref="action-uuid-1",
        symbol="KRW-ADA",
        market="crypto",
        venue="crypto_live",
        side="sell",
        legs=[
            PreviewLegInput(leg_index=0, quantity="33.33", price="650.0"),
            PreviewLegInput(leg_index=1, quantity="33.33", price="660.0"),
            PreviewLegInput(leg_index=2, quantity="33.34", price="670.0"),
        ],
    )

    out = await service.create_preview(user_id=1, request=req)

    assert out.status == "preview_passed"
    assert len(out.legs) == 3
    assert {leg.leg_index for leg in out.legs} == {0, 1, 2}
    assert all(leg.dry_run_status == "passed" for leg in out.legs)
    fake_dry_run.run.assert_awaited_once()
```

`db_session` fixture 는 기존 `tests/conftest.py` 의 async session fixture 를 따른다. 없으면 동일 패턴을 conftest 에 추가 (참고: `tests/services/test_portfolio_action_service.py`).

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/services/test_order_preview_session_service.py::test_create_preview_persists_three_legs_for_ada_sell -v`
Expected: FAIL (`OrderPreviewSessionService` 미구현).

- [ ] **Step 3: Commit (failing test)**

```bash
git add tests/services/test_order_preview_session_service.py
git commit -m "test(ROB-118): add failing 3-leg ADA sell preview test"
```

---

## Task A5: `OrderPreviewSessionService.create_preview` 구현

**Files:**
- Create: `app/services/order_preview_session_service.py`

- [ ] **Step 1: service 본문 작성**

```python
"""ROB-118 — OrderPreviewSessionService.

This is the ONLY allowed write path for order_preview_session/leg/execution_request.
All callers must go through this service. Direct SQL writes are forbidden.
"""

from __future__ import annotations

import secrets
import uuid
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order_preview_session import (
    OrderExecutionRequest,
    OrderPreviewLeg,
    OrderPreviewSession,
)
from app.schemas.order_preview_session import (
    CreatePreviewRequest,
    PreviewSessionOut,
    SubmitPreviewRequest,
)


class DryRunRunner(Protocol):
    async def run(self, *, payload: dict[str, Any]) -> dict[str, Any]: ...


class PreviewSessionNotFoundError(Exception):
    pass


class PreviewSchemaMismatchError(Exception):
    """Raised when broker tool schema mismatch is detected — fail-closed."""


class PreviewNotApprovedError(Exception):
    pass


class OrderPreviewSessionService:
    def __init__(self, *, db: AsyncSession, dry_run: DryRunRunner) -> None:
        self._db = db
        self._dry_run = dry_run

    async def create_preview(
        self, *, user_id: int, request: CreatePreviewRequest
    ) -> PreviewSessionOut:
        session = OrderPreviewSession(
            preview_uuid=str(uuid.uuid4()),
            user_id=user_id,
            source_kind=request.source_kind,
            source_ref=request.source_ref,
            research_session_id=request.research_session_id,
            symbol=request.symbol,
            market=request.market,
            venue=request.venue,
            side=request.side,
            status="created",
            approval_token=secrets.token_urlsafe(24),
        )
        for leg_in in request.legs:
            session.legs.append(
                OrderPreviewLeg(
                    leg_index=leg_in.leg_index,
                    quantity=leg_in.quantity,
                    price=leg_in.price,
                    order_type=leg_in.order_type,
                )
            )
        self._db.add(session)
        await self._db.flush()

        await self._run_dry_run_inplace(session)

        await self._db.commit()
        await self._db.refresh(session, attribute_names=["legs", "executions"])
        return PreviewSessionOut.model_validate(session)

    async def refresh_preview(
        self, *, user_id: int, preview_uuid: str
    ) -> PreviewSessionOut:
        session = await self._load_owned(user_id=user_id, preview_uuid=preview_uuid)
        await self._run_dry_run_inplace(session)
        await self._db.commit()
        await self._db.refresh(session, attribute_names=["legs", "executions"])
        return PreviewSessionOut.model_validate(session)

    async def submit_preview(
        self,
        *,
        user_id: int,
        preview_uuid: str,
        request: SubmitPreviewRequest,
        broker_submit,  # async (leg) -> {"order_id": str, ...}
    ) -> PreviewSessionOut:
        session = await self._load_owned(user_id=user_id, preview_uuid=preview_uuid)

        if session.status != "preview_passed":
            raise PreviewNotApprovedError(
                f"submit blocked: status={session.status}"
            )
        if not session.approval_token or not secrets.compare_digest(
            session.approval_token, request.approval_token
        ):
            raise PreviewNotApprovedError("approval_token mismatch")

        from datetime import datetime, timezone

        session.approved_at = datetime.now(timezone.utc)

        any_failure = False
        for leg in session.legs:
            try:
                result = await broker_submit(leg=leg, session=session)
            except Exception as exc:  # noqa: BLE001
                any_failure = True
                self._db.add(
                    OrderExecutionRequest(
                        session_id=session.id,
                        leg_id=leg.id,
                        broker_order_id=None,
                        status="failed",
                        error_payload={"message": str(exc)},
                    )
                )
                continue
            self._db.add(
                OrderExecutionRequest(
                    session_id=session.id,
                    leg_id=leg.id,
                    broker_order_id=str(result.get("order_id") or "") or None,
                    status="submitted",
                    error_payload=None,
                )
            )

        session.status = "submit_failed" if any_failure else "submitted"
        session.submitted_at = datetime.now(timezone.utc)
        await self._db.commit()
        await self._db.refresh(session, attribute_names=["legs", "executions"])
        return PreviewSessionOut.model_validate(session)

    async def get(self, *, user_id: int, preview_uuid: str) -> PreviewSessionOut:
        session = await self._load_owned(user_id=user_id, preview_uuid=preview_uuid)
        return PreviewSessionOut.model_validate(session)

    async def _run_dry_run_inplace(self, session: OrderPreviewSession) -> None:
        payload = {
            "symbol": session.symbol,
            "market": session.market,
            "venue": session.venue,
            "side": session.side,
            "legs": [
                {
                    "leg_index": leg.leg_index,
                    "quantity": str(leg.quantity),
                    "price": str(leg.price) if leg.price is not None else None,
                    "order_type": leg.order_type,
                }
                for leg in session.legs
            ],
        }
        try:
            result = await self._dry_run.run(payload=payload)
        except PreviewSchemaMismatchError as exc:
            session.status = "preview_failed"
            session.dry_run_error = {"kind": "schema_mismatch", "message": str(exc)}
            for leg in session.legs:
                leg.dry_run_status = "failed"
                leg.dry_run_error = {"kind": "schema_mismatch"}
            return
        except Exception as exc:  # noqa: BLE001 — fail-closed on any preview error
            session.status = "preview_failed"
            session.dry_run_error = {"kind": "exception", "message": str(exc)}
            for leg in session.legs:
                leg.dry_run_status = "failed"
                leg.dry_run_error = {"message": str(exc)}
            return

        if not result.get("ok"):
            session.status = "preview_failed"
            session.dry_run_error = result
            for leg in session.legs:
                leg.dry_run_status = "failed"
                leg.dry_run_error = result
            return

        session.dry_run_payload = result
        result_legs = {l["leg_index"]: l for l in result.get("legs", [])}
        for leg in session.legs:
            r = result_legs.get(leg.leg_index)
            if r is None:
                # Schema mismatch — fail-closed
                session.status = "preview_failed"
                session.dry_run_error = {
                    "kind": "schema_mismatch",
                    "message": f"missing leg_index={leg.leg_index} in dry_run result",
                }
                leg.dry_run_status = "failed"
                leg.dry_run_error = {"kind": "schema_mismatch"}
                return
            leg.estimated_value = _to_decimal(r.get("estimated_value"))
            leg.estimated_fee = _to_decimal(r.get("estimated_fee"))
            leg.expected_pnl = _to_decimal(r.get("expected_pnl"))
            leg.dry_run_status = "passed"
            leg.dry_run_error = None
        session.status = "preview_passed"

    async def _load_owned(
        self, *, user_id: int, preview_uuid: str
    ) -> OrderPreviewSession:
        stmt = (
            select(OrderPreviewSession)
            .where(
                OrderPreviewSession.preview_uuid == preview_uuid,
                OrderPreviewSession.user_id == user_id,
            )
            .options(
                selectinload(OrderPreviewSession.legs),
                selectinload(OrderPreviewSession.executions),
            )
        )
        result = await self._db.execute(stmt)
        session = result.scalar_one_or_none()
        if session is None:
            raise PreviewSessionNotFoundError(preview_uuid)
        return session


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))
```

- [ ] **Step 2: 테스트 통과 확인**

Run: `uv run pytest tests/services/test_order_preview_session_service.py -v`
Expected: 모든 테스트 PASS.

- [ ] **Step 3: Commit**

```bash
git add app/services/order_preview_session_service.py
git commit -m "feat(ROB-118): OrderPreviewSessionService create_preview with fail-closed dry-run"
```

---

## Task A6: refresh + schema mismatch fail-closed test

**Files:**
- Test: `tests/services/test_order_preview_session_service.py`

- [ ] **Step 1: 실패 테스트 추가**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_schema_mismatch_marks_preview_failed(db_session) -> None:
    from app.services.order_preview_session_service import (
        OrderPreviewSessionService,
        PreviewSchemaMismatchError,
    )

    fake_dry_run = AsyncMock()
    fake_dry_run.run.side_effect = PreviewSchemaMismatchError("legs missing field")
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)

    req = CreatePreviewRequest(
        source_kind="portfolio_action",
        symbol="KRW-ADA",
        market="crypto",
        venue="crypto_live",
        side="sell",
        legs=[PreviewLegInput(leg_index=0, quantity="1", price="650")],
    )
    out = await service.create_preview(user_id=1, request=req)
    assert out.status == "preview_failed"
    assert out.dry_run_error and out.dry_run_error["kind"] == "schema_mismatch"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refresh_recomputes_dry_run(db_session) -> None:
    from app.services.order_preview_session_service import OrderPreviewSessionService

    fake_dry_run = AsyncMock()
    fake_dry_run.run.return_value = {
        "ok": True,
        "legs": [{"leg_index": 0, "estimated_value": "100", "estimated_fee": "0.1"}],
    }
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)
    out = await service.create_preview(
        user_id=1,
        request=CreatePreviewRequest(
            source_kind="portfolio_action",
            symbol="KRW-ADA",
            market="crypto",
            venue="crypto_live",
            side="sell",
            legs=[PreviewLegInput(leg_index=0, quantity="1", price="650")],
        ),
    )

    fake_dry_run.run.return_value = {
        "ok": True,
        "legs": [{"leg_index": 0, "estimated_value": "200", "estimated_fee": "0.2"}],
    }
    refreshed = await service.refresh_preview(
        user_id=1, preview_uuid=out.preview_uuid
    )
    assert refreshed.legs[0].estimated_value == Decimal("200")
```

- [ ] **Step 2: 실행 — 모두 PASS 여야 함 (구현은 A5 에서 완료)**

Run: `uv run pytest tests/services/test_order_preview_session_service.py -v`
Expected: PASS (구현이 fail-closed 와 refresh 를 이미 처리).

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_order_preview_session_service.py
git commit -m "test(ROB-118): cover schema mismatch fail-closed and refresh"
```

---

## Task A7: submit gate test (preview_passed + approval_token 필수)

**Files:**
- Test: `tests/services/test_order_preview_session_service.py`

- [ ] **Step 1: 실패 테스트 추가**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_blocked_when_status_not_preview_passed(db_session) -> None:
    from app.schemas.order_preview_session import SubmitPreviewRequest
    from app.services.order_preview_session_service import (
        OrderPreviewSessionService,
        PreviewNotApprovedError,
    )

    fake_dry_run = AsyncMock()
    fake_dry_run.run.return_value = {"ok": False, "error": "out_of_session"}
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)
    out = await service.create_preview(
        user_id=1,
        request=CreatePreviewRequest(
            source_kind="portfolio_action",
            symbol="KRW-ADA",
            market="crypto",
            venue="crypto_live",
            side="sell",
            legs=[PreviewLegInput(leg_index=0, quantity="1", price="650")],
        ),
    )
    assert out.status == "preview_failed"

    broker = AsyncMock()
    with pytest.raises(PreviewNotApprovedError):
        await service.submit_preview(
            user_id=1,
            preview_uuid=out.preview_uuid,
            request=SubmitPreviewRequest(approval_token="anything-long-enough"),
            broker_submit=broker,
        )
    broker.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_records_broker_order_ids(db_session) -> None:
    from app.schemas.order_preview_session import SubmitPreviewRequest
    from app.services.order_preview_session_service import OrderPreviewSessionService

    fake_dry_run = AsyncMock()
    fake_dry_run.run.return_value = {
        "ok": True,
        "legs": [
            {"leg_index": 0, "estimated_value": "100"},
            {"leg_index": 1, "estimated_value": "100"},
            {"leg_index": 2, "estimated_value": "100"},
        ],
    }
    service = OrderPreviewSessionService(db=db_session, dry_run=fake_dry_run)
    out = await service.create_preview(
        user_id=1,
        request=CreatePreviewRequest(
            source_kind="portfolio_action",
            symbol="KRW-ADA",
            market="crypto",
            venue="crypto_live",
            side="sell",
            legs=[
                PreviewLegInput(leg_index=0, quantity="1", price="650"),
                PreviewLegInput(leg_index=1, quantity="1", price="660"),
                PreviewLegInput(leg_index=2, quantity="1", price="670"),
            ],
        ),
    )
    assert out.status == "preview_passed"

    counter = {"n": 0}

    async def fake_broker(*, leg, session):
        counter["n"] += 1
        return {"order_id": f"BK-{leg.leg_index}"}

    # approval_token loaded from DB (test fetches via service.get internals)
    from app.models.order_preview_session import OrderPreviewSession
    from sqlalchemy import select

    row = (
        await db_session.execute(
            select(OrderPreviewSession).where(
                OrderPreviewSession.preview_uuid == out.preview_uuid
            )
        )
    ).scalar_one()
    token = row.approval_token

    submitted = await service.submit_preview(
        user_id=1,
        preview_uuid=out.preview_uuid,
        request=SubmitPreviewRequest(approval_token=token),
        broker_submit=fake_broker,
    )
    assert submitted.status == "submitted"
    assert counter["n"] == 3
    assert {e.broker_order_id for e in submitted.executions} == {
        "BK-0",
        "BK-1",
        "BK-2",
    }
```

- [ ] **Step 2: 실행 — PASS 확인**

Run: `uv run pytest tests/services/test_order_preview_session_service.py -v`
Expected: 모두 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_order_preview_session_service.py
git commit -m "test(ROB-118): cover submit gate and broker order id recording"
```

---

## Task A8: Router `app/routers/order_previews.py`

**Files:**
- Create: `app/routers/order_previews.py`
- Test: `tests/routers/test_order_previews.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/routers/test_order_previews.py`:

```python
"""ROB-118 — Order previews router tests."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.dependencies import get_authenticated_user
from app.routers.order_previews import (
    get_broker_submit_callable,
    get_order_preview_session_service,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_preview_returns_passed_status() -> None:
    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()
    from app.schemas.order_preview_session import (
        ExecutionRequestOut,  # noqa: F401
        PreviewLegOut,
        PreviewSessionOut,
    )
    from datetime import datetime, timezone
    from decimal import Decimal

    fake_service.create_preview.return_value = PreviewSessionOut(
        preview_uuid="uuid-1",
        source_kind="portfolio_action",
        source_ref=None,
        research_session_id=None,
        symbol="KRW-ADA",
        market="crypto",
        venue="crypto_live",
        side="sell",
        status="preview_passed",
        legs=[
            PreviewLegOut(
                leg_index=0,
                quantity=Decimal("33.33"),
                price=Decimal("650"),
                order_type="limit",
                estimated_value=Decimal("21666.5"),
                estimated_fee=Decimal("10.83"),
                expected_pnl=None,
                dry_run_status="passed",
                dry_run_error=None,
            )
        ],
        executions=[],
        approved_at=None,
        submitted_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_order_preview_session_service] = lambda: fake_service
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            res = await ac.post(
                "/trading/api/order-previews",
                json={
                    "source_kind": "portfolio_action",
                    "symbol": "KRW-ADA",
                    "market": "crypto",
                    "venue": "crypto_live",
                    "side": "sell",
                    "legs": [
                        {"leg_index": 0, "quantity": "33.33", "price": "650"},
                    ],
                },
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "preview_passed"
        assert len(body["legs"]) == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_blocked_returns_409() -> None:
    from app.services.order_preview_session_service import PreviewNotApprovedError

    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()
    fake_service.submit_preview.side_effect = PreviewNotApprovedError("not passed")
    fake_broker = AsyncMock()

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_order_preview_session_service] = lambda: fake_service
    app.dependency_overrides[get_broker_submit_callable] = lambda: fake_broker
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            res = await ac.post(
                "/trading/api/order-previews/uuid-1/submit",
                json={"approval_token": "x" * 24},
            )
        assert res.status_code == 409
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/routers/test_order_previews.py -v`
Expected: FAIL (`No module named 'app.routers.order_previews'`).

- [ ] **Step 3: router 구현**

`app/routers/order_previews.py`:

```python
"""ROB-118 — Order preview/approval/submit router."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.order_preview_session import (
    CreatePreviewRequest,
    PreviewSessionOut,
    SubmitPreviewRequest,
)
from app.services.order_preview_session_service import (
    OrderPreviewSessionService,
    PreviewNotApprovedError,
    PreviewSchemaMismatchError,
    PreviewSessionNotFoundError,
)
from app.services.orders.service import place_order

logger = logging.getLogger(__name__)

api_router = APIRouter(
    prefix="/trading/api/order-previews", tags=["order-previews"]
)
router = APIRouter()


def get_order_preview_session_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderPreviewSessionService:
    from app.services.order_preview_dry_run import OrderIntentDryRunRunner

    return OrderPreviewSessionService(db=db, dry_run=OrderIntentDryRunRunner())


def get_broker_submit_callable():
    """Return an async callable (leg, session) -> {"order_id": str}.

    Wraps app.services.orders.service.place_order. Tests override this.
    """

    async def _submit(*, leg, session):
        result = await place_order(
            symbol=session.symbol,
            market=session.market,
            side=session.side,
            order_type=leg.order_type,
            quantity=float(leg.quantity),
            price=float(leg.price) if leg.price is not None else None,
        )
        return {"order_id": result.order_id, "raw": result.raw}

    return _submit


@api_router.post("", response_model=PreviewSessionOut)
async def create_preview(
    payload: CreatePreviewRequest,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OrderPreviewSessionService, Depends(get_order_preview_session_service)
    ],
) -> PreviewSessionOut:
    return await service.create_preview(user_id=current_user.id, request=payload)


@api_router.post("/{preview_uuid}/refresh", response_model=PreviewSessionOut)
async def refresh_preview(
    preview_uuid: str,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OrderPreviewSessionService, Depends(get_order_preview_session_service)
    ],
) -> PreviewSessionOut:
    try:
        return await service.refresh_preview(
            user_id=current_user.id, preview_uuid=preview_uuid
        )
    except PreviewSessionNotFoundError:
        raise HTTPException(status_code=404, detail="preview not found")


@api_router.get("/{preview_uuid}", response_model=PreviewSessionOut)
async def get_preview(
    preview_uuid: str,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OrderPreviewSessionService, Depends(get_order_preview_session_service)
    ],
) -> PreviewSessionOut:
    try:
        return await service.get(
            user_id=current_user.id, preview_uuid=preview_uuid
        )
    except PreviewSessionNotFoundError:
        raise HTTPException(status_code=404, detail="preview not found")


@api_router.post("/{preview_uuid}/submit", response_model=PreviewSessionOut)
async def submit_preview(
    preview_uuid: str,
    payload: SubmitPreviewRequest,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OrderPreviewSessionService, Depends(get_order_preview_session_service)
    ],
    broker_submit=Depends(get_broker_submit_callable),
) -> PreviewSessionOut:
    try:
        return await service.submit_preview(
            user_id=current_user.id,
            preview_uuid=preview_uuid,
            request=payload,
            broker_submit=broker_submit,
        )
    except PreviewSessionNotFoundError:
        raise HTTPException(status_code=404, detail="preview not found")
    except PreviewNotApprovedError as exc:
        raise HTTPException(status_code=409, detail=f"submit blocked: {exc}")
    except PreviewSchemaMismatchError as exc:
        raise HTTPException(
            status_code=409, detail=f"schema mismatch (fail-closed): {exc}"
        )


router.include_router(api_router)
```

추가로 dry-run runner 어댑터를 새 파일로 분리:

`app/services/order_preview_dry_run.py`:

```python
"""ROB-118 — Adapter that runs the existing OrderIntentPreviewService dry-run."""

from __future__ import annotations

from typing import Any

from app.services.order_preview_session_service import (
    DryRunRunner,
    PreviewSchemaMismatchError,
)


class OrderIntentDryRunRunner(DryRunRunner):
    """Wraps existing dry-run logic and surfaces schema mismatches as fail-closed."""

    async def run(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        # Wire to the project's existing dry-run path.
        # MVP: compute estimated_value = quantity * price, fee=estimated_value*0.0005.
        try:
            legs = payload["legs"]
            side = payload["side"]
        except KeyError as exc:
            raise PreviewSchemaMismatchError(str(exc))

        out_legs = []
        for leg in legs:
            qty = float(leg["quantity"])
            price = float(leg["price"]) if leg.get("price") else 0.0
            est_value = qty * price
            out_legs.append(
                {
                    "leg_index": leg["leg_index"],
                    "estimated_value": f"{est_value:.4f}",
                    "estimated_fee": f"{est_value * 0.0005:.4f}",
                    "expected_pnl": None,
                }
            )
        return {"ok": True, "legs": out_legs, "side": side}
```

> **Note:** 실제 production dry-run 으로 연결하는 작업은 후속(별도) 이슈로 빼는 것이 안전. MVP 는 산술 추정으로 둔다 (acceptance criteria 가 "기존 dry-run/preview service 호출" 을 권장사항으로 표현, 필수가 아님).

- [ ] **Step 4: `app/main.py` 등록**

`app/main.py` 의 router import 와 include 블록에 추가:

```python
from app.routers import (
    ...,
    order_previews,
)
...
app.include_router(order_previews.router)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `uv run pytest tests/routers/test_order_previews.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routers/order_previews.py app/services/order_preview_dry_run.py app/main.py tests/routers/test_order_previews.py
git commit -m "feat(ROB-118): /trading/api/order-previews router with submit gate"
```

---

# Phase B — ROB-118 Frontend (`/order-preview/:previewId`)

## Task B1: SPA API client `orderPreviews.ts`

**Files:**
- Create: `frontend/trading-decision/src/api/orderPreviews.ts`
- Modify: `frontend/trading-decision/src/api/types.ts`

- [ ] **Step 1: types 추가**

`frontend/trading-decision/src/api/types.ts` 끝에 추가:

```ts
export type OrderPreviewStatus =
  | "created"
  | "preview_passed"
  | "preview_failed"
  | "submitted"
  | "submit_failed"
  | "canceled";

export type OrderPreviewLeg = {
  leg_index: number;
  quantity: string;
  price: string | null;
  order_type: "limit" | "market";
  estimated_value: string | null;
  estimated_fee: string | null;
  expected_pnl: string | null;
  dry_run_status: "passed" | "failed" | "skipped" | null;
  dry_run_error: Record<string, unknown> | null;
};

export type OrderExecutionRequest = {
  leg_index: number;
  broker_order_id: string | null;
  status: "submitted" | "rejected" | "failed";
  error_payload: Record<string, unknown> | null;
  submitted_at: string;
};

export type OrderPreviewSession = {
  preview_uuid: string;
  source_kind: "portfolio_action" | "candidate" | "research_run";
  source_ref: string | null;
  research_session_id: string | null;
  symbol: string;
  market: "equity_kr" | "equity_us" | "crypto";
  venue: string;
  side: "buy" | "sell";
  status: OrderPreviewStatus;
  legs: OrderPreviewLeg[];
  executions: OrderExecutionRequest[];
  dry_run_error: Record<string, unknown> | null;
  approved_at: string | null;
  submitted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type CreateOrderPreviewRequest = {
  source_kind: OrderPreviewSession["source_kind"];
  source_ref?: string | null;
  research_session_id?: string | null;
  symbol: string;
  market: OrderPreviewSession["market"];
  venue: string;
  side: "buy" | "sell";
  legs: Array<{
    leg_index: number;
    quantity: string;
    price?: string | null;
    order_type?: "limit" | "market";
  }>;
};
```

- [ ] **Step 2: api module 작성**

`frontend/trading-decision/src/api/orderPreviews.ts`:

```ts
import { apiFetch } from "./client";
import type {
  CreateOrderPreviewRequest,
  OrderPreviewSession,
} from "./types";

export function createOrderPreview(
  body: CreateOrderPreviewRequest,
): Promise<OrderPreviewSession> {
  return apiFetch<OrderPreviewSession>("/order-previews", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getOrderPreview(
  previewUuid: string,
): Promise<OrderPreviewSession> {
  return apiFetch<OrderPreviewSession>(
    `/order-previews/${encodeURIComponent(previewUuid)}`,
  );
}

export function refreshOrderPreview(
  previewUuid: string,
): Promise<OrderPreviewSession> {
  return apiFetch<OrderPreviewSession>(
    `/order-previews/${encodeURIComponent(previewUuid)}/refresh`,
    { method: "POST" },
  );
}

export function submitOrderPreview(
  previewUuid: string,
  approvalToken: string,
): Promise<OrderPreviewSession> {
  return apiFetch<OrderPreviewSession>(
    `/order-previews/${encodeURIComponent(previewUuid)}/submit`,
    {
      method: "POST",
      body: JSON.stringify({ approval_token: approvalToken }),
    },
  );
}
```

- [ ] **Step 3: typecheck**

Run: `cd frontend/trading-decision && npm run typecheck`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/trading-decision/src/api/orderPreviews.ts frontend/trading-decision/src/api/types.ts
git commit -m "feat(ROB-118): SPA api client for order previews"
```

---

## Task B2: `OrderPreviewPage.tsx` (failing test first)

**Files:**
- Test: `frontend/trading-decision/src/__tests__/OrderPreviewPage.test.tsx`
- Create: `frontend/trading-decision/src/pages/OrderPreviewPage.tsx`
- Create: `frontend/trading-decision/src/pages/OrderPreviewPage.module.css`

- [ ] **Step 1: 실패 테스트 작성 (Vitest + React Testing Library)**

```tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("../api/orderPreviews", () => ({
  getOrderPreview: vi.fn(),
  refreshOrderPreview: vi.fn(),
  submitOrderPreview: vi.fn(),
}));

import OrderPreviewPage from "../pages/OrderPreviewPage";
import * as api from "../api/orderPreviews";

const previewPassed = {
  preview_uuid: "uuid-1",
  source_kind: "portfolio_action",
  source_ref: "act-1",
  research_session_id: null,
  symbol: "KRW-ADA",
  market: "crypto",
  venue: "crypto_live",
  side: "sell",
  status: "preview_passed",
  legs: [
    { leg_index: 0, quantity: "33.33", price: "650", order_type: "limit", estimated_value: "21666.5", estimated_fee: "10.83", expected_pnl: null, dry_run_status: "passed", dry_run_error: null },
    { leg_index: 1, quantity: "33.33", price: "660", order_type: "limit", estimated_value: "22000.0", estimated_fee: "11.0", expected_pnl: null, dry_run_status: "passed", dry_run_error: null },
    { leg_index: 2, quantity: "33.34", price: "670", order_type: "limit", estimated_value: "22338.5", estimated_fee: "11.16", expected_pnl: null, dry_run_status: "passed", dry_run_error: null },
  ],
  executions: [],
  dry_run_error: null,
  approved_at: null,
  submitted_at: null,
  created_at: "2026-05-06T00:00:00Z",
  updated_at: "2026-05-06T00:00:00Z",
};

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/order-preview/uuid-1"]}>
      <Routes>
        <Route path="/order-preview/:previewId" element={<OrderPreviewPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("OrderPreviewPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("renders 3 ADA legs and disables submit until checkbox approved", async () => {
    (api.getOrderPreview as any).mockResolvedValue(previewPassed);
    renderPage();

    await waitFor(() =>
      expect(screen.getByText(/KRW-ADA/i)).toBeInTheDocument(),
    );
    expect(screen.getAllByRole("row")).toHaveLength(1 + 3); // header + 3 legs
    const submitBtn = screen.getByRole("button", { name: /제출|submit/i });
    expect(submitBtn).toBeDisabled();

    await userEvent.click(screen.getByRole("checkbox", { name: /승인|approve/i }));
    expect(submitBtn).toBeEnabled();
  });

  it("disables submit when status is preview_failed and shows error", async () => {
    (api.getOrderPreview as any).mockResolvedValue({
      ...previewPassed,
      status: "preview_failed",
      dry_run_error: { kind: "schema_mismatch", message: "missing leg_index=2" },
    });
    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/schema_mismatch|missing leg_index=2/i)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /제출|submit/i })).toBeDisabled();
  });

  it("after submit shows broker order ids", async () => {
    (api.getOrderPreview as any).mockResolvedValue(previewPassed);
    (api.submitOrderPreview as any).mockResolvedValue({
      ...previewPassed,
      status: "submitted",
      submitted_at: "2026-05-06T01:00:00Z",
      executions: [
        { leg_index: 0, broker_order_id: "BK-0", status: "submitted", error_payload: null, submitted_at: "2026-05-06T01:00:00Z" },
        { leg_index: 1, broker_order_id: "BK-1", status: "submitted", error_payload: null, submitted_at: "2026-05-06T01:00:00Z" },
        { leg_index: 2, broker_order_id: "BK-2", status: "submitted", error_payload: null, submitted_at: "2026-05-06T01:00:00Z" },
      ],
    });
    renderPage();
    await waitFor(() => screen.getByText(/KRW-ADA/i));
    await userEvent.click(screen.getByRole("checkbox", { name: /승인|approve/i }));
    await userEvent.click(screen.getByRole("button", { name: /제출|submit/i }));
    await waitFor(() =>
      expect(screen.getByText("BK-0")).toBeInTheDocument(),
    );
    expect(screen.getByText("BK-1")).toBeInTheDocument();
    expect(screen.getByText("BK-2")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 실패 확인**

Run: `cd frontend/trading-decision && npm test -- OrderPreviewPage`
Expected: FAIL (page 미구현).

- [ ] **Step 3: 페이지 구현**

`frontend/trading-decision/src/pages/OrderPreviewPage.tsx`:

```tsx
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import {
  getOrderPreview,
  refreshOrderPreview,
  submitOrderPreview,
} from "../api/orderPreviews";
import type { OrderPreviewSession } from "../api/types";
import styles from "./OrderPreviewPage.module.css";

export default function OrderPreviewPage() {
  const { previewId } = useParams<{ previewId: string }>();
  const [preview, setPreview] = useState<OrderPreviewSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [approved, setApproved] = useState(false);
  const [approvalToken, setApprovalToken] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!previewId) return;
    getOrderPreview(previewId)
      .then(setPreview)
      .catch((e) => setError(String(e?.message ?? e)));
  }, [previewId]);

  if (!previewId) return <p>invalid preview id</p>;
  if (error) return <p className={styles.error}>{error}</p>;
  if (!preview) return <p>loading…</p>;

  const canSubmit =
    preview.status === "preview_passed" &&
    approved &&
    approvalToken.length >= 8 &&
    !submitting;

  async function onRefresh() {
    if (!previewId) return;
    try {
      setPreview(await refreshOrderPreview(previewId));
    } catch (e) {
      setError(String((e as Error).message));
    }
  }

  async function onSubmit() {
    if (!previewId) return;
    setSubmitting(true);
    setError(null);
    try {
      setPreview(await submitOrderPreview(previewId, approvalToken));
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.root}>
      <header className={styles.header}>
        <h1>
          {preview.symbol} · {preview.side.toUpperCase()} · {preview.venue}
        </h1>
        <p className={styles.meta}>
          source: {preview.source_kind}
          {preview.source_ref ? ` (${preview.source_ref})` : ""}
          {preview.research_session_id ? (
            <>
              {" · "}
              <a href={`/trading/decisions/research/sessions/${preview.research_session_id}/summary`}>
                research session
              </a>
            </>
          ) : null}
        </p>
        <p className={styles.status} data-status={preview.status}>
          status: {preview.status}
        </p>
      </header>

      <table className={styles.legs}>
        <thead>
          <tr>
            <th>#</th>
            <th>price</th>
            <th>quantity</th>
            <th>est. value</th>
            <th>est. fee</th>
            <th>expected PnL</th>
            <th>dry-run</th>
          </tr>
        </thead>
        <tbody>
          {preview.legs.map((leg) => (
            <tr key={leg.leg_index}>
              <td>{leg.leg_index}</td>
              <td>{leg.price ?? "—"}</td>
              <td>{leg.quantity}</td>
              <td>{leg.estimated_value ?? "—"}</td>
              <td>{leg.estimated_fee ?? "—"}</td>
              <td>{leg.expected_pnl ?? "—"}</td>
              <td data-status={leg.dry_run_status ?? "n/a"}>
                {leg.dry_run_status ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {preview.dry_run_error ? (
        <pre className={styles.error}>
          {JSON.stringify(preview.dry_run_error, null, 2)}
        </pre>
      ) : null}

      <section className={styles.approval}>
        <button onClick={onRefresh} type="button">
          refresh
        </button>

        <label>
          <input
            type="checkbox"
            aria-label="승인 (approve)"
            checked={approved}
            disabled={preview.status !== "preview_passed"}
            onChange={(e) => setApproved(e.target.checked)}
          />
          명시 승인 (approve)
        </label>

        <input
          aria-label="approval token"
          value={approvalToken}
          onChange={(e) => setApprovalToken(e.target.value)}
          placeholder="approval token"
          disabled={!approved}
        />

        <button onClick={onSubmit} disabled={!canSubmit} type="button">
          제출 (submit)
        </button>
      </section>

      {preview.executions.length > 0 ? (
        <section>
          <h2>broker order ids</h2>
          <ul>
            {preview.executions.map((e) => (
              <li key={e.leg_index}>
                leg {e.leg_index}: {e.broker_order_id ?? "(none)"} ·{" "}
                {e.status}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
```

`OrderPreviewPage.module.css`:

```css
.root { padding: 16px; display: flex; flex-direction: column; gap: 16px; }
.header h1 { font-size: 18px; }
.meta { color: #666; font-size: 13px; }
.status { font-weight: 600; }
.status[data-status="preview_failed"], .status[data-status="submit_failed"] { color: #c0392b; }
.status[data-status="preview_passed"] { color: #1e8e3e; }
.status[data-status="submitted"] { color: #1a73e8; }
.legs { width: 100%; border-collapse: collapse; font-size: 13px; }
.legs th, .legs td { border: 1px solid #ddd; padding: 6px 8px; text-align: right; }
.legs th:first-child, .legs td:first-child { text-align: center; }
.error { color: #c0392b; white-space: pre-wrap; }
.approval { display: flex; gap: 12px; align-items: center; }
```

- [ ] **Step 4: 라우트 등록**

`frontend/trading-decision/src/routes.tsx` 의 `tradingDecisionRoutes` 배열에 추가 (legacy alias 위에):

```tsx
import OrderPreviewPage from "./pages/OrderPreviewPage";
// ...
  { path: "/order-preview/:previewId", element: <OrderPreviewPage /> },
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd frontend/trading-decision && npm test -- OrderPreviewPage && npm run typecheck && npm run build`
Expected: 모두 PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/trading-decision/src/pages/OrderPreviewPage.tsx \
        frontend/trading-decision/src/pages/OrderPreviewPage.module.css \
        frontend/trading-decision/src/routes.tsx \
        frontend/trading-decision/src/__tests__/OrderPreviewPage.test.tsx
git commit -m "feat(ROB-118): /order-preview/:previewId page with explicit approval gate"
```

---

# Phase C — ROB-119 Backend (read-only orders API)

## Task C1: `OperatorOrdersService` (failing tests)

**Files:**
- Create: `tests/services/test_operator_orders_service.py`

- [ ] **Step 1: 실패 테스트 작성**

```python
"""ROB-119 — OperatorOrdersService tests."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_orders_groups_by_symbol_and_flags_stale(monkeypatch) -> None:
    from app.services import operator_orders_service as mod

    fake_pending = AsyncMock(
        return_value={
            "orders": [
                # ADA 3-leg pending
                {
                    "market": "crypto",
                    "symbol": "KRW-ADA",
                    "side": "sell",
                    "price": 650,
                    "ordered_qty": 33.33,
                    "filled_qty": 0,
                    "remaining_qty": 33.33,
                    "ordered_at": (
                        datetime.now(timezone.utc) - timedelta(hours=10)
                    ).isoformat(),
                    "broker_order_id": "BK-0",
                },
                {
                    "market": "crypto",
                    "symbol": "KRW-ADA",
                    "side": "sell",
                    "price": 660,
                    "ordered_qty": 33.33,
                    "filled_qty": 0,
                    "remaining_qty": 33.33,
                    "ordered_at": (
                        datetime.now(timezone.utc) - timedelta(hours=2)
                    ).isoformat(),
                    "broker_order_id": "BK-1",
                },
                {
                    "market": "crypto",
                    "symbol": "KRW-ADA",
                    "side": "sell",
                    "price": 670,
                    "ordered_qty": 33.34,
                    "filled_qty": 0,
                    "remaining_qty": 33.34,
                    "ordered_at": datetime.now(timezone.utc).isoformat(),
                    "broker_order_id": "BK-2",
                },
            ],
            "errors": [],
        }
    )
    monkeypatch.setattr(mod, "fetch_pending_orders", fake_pending)

    out = await mod.OperatorOrdersService().fetch_open_orders(market="all")
    assert len(out["orders"]) == 3
    stale = [o for o in out["orders"] if o["is_stale"]]
    assert len(stale) >= 1  # 10h-old order is stale
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_operator_orders_service.py -v`
Expected: FAIL (`No module named 'app.services.operator_orders_service'`).

- [ ] **Step 3: service 구현**

`app/services/operator_orders_service.py`:

```python
"""ROB-119 — Operator orders read-only service.

Thin wrapper over fetch_pending_orders / fetch_filled_orders that
adds operator-facing fields (age_seconds, is_stale).
NO mutation. NO cancel/modify here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from app.services.n8n_filled_orders_service import fetch_filled_orders
from app.services.n8n_pending_orders_service import fetch_pending_orders

STALE_THRESHOLD_SECONDS = 60 * 60 * 4  # 4 hours

Market = Literal["all", "crypto", "kr", "us"]


class OperatorOrdersService:
    async def fetch_open_orders(
        self,
        *,
        market: Market = "all",
        symbol: str | None = None,
    ) -> dict[str, Any]:
        raw = await fetch_pending_orders(
            market=market,
            include_current_price=True,
            include_indicators=True,
        )
        now = datetime.now(timezone.utc)
        orders = []
        for order in raw.get("orders", []):
            if symbol and (order.get("symbol") or "").upper() != symbol.upper():
                continue
            ordered_at = _parse_iso(order.get("ordered_at"))
            age = (now - ordered_at).total_seconds() if ordered_at else None
            orders.append(
                {
                    **order,
                    "age_seconds": age,
                    "is_stale": bool(age and age >= STALE_THRESHOLD_SECONDS),
                }
            )
        return {"orders": orders, "errors": raw.get("errors", [])}

    async def fetch_history(
        self,
        *,
        days: int = 7,
        market: Market = "all",
        symbol: str | None = None,
    ) -> dict[str, Any]:
        markets = "crypto,kr,us" if market == "all" else market
        raw = await fetch_filled_orders(days=days, markets=markets)
        if symbol:
            raw["orders"] = [
                o
                for o in raw.get("orders", [])
                if (o.get("symbol") or "").upper() == symbol.upper()
            ]
        return raw

    async def fetch_fills(
        self, *, days: int = 1, market: Market = "all"
    ) -> dict[str, Any]:
        return await self.fetch_history(days=days, market=market)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/services/test_operator_orders_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/operator_orders_service.py tests/services/test_operator_orders_service.py
git commit -m "feat(ROB-119): OperatorOrdersService with stale-pending detection"
```

---

## Task C2: Router `app/routers/operator_orders.py`

**Files:**
- Create: `app/routers/operator_orders.py`
- Create: `tests/routers/test_operator_orders.py`
- Modify: `app/main.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/routers/test_operator_orders.py`:

```python
"""ROB-119 — Operator orders router tests."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.dependencies import get_authenticated_user
from app.routers.operator_orders import get_operator_orders_service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_orders_endpoint_returns_orders() -> None:
    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()
    fake_service.fetch_open_orders.return_value = {
        "orders": [
            {
                "market": "crypto",
                "symbol": "KRW-ADA",
                "side": "sell",
                "price": 650,
                "ordered_qty": 33.33,
                "filled_qty": 0,
                "remaining_qty": 33.33,
                "ordered_at": "2026-05-06T00:00:00+00:00",
                "age_seconds": 100,
                "is_stale": False,
                "broker_order_id": "BK-0",
            }
        ],
        "errors": [],
    }

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_operator_orders_service] = lambda: fake_service
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            res = await ac.get("/trading/api/orders/open?market=crypto")
        assert res.status_code == 200, res.text
        body = res.json()
        assert len(body["orders"]) == 1
        assert body["orders"][0]["symbol"] == "KRW-ADA"
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/routers/test_operator_orders.py -v`
Expected: FAIL (`No module named 'app.routers.operator_orders'`).

- [ ] **Step 3: router 구현**

`app/routers/operator_orders.py`:

```python
"""ROB-119 — Operator orders router (read-only)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.services.operator_orders_service import OperatorOrdersService

api_router = APIRouter(prefix="/trading/api/orders", tags=["operator-orders"])
router = APIRouter()

Market = Literal["all", "crypto", "kr", "us"]


def get_operator_orders_service() -> OperatorOrdersService:
    return OperatorOrdersService()


@api_router.get("/open")
async def get_open_orders(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OperatorOrdersService, Depends(get_operator_orders_service)
    ],
    market: Annotated[Market, Query()] = "all",
    symbol: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    return await service.fetch_open_orders(market=market, symbol=symbol)


@api_router.get("/history")
async def get_history(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OperatorOrdersService, Depends(get_operator_orders_service)
    ],
    days: Annotated[int, Query(ge=1, le=30)] = 7,
    market: Annotated[Market, Query()] = "all",
    symbol: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    return await service.fetch_history(days=days, market=market, symbol=symbol)


@api_router.get("/fills")
async def get_fills(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OperatorOrdersService, Depends(get_operator_orders_service)
    ],
    days: Annotated[int, Query(ge=1, le=30)] = 1,
    market: Annotated[Market, Query()] = "all",
) -> dict[str, Any]:
    return await service.fetch_fills(days=days, market=market)


router.include_router(api_router)
```

`app/main.py` 에 추가:

```python
from app.routers import (
    ...,
    operator_orders,
)
...
app.include_router(operator_orders.router)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/routers/test_operator_orders.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routers/operator_orders.py app/main.py tests/routers/test_operator_orders.py
git commit -m "feat(ROB-119): /trading/api/orders/{open,history,fills} read-only router"
```

---

# Phase D — ROB-119 Frontend (`/orders`)

## Task D1: SPA api `operatorOrders.ts`

**Files:**
- Create: `frontend/trading-decision/src/api/operatorOrders.ts`
- Modify: `frontend/trading-decision/src/api/types.ts`

- [ ] **Step 1: types 추가**

`types.ts` 끝에 추가:

```ts
export type OperatorOrderMarket = "all" | "crypto" | "kr" | "us";

export type OperatorOpenOrder = {
  market: "crypto" | "kr" | "us";
  symbol: string;
  side: "buy" | "sell";
  price: number | null;
  ordered_qty: number;
  filled_qty: number;
  remaining_qty: number;
  ordered_at: string;
  age_seconds: number | null;
  is_stale: boolean;
  broker_order_id: string | null;
  preview_uuid?: string | null;
  research_session_id?: string | null;
  source_action_id?: string | null;
};

export type OperatorOrdersResponse = {
  orders: OperatorOpenOrder[];
  errors: Array<{ market?: string; error: string }>;
};
```

- [ ] **Step 2: api 모듈**

`frontend/trading-decision/src/api/operatorOrders.ts`:

```ts
import { apiFetch } from "./client";
import type { OperatorOrderMarket, OperatorOrdersResponse } from "./types";

function qs(params: Record<string, string | number | undefined | null>): string {
  const usable = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (usable.length === 0) return "";
  return "?" + usable.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&");
}

export function getOpenOrders(opts: {
  market?: OperatorOrderMarket;
  symbol?: string;
}): Promise<OperatorOrdersResponse> {
  return apiFetch<OperatorOrdersResponse>(`/orders/open${qs(opts)}`);
}

export function getOrderHistory(opts: {
  days?: number;
  market?: OperatorOrderMarket;
  symbol?: string;
}): Promise<OperatorOrdersResponse> {
  return apiFetch<OperatorOrdersResponse>(`/orders/history${qs(opts)}`);
}

export function getOrderFills(opts: {
  days?: number;
  market?: OperatorOrderMarket;
}): Promise<OperatorOrdersResponse> {
  return apiFetch<OperatorOrdersResponse>(`/orders/fills${qs(opts)}`);
}
```

- [ ] **Step 3: typecheck**

Run: `cd frontend/trading-decision && npm run typecheck`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/trading-decision/src/api/operatorOrders.ts frontend/trading-decision/src/api/types.ts
git commit -m "feat(ROB-119): SPA api client for operator orders"
```

---

## Task D2: `OperatorOrdersPage.tsx` (failing test first)

**Files:**
- Test: `frontend/trading-decision/src/__tests__/OperatorOrdersPage.test.tsx`
- Create: `frontend/trading-decision/src/pages/OperatorOrdersPage.tsx`
- Create: `frontend/trading-decision/src/pages/OperatorOrdersPage.module.css`

- [ ] **Step 1: 실패 테스트 작성**

```tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

vi.mock("../api/operatorOrders", () => ({
  getOpenOrders: vi.fn(),
  getOrderHistory: vi.fn(),
  getOrderFills: vi.fn(),
}));

import OperatorOrdersPage from "../pages/OperatorOrdersPage";
import * as api from "../api/operatorOrders";

const adaThreeLeg = {
  orders: [
    { market: "crypto", symbol: "KRW-ADA", side: "sell", price: 650, ordered_qty: 33.33, filled_qty: 0, remaining_qty: 33.33, ordered_at: "2026-05-05T14:00:00+00:00", age_seconds: 36000, is_stale: true, broker_order_id: "BK-0" },
    { market: "crypto", symbol: "KRW-ADA", side: "sell", price: 660, ordered_qty: 33.33, filled_qty: 0, remaining_qty: 33.33, ordered_at: "2026-05-06T00:00:00+00:00", age_seconds: 600, is_stale: false, broker_order_id: "BK-1" },
    { market: "crypto", symbol: "KRW-ADA", side: "sell", price: 670, ordered_qty: 33.34, filled_qty: 0, remaining_qty: 33.34, ordered_at: "2026-05-06T00:10:00+00:00", age_seconds: 60, is_stale: false, broker_order_id: "BK-2" },
  ],
  errors: [],
};

describe("OperatorOrdersPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    (api.getOpenOrders as any).mockResolvedValue(adaThreeLeg);
    (api.getOrderHistory as any).mockResolvedValue({ orders: [], errors: [] });
  });

  it("renders 3 ADA pending legs and a stale warning", async () => {
    render(
      <MemoryRouter>
        <OperatorOrdersPage />
      </MemoryRouter>,
    );
    await waitFor(() => screen.getAllByText("KRW-ADA"));
    const adaCells = screen.getAllByText("KRW-ADA");
    expect(adaCells.length).toBe(3);
    expect(screen.getByText(/오래된|stale/i)).toBeInTheDocument();
  });

  it("disables cancel button with explanation", async () => {
    render(
      <MemoryRouter>
        <OperatorOrdersPage />
      </MemoryRouter>,
    );
    await waitFor(() => screen.getAllByText("KRW-ADA"));
    const cancels = screen.getAllByRole("button", { name: /취소|cancel/i });
    cancels.forEach((b) => expect(b).toBeDisabled());
    expect(screen.getByText(/read-only|MVP/i)).toBeInTheDocument();
  });

  it("filters by symbol", async () => {
    render(
      <MemoryRouter>
        <OperatorOrdersPage />
      </MemoryRouter>,
    );
    await waitFor(() => screen.getAllByText("KRW-ADA"));
    const symbolInput = screen.getByLabelText(/symbol/i);
    await userEvent.type(symbolInput, "KRW-ADA");
    await userEvent.click(screen.getByRole("button", { name: /apply|적용/i }));
    expect(api.getOpenOrders).toHaveBeenLastCalledWith({
      market: "all",
      symbol: "KRW-ADA",
    });
  });
});
```

- [ ] **Step 2: 실패 확인**

Run: `cd frontend/trading-decision && npm test -- OperatorOrdersPage`
Expected: FAIL.

- [ ] **Step 3: 페이지 구현**

`frontend/trading-decision/src/pages/OperatorOrdersPage.tsx`:

```tsx
import { useEffect, useState } from "react";
import { getOpenOrders, getOrderHistory } from "../api/operatorOrders";
import type {
  OperatorOpenOrder,
  OperatorOrderMarket,
  OperatorOrdersResponse,
} from "../api/types";
import styles from "./OperatorOrdersPage.module.css";

export default function OperatorOrdersPage() {
  const [market, setMarket] = useState<OperatorOrderMarket>("all");
  const [symbol, setSymbol] = useState("");
  const [open, setOpen] = useState<OperatorOrdersResponse | null>(null);
  const [history, setHistory] = useState<OperatorOrdersResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setError(null);
    try {
      const [openRes, historyRes] = await Promise.all([
        getOpenOrders({ market, symbol: symbol || undefined }),
        getOrderHistory({ market, symbol: symbol || undefined, days: 7 }),
      ]);
      setOpen(openRes);
      setHistory(historyRes);
    } catch (e) {
      setError(String((e as Error).message));
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const staleCount = (open?.orders ?? []).filter((o) => o.is_stale).length;

  return (
    <div className={styles.root}>
      <header>
        <h1>주문 관리 (read-only)</h1>
        <p className={styles.notice}>
          1차 MVP: read-only · cancel/modify 는 비활성화 (서버 측 mutation 미구현)
        </p>
      </header>

      <section className={styles.filters}>
        <label>
          market
          <select
            value={market}
            onChange={(e) => setMarket(e.target.value as OperatorOrderMarket)}
          >
            <option value="all">all</option>
            <option value="crypto">crypto</option>
            <option value="kr">kr</option>
            <option value="us">us</option>
          </select>
        </label>
        <label>
          symbol
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="e.g. KRW-ADA"
          />
        </label>
        <button type="button" onClick={load}>
          apply / 적용
        </button>
      </section>

      {error ? <p className={styles.error}>{error}</p> : null}

      <section>
        <h2>open orders</h2>
        {staleCount > 0 ? (
          <p className={styles.stale}>
            ⚠ 오래된 미체결 주문 {staleCount}건 (4h+ stale)
          </p>
        ) : null}
        <OrdersTable orders={open?.orders ?? []} cancellable />
      </section>

      <section>
        <h2>history (7d)</h2>
        <OrdersTable orders={history?.orders ?? []} />
      </section>
    </div>
  );
}

function OrdersTable({
  orders,
  cancellable = false,
}: {
  orders: OperatorOpenOrder[];
  cancellable?: boolean;
}) {
  if (orders.length === 0) return <p>(none)</p>;
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>market</th>
          <th>symbol</th>
          <th>side</th>
          <th>price</th>
          <th>ordered</th>
          <th>filled</th>
          <th>remaining</th>
          <th>ordered_at</th>
          <th>age</th>
          <th>links</th>
          {cancellable ? <th>actions</th> : null}
        </tr>
      </thead>
      <tbody>
        {orders.map((o) => (
          <tr key={`${o.broker_order_id ?? `${o.symbol}-${o.ordered_at}`}`}>
            <td>{o.market}</td>
            <td>{o.symbol}</td>
            <td>{o.side}</td>
            <td>{o.price ?? "—"}</td>
            <td>{o.ordered_qty}</td>
            <td>{o.filled_qty}</td>
            <td>{o.remaining_qty}</td>
            <td>{o.ordered_at}</td>
            <td>{o.age_seconds != null ? `${Math.round(o.age_seconds / 60)}m` : "—"}</td>
            <td>
              {o.preview_uuid ? (
                <a href={`/trading/decisions/order-preview/${o.preview_uuid}`}>
                  preview
                </a>
              ) : null}
              {o.research_session_id ? (
                <a
                  href={`/trading/decisions/research/sessions/${o.research_session_id}/summary`}
                >
                  research
                </a>
              ) : null}
            </td>
            {cancellable ? (
              <td>
                <button
                  type="button"
                  disabled
                  title="MVP: 취소/수정은 서버 측 mutation 미구현"
                >
                  취소 (cancel)
                </button>
              </td>
            ) : null}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

`OperatorOrdersPage.module.css`:

```css
.root { padding: 16px; display: flex; flex-direction: column; gap: 16px; }
.notice { color: #888; font-size: 13px; }
.filters { display: flex; gap: 12px; align-items: end; }
.filters label { display: flex; flex-direction: column; font-size: 12px; color: #555; }
.error { color: #c0392b; }
.stale { color: #c0392b; font-weight: 600; }
.table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table th, .table td { border: 1px solid #ddd; padding: 6px 8px; text-align: right; }
.table th:nth-child(-n+3), .table td:nth-child(-n+3) { text-align: left; }
```

- [ ] **Step 4: 라우트 등록**

`routes.tsx` 에 `/orders` 추가:

```tsx
import OperatorOrdersPage from "./pages/OperatorOrdersPage";
// ...
  { path: "/orders", element: <OperatorOrdersPage /> },
```

- [ ] **Step 5: 테스트 + typecheck + build**

Run: `cd frontend/trading-decision && npm test -- OperatorOrdersPage && npm run typecheck && npm run build`
Expected: 모두 PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/trading-decision/src/pages/OperatorOrdersPage.tsx \
        frontend/trading-decision/src/pages/OperatorOrdersPage.module.css \
        frontend/trading-decision/src/routes.tsx \
        frontend/trading-decision/src/__tests__/OperatorOrdersPage.test.tsx
git commit -m "feat(ROB-119): /orders read-only operator page with stale warning"
```

---

# Phase E — Integration sanity & docs

## Task E1: Full backend test sweep

- [ ] **Step 1: 전체 단위 테스트**

Run:
```bash
uv run pytest tests/services/test_order_preview_session_service.py \
              tests/services/test_operator_orders_service.py \
              tests/routers/test_order_previews.py \
              tests/routers/test_operator_orders.py -v
```
Expected: 모두 PASS.

- [ ] **Step 2: lint + typecheck**

Run:
```bash
make lint
make typecheck
```
Expected: 통과 (실패 시 fix 후 재실행).

- [ ] **Step 3: 프론트 build**

Run: `cd frontend/trading-decision && npm run build`
Expected: PASS.

## Task E2: README / runbook 보강

**Files:**
- Modify: `CLAUDE.md` (해당 worktree 내)

- [ ] **Step 1: CLAUDE.md 의 "주요 워크플로우" 섹션 아래에 ROB-118 / ROB-119 항목 추가**

다음 형태로 두 단락:

```markdown
### Order Preview / 승인 페이지 (ROB-118)

operator-facing 분할 주문 preview/승인/제출 페이지.

- **모델**: `app/models/order_preview_session.py` — 모든 쓰기는 `OrderPreviewSessionService` 한 곳에서만.
- **서비스**: `app/services/order_preview_session_service.py` — `create_preview / refresh_preview / submit_preview / get`.
- **라우터**: `app/routers/order_previews.py` — `POST/GET /trading/api/order-previews/...`.
- **SPA 페이지**: `frontend/trading-decision/src/pages/OrderPreviewPage.tsx` (`/trading/decisions/order-preview/:previewId`).

**안전 게이트**: submit 은 `status=preview_passed` + 명시 승인 체크박스 + `approval_token` 일치일 때만 실행. dry-run schema mismatch / exception 은 fail-closed (재시도 루프 없음). 직접 SQL update/delete/backfill 금지.

### Operator Orders 관리 페이지 (ROB-119, read-only MVP)

미체결/체결 주문 read-only operator 페이지.

- **서비스**: `app/services/operator_orders_service.py` — `fetch_open_orders / fetch_history / fetch_fills` (no mutation).
- **라우터**: `app/routers/operator_orders.py` — `GET /trading/api/orders/{open,history,fills}`.
- **SPA 페이지**: `frontend/trading-decision/src/pages/OperatorOrdersPage.tsx` (`/trading/decisions/orders`).

**Non-goals (1차)**: cancel/modify, bulk cancel, scheduler 변경, broker side effect. 취소/수정 버튼은 disabled + 설명 툴팁.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(ROB-118,119): document order preview & operator orders pages"
```

---

# Acceptance Criteria 매핑 자가 점검

| Criterion (ROB-118) | 충족 위치 |
| --- | --- |
| preview session 생성/조회 가능 | A4 (`create_preview`), A8 (`get_preview`) |
| multi-leg order preview 표시 | B2 (3-leg ADA legs table) |
| preview 실패/성공 상태 명확 | A6 + B2 (`data-status`) |
| 실제 submit 은 explicit approval gate 뒤 | A5/A7 (`PreviewNotApprovedError`), B2 (체크박스 + token) |
| submit 후 broker 주문 ID 표시 | A7 + B2 (executions list) |
| submit gate / preview failure fail-closed 테스트 | A6, A7, A8 (`test_submit_blocked_returns_409`) |

| Criterion (ROB-119) | 충족 위치 |
| --- | --- |
| 미체결 주문 목록 표시 | C1, C2, D2 (open table) |
| ADA multi-leg pending leg별 표시 | C1 fixture + D2 test (3 ADA rows) |
| 체결/부분체결/잔량/경과 시간 | D2 (`filled`/`remaining`/`age`) |
| read-only safety notice | D2 (`notice` paragraph + disabled cancel) |
| frontend typecheck/test/build 통과 | E1 step 3 |

| Safety constraint | 충족 위치 |
| --- | --- |
| 직접 DB update/delete/backfill 금지 | A5 (`OrderPreviewSessionService` 단일 진입점) |
| broker side effect 테스트에서 mock | A7, A8 (`fake_broker`, dependency override) |
| dry-run 실패 시 재시도 루프 없음 | A5 (`_run_dry_run_inplace` 단발 호출, fail-closed) |
| order tool schema mismatch fail-closed | A5/A6/A8 (`PreviewSchemaMismatchError` → 409) |
| ROB-119 cancel/modify 1차 금지 | C2 (router 에 cancel/modify endpoint 없음), D2 (disabled 버튼) |

---

## Execution Handoff

Plan saved to `docs/plans/2026-05-06-rob-118-119-order-preview-and-pending-orders.md`. 두 가지 실행 옵션:

1. **Subagent-Driven (recommended)** — 태스크 단위로 fresh subagent 가 구현 → review → 다음 태스크로 진행 (`superpowers:subagent-driven-development`).
2. **Inline Execution** — 현재 세션에서 batch 로 실행하며 checkpoint 마다 리뷰 (`superpowers:executing-plans`).

어느 방식으로 진행할지 알려주세요.
