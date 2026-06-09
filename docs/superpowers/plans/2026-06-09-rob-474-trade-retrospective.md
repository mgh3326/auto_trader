# ROB-474 매매 회고 구조적 저장·집계 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매매 회고를 journal-side typed 테이블 `review.trade_retrospectives`에 구조적으로 저장하고, 근거(strategy_key)별·시간(KST window)별 집계 read 경로를 제공하며, `save_trade_journal`의 mock 계정 차단을 해제한다.

**Architecture:** 신규 review 스키마 테이블 1개 + `TradeRetrospectiveService`(repository 포함, 유일 쓰기 경로) + 3개 MCP 도구(write `save_trade_retrospective`, read `get_trade_retrospectives`/`get_retrospective_aggregate`). 체결 outcome은 journal-side에만 두어 `investment_report_items` 불변식("execution state는 report item에 두지 않음")을 보존. 기존 `get_mock_loop_retrospective`(KST-day×watch-loop×%)와 비중복(strategy×free-window×절대 PnL).

**Tech Stack:** Python 3.13, SQLAlchemy async, FastMCP, alembic, pytest(+asyncio, integration marker), Postgres(review schema). Spec: `docs/superpowers/specs/2026-06-09-rob-474-trade-retrospective-design.md`.

**Conventions locked from grounding:**
- review 모델: `BigInteger` PK, `Text`(String 아님), `Numeric(20,4)` 금액, `Numeric(8,4)` 퍼센트, `Enum(InstrumentType, name="instrument_type", create_type=False)`, `func.now()` server_default, CHECK `ck_<table>_<col>`, Index `ix_<table>_<cols>`, Unique `uq_<table>_<col>`, `{"schema": "review"}` 마지막.
- MCP envelope: write 성공 `{"success": True, "action": "created"|"updated", "data": {...}}`, read 성공 `{"success": True, "entries"/"groups": [...], ...}`, 실패 `{"success": False, "error": "<msg>"}`, 최상위 `try/except` → `logger.exception("<tool> failed")`.
- 테스트 DB: `db_session` 픽스처가 `Base.metadata.create_all` 실행 → review.py에 모델 추가하면 자동 생성(alembic 불필요, 마이그레이션은 operator cutover 용). review-table 테스트는 `pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("investment_reports_cleanup_lock")]` + autouse cleanup 픽스처 필수(xdist TRUNCATE deadlock 회피).
- 현재 alembic head: `20260609_rob455` (impl 시 `uv run alembic heads`로 재확인, main 전진 시 down_revision rebase).
- 커밋 trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- **Modify** `app/mcp_server/tooling/trade_journal_tools.py` — mock 계정 허용(`:148`, `:150`, `:248` + docstring)
- **Modify** `app/mcp_server/tooling/trade_journal_registration.py` — 설명에 mock 추가
- **Modify** `app/models/review.py` — `TradeRetrospective` 모델 추가
- **Create** `alembic/versions/20260609_rob474_trade_retrospectives.py` — 테이블 1개 생성
- **Create** `app/services/trade_journal/trade_retrospective_service.py` — repository + save + read/aggregate + serializer/helpers
- **Create** `app/mcp_server/tooling/trade_retrospective_tools.py` — 3개 도구 함수
- **Create** `app/mcp_server/tooling/trade_retrospective_registration.py` — TOOL_NAMES + register fn
- **Modify** `app/mcp_server/tooling/registry.py` — import + register 호출
- **Modify** `app/mcp_server/__init__.py` — `AVAILABLE_TOOL_NAMES`에 3개 추가
- **Create** `tests/test_trade_journal_mock_unblock.py`, `tests/test_trade_retrospective_model.py`, `tests/test_trade_retrospective_service.py`, `tests/test_trade_retrospective_aggregate.py`, `tests/test_trade_retrospective_tools.py`

---

## Task 1: `save_trade_journal` mock 계정 개방 (migration 0, 독립)

**Files:**
- Modify: `app/mcp_server/tooling/trade_journal_tools.py:147-159, 248, 130, 257`
- Modify: `app/mcp_server/tooling/trade_journal_registration.py` (mock 언급)
- Test: `tests/test_trade_journal_mock_unblock.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trade_journal_mock_unblock.py
"""ROB-474 — save_trade_journal must accept account_type='mock'."""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.trade_journal_tools import (
    get_trade_journal,
    save_trade_journal,
)
from app.models.trade_journal import TradeJournal

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession):
    await db_session.execute(delete(TradeJournal))
    await db_session.commit()


async def test_save_mock_journal_succeeds():
    res = await save_trade_journal(
        symbol="005930",
        thesis="mock retro practice",
        side="buy",
        entry_price=50000,
        quantity=10,
        account_type="mock",
    )
    assert res["success"] is True, res
    assert res["data"]["account_type"] == "mock"


async def test_mock_does_not_require_account():
    res = await save_trade_journal(
        symbol="005930", thesis="t", account_type="mock"
    )
    assert res["success"] is True, res


async def test_mock_forbids_paper_trade_id():
    res = await save_trade_journal(
        symbol="005930", thesis="t", account_type="mock", paper_trade_id=7
    )
    assert res["success"] is False
    assert "paper_trade_id" in res["error"]


async def test_get_default_surfaces_mock():
    await save_trade_journal(symbol="005930", thesis="t", account_type="mock")
    res = await get_trade_journal()  # default account_type must now be None (query all)
    assert res["success"] is True
    assert any(e["account_type"] == "mock" for e in res["entries"]), res
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_journal_mock_unblock.py -v`
Expected: FAIL — `test_save_mock_journal_succeeds` returns `{"success": False, "error": "Invalid account_type: mock"}`; `test_get_default_surfaces_mock` returns no mock rows.

- [ ] **Step 3: Widen the validation tuple + paper_trade_id guard**

In `app/mcp_server/tooling/trade_journal_tools.py` around line 147:

```python
    # account_type 검증
    if account_type not in ("live", "paper", "mock"):
        return {"success": False, "error": f"Invalid account_type: {account_type}"}
    if account_type in ("live", "mock") and paper_trade_id is not None:
        return {
            "success": False,
            "error": "paper_trade_id cannot be set for live account_type",
        }
    if account_type == "paper" and not account:
        return {
            "success": False,
            "error": "account is required for paper account_type",
        }
```

(Note: leave the paper `account` requirement paper-only — mock has no broker account.)

- [ ] **Step 4: Flip get_trade_journal default to None**

In the same file, `get_trade_journal` signature (~line 248):

```python
    account_type: str | None = None,
```

- [ ] **Step 5: Update docstrings + registration description**

In `trade_journal_tools.py` docstring (~line 130) change the account_type note to:
```python
    account_type='paper'|'mock' for paper/mock journals (paper requires account name).
```
In `get_trade_journal` docstring (~line 257):
```python
    account_type defaults to None (all); set 'live'|'paper'|'mock' to filter.
```
In `app/mcp_server/tooling/trade_journal_registration.py`, find the `save_trade_journal` and `get_trade_journal` description strings and add `mock` wherever `live`/`paper` are listed (e.g. `"account_type: live|paper|mock"`).

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_journal_mock_unblock.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add tests/test_trade_journal_mock_unblock.py app/mcp_server/tooling/trade_journal_tools.py app/mcp_server/tooling/trade_journal_registration.py
git commit -m "feat(ROB-474): save_trade_journal account_type='mock' 개방 (도구 게이트만, migration 0)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `TradeRetrospective` ORM 모델

**Files:**
- Modify: `app/models/review.py` (모델 추가 + import 보강)
- Test: `tests/test_trade_retrospective_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trade_retrospective_model.py
"""ROB-474 — review.trade_retrospectives model round-trip."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeRetrospective

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()


async def test_insert_and_read_back(db_session: AsyncSession):
    row = TradeRetrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        side="buy",
        strategy_key="oversold_bounce",
        correlation_id="cid-1",
        realized_pnl=Decimal("12345.6700"),
        realized_pnl_currency="KRW",
        realized_pnl_source="caller_supplied",
        pnl_pct=Decimal("3.2100"),
        lesson="hold longer",
        next_strategy="scale in on dip",
    )
    db_session.add(row)
    await db_session.commit()

    got = (
        await db_session.execute(
            select(TradeRetrospective).where(TradeRetrospective.correlation_id == "cid-1")
        )
    ).scalar_one()
    assert got.account_mode == "kis_mock"
    assert got.outcome == "filled"
    assert got.realized_pnl == Decimal("12345.6700")
    assert got.fill_evidence_available is True  # server_default
    assert got.created_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_retrospective_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'TradeRetrospective'`.

- [ ] **Step 3: Add the model**

In `app/models/review.py`, ensure imports include `Boolean` and `Decimal`:
- top sqlalchemy import group must include `Boolean` (add it alphabetically to the existing `from sqlalchemy import (...)` block).
- ensure `from decimal import Decimal` and `from datetime import datetime` and `from sqlalchemy.dialects.postgresql import JSONB` are present (most already are; add `Boolean` for sure).

Append this class (after `TradeJournalCounterfactual`):

```python
class TradeRetrospective(Base):
    """ROB-474 — structured trade retrospective (outcome + lesson + next strategy).

    Journal-side typed home for retro outcome so investment_report_items keeps its
    'no execution state on items' invariant. correlation_id is the idempotency key
    (NULL => ad-hoc append; set => upsert). journal_id uses SET NULL: a retro is a
    durable learning record that should survive journal deletion (deliberate
    deviation from the CASCADE used by sibling review tables).
    """

    __tablename__ = "trade_retrospectives"
    __table_args__ = (
        UniqueConstraint(
            "correlation_id", name="uq_trade_retrospectives_correlation_id"
        ),
        CheckConstraint(
            "account_mode IN ('kis_mock','kiwoom_mock','kis_live','alpaca_paper','upbit_live')",
            name="ck_trade_retrospectives_account_mode",
        ),
        CheckConstraint(
            "outcome IN ('filled','partially_filled','unfilled','rejected','cancelled')",
            name="ck_trade_retrospectives_outcome",
        ),
        CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name="ck_trade_retrospectives_side",
        ),
        CheckConstraint(
            "realized_pnl_currency IS NULL OR realized_pnl_currency IN ('KRW','USD')",
            name="ck_trade_retrospectives_currency",
        ),
        CheckConstraint(
            "realized_pnl_source IS NULL OR "
            "realized_pnl_source IN ('caller_supplied','derived_from_journal')",
            name="ck_trade_retrospectives_pnl_source",
        ),
        Index("ix_trade_retrospectives_correlation_id", "correlation_id"),
        Index("ix_trade_retrospectives_journal_id", "journal_id"),
        Index("ix_trade_retrospectives_strategy_key", "strategy_key"),
        Index("ix_trade_retrospectives_symbol", "symbol"),
        Index("ix_trade_retrospectives_report_uuid", "report_uuid"),
        Index(
            "ix_trade_retrospectives_account_mode_created",
            "account_mode",
            "created_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    journal_id: Mapped[int | None] = mapped_column(
        ForeignKey("review.trade_journals.id", ondelete="SET NULL")
    )
    report_uuid: Mapped[str | None] = mapped_column(Text)
    report_item_uuid: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )
    side: Mapped[str | None] = mapped_column(Text)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str | None] = mapped_column(Text)
    strategy_key: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    plan_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    realized_pnl_currency: Mapped[str | None] = mapped_column(Text)
    realized_pnl_source: Mapped[str | None] = mapped_column(Text)
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    fill_evidence_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    rationale: Mapped[str | None] = mapped_column(Text)
    result_summary: Mapped[str | None] = mapped_column(Text)
    lesson: Mapped[str | None] = mapped_column(Text)
    next_strategy: Mapped[str | None] = mapped_column(Text)
    evidence_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    created_by_profile: Mapped[str | None] = mapped_column(Text)
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

(`UniqueConstraint`, `CheckConstraint`, `Index`, `ForeignKey`, `Enum`, `InstrumentType`, `func`, `text`, `TIMESTAMP`, `BigInteger`, `Numeric`, `Text`, `JSONB`, `Mapped`, `mapped_column` are already imported in review.py per the sibling models; only `Boolean` likely needs adding.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_retrospective_model.py -v`
Expected: PASS (`db_session` create_all builds the new table).

- [ ] **Step 5: Commit**

```bash
git add app/models/review.py tests/test_trade_retrospective_model.py
git commit -m "feat(ROB-474): review.trade_retrospectives ORM 모델 추가

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Alembic 마이그레이션 (operator cutover 용)

**Files:**
- Create: `alembic/versions/20260609_rob474_trade_retrospectives.py`

- [ ] **Step 1: Confirm current head**

Run: `uv run alembic heads`
Expected: single head `20260609_rob455`. If it differs (main advanced), use the printed head as `down_revision` below.

- [ ] **Step 2: Write the migration file**

```python
"""rob474 trade_retrospectives

Revision ID: 20260609_rob474
Revises: 20260609_rob455
Create Date: 2026-06-09
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260609_rob474"
down_revision: Union[str, Sequence[str], None] = "20260609_rob455"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_retrospectives",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("report_uuid", sa.Text(), nullable=True),
        sa.Column("report_item_uuid", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column(
            "instrument_type",
            postgresql.ENUM(name="instrument_type", create_type=False),
            nullable=False,
        ),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=True),
        sa.Column("strategy_key", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("plan_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("fill_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(20, 4), nullable=True),
        sa.Column("realized_pnl_currency", sa.Text(), nullable=True),
        sa.Column("realized_pnl_source", sa.Text(), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column(
            "fill_evidence_available",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("lesson", sa.Text(), nullable=True),
        sa.Column("next_strategy", sa.Text(), nullable=True),
        sa.Column("evidence_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by_profile", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["journal_id"], ["review.trade_journals.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "correlation_id", name="uq_trade_retrospectives_correlation_id"
        ),
        sa.CheckConstraint(
            "account_mode IN ('kis_mock','kiwoom_mock','kis_live','alpaca_paper','upbit_live')",
            name="ck_trade_retrospectives_account_mode",
        ),
        sa.CheckConstraint(
            "outcome IN ('filled','partially_filled','unfilled','rejected','cancelled')",
            name="ck_trade_retrospectives_outcome",
        ),
        sa.CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name="ck_trade_retrospectives_side",
        ),
        sa.CheckConstraint(
            "realized_pnl_currency IS NULL OR realized_pnl_currency IN ('KRW','USD')",
            name="ck_trade_retrospectives_currency",
        ),
        sa.CheckConstraint(
            "realized_pnl_source IS NULL OR "
            "realized_pnl_source IN ('caller_supplied','derived_from_journal')",
            name="ck_trade_retrospectives_pnl_source",
        ),
        schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_correlation_id",
        "trade_retrospectives", ["correlation_id"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_journal_id",
        "trade_retrospectives", ["journal_id"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_strategy_key",
        "trade_retrospectives", ["strategy_key"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_symbol",
        "trade_retrospectives", ["symbol"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_report_uuid",
        "trade_retrospectives", ["report_uuid"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_account_mode_created",
        "trade_retrospectives", ["account_mode", "created_at"], schema="review",
    )


def downgrade() -> None:
    for ix in (
        "ix_trade_retrospectives_account_mode_created",
        "ix_trade_retrospectives_report_uuid",
        "ix_trade_retrospectives_symbol",
        "ix_trade_retrospectives_strategy_key",
        "ix_trade_retrospectives_journal_id",
        "ix_trade_retrospectives_correlation_id",
    ):
        op.drop_index(ix, "trade_retrospectives", schema="review")
    op.drop_table("trade_retrospectives", schema="review")
```

- [ ] **Step 3: Sanity-check the file parses + history is linear**

Run: `uv run alembic history | head -5`
Expected: the new `20260609_rob474` sits on top of `20260609_rob455` with no extra heads.
(Do NOT run `alembic upgrade head` against the test DB — alembic is blocked by the timescaledb extension in CI; the table is exercised via `create_all` in tests. Operator runs `alembic upgrade head` in prod per CLAUDE.md cutover gate.)

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/20260609_rob474_trade_retrospectives.py
git commit -m "feat(ROB-474): trade_retrospectives 테이블 alembic 마이그레이션

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `TradeRetrospectiveService` — repository + save + helpers + serializer

**Files:**
- Create: `app/services/trade_journal/trade_retrospective_service.py`
- Test: `tests/test_trade_retrospective_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trade_retrospective_service.py
"""ROB-474 — TradeRetrospectiveService save/guard/derive/upsert."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeRetrospective
from app.models.trade_journal import TradeJournal
from app.services.trade_journal import trade_retrospective_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.execute(delete(TradeJournal))
    await db_session.commit()


async def _mock_journal(db, *, cid="j1"):
    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal("50000"),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=cid,
        status="closed",
        exit_price=Decimal("55000"),
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal("10"),
    )
    db.add(j)
    await db.commit()
    await db.refresh(j)
    return j


async def test_invalid_outcome_rejected(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session, symbol="005930", instrument_type="equity_kr",
            account_mode="kis_mock", outcome="bogus",
        )


async def test_invalid_account_mode_rejected(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session, symbol="005930", instrument_type="equity_kr",
            account_mode="bogus_mode", outcome="filled",
        )


async def test_kiwoom_guard_blocks_fabricated_pnl(db_session: AsyncSession):
    with pytest.raises(svc.RetrospectiveValidationError):
        await svc.save_retrospective(
            db_session, symbol="005930", instrument_type="equity_kr",
            account_mode="kiwoom_mock", outcome="filled",
            realized_pnl=1000.0, realized_pnl_currency="KRW",
        )


async def test_kiwoom_forces_no_fill_evidence(db_session: AsyncSession):
    action, row = await svc.save_retrospective(
        db_session, symbol="005930", instrument_type="equity_kr",
        account_mode="kiwoom_mock", outcome="unfilled",
    )
    await db_session.commit()
    assert action == "created"
    assert row.fill_evidence_available is False


async def test_caller_supplied_realized_pnl(db_session: AsyncSession):
    action, row = await svc.save_retrospective(
        db_session, symbol="005930", instrument_type="equity_kr",
        account_mode="kis_mock", outcome="filled",
        realized_pnl=12345.67, realized_pnl_currency="KRW",
    )
    await db_session.commit()
    assert row.realized_pnl == Decimal("12345.6700")
    assert row.realized_pnl_source == "caller_supplied"


async def test_derive_realized_pnl_from_journal(db_session: AsyncSession):
    j = await _mock_journal(db_session, cid="j1")
    action, row = await svc.save_retrospective(
        db_session, symbol="005930", instrument_type="equity_kr",
        account_mode="kis_mock", outcome="filled", side="buy",
        journal_id=j.id, realized_pnl_currency="KRW",
    )
    await db_session.commit()
    # (55000 - 50000) * 10 = 50000
    assert row.realized_pnl == Decimal("50000.0000")
    assert row.realized_pnl_source == "derived_from_journal"


async def test_upsert_idempotent_by_correlation_id(db_session: AsyncSession):
    a1, _ = await svc.save_retrospective(
        db_session, symbol="005930", instrument_type="equity_kr",
        account_mode="kis_mock", outcome="filled", correlation_id="dup",
        lesson="v1",
    )
    await db_session.commit()
    a2, _ = await svc.save_retrospective(
        db_session, symbol="005930", instrument_type="equity_kr",
        account_mode="kis_mock", outcome="filled", correlation_id="dup",
        lesson="v2",
    )
    await db_session.commit()
    assert a1 == "created"
    assert a2 == "updated"
    rows = (await db_session.execute(
        select(TradeRetrospective).where(TradeRetrospective.correlation_id == "dup")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].lesson == "v2"


async def test_null_correlation_id_appends(db_session: AsyncSession):
    for _ in range(2):
        await svc.save_retrospective(
            db_session, symbol="005930", instrument_type="equity_kr",
            account_mode="kis_mock", outcome="filled",
        )
        await db_session.commit()
    rows = (await db_session.execute(select(TradeRetrospective))).scalars().all()
    assert len(rows) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_retrospective_service.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.trade_journal.trade_retrospective_service`.

- [ ] **Step 3: Write the service module (save half)**

```python
# app/services/trade_journal/trade_retrospective_service.py
"""ROB-474 — structured trade retrospective storage + aggregation.

Repository is the only write surface for review.trade_retrospectives.
Reads are plain module-level async functions (no class), JSON-safe, null-not-zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.models.review import TradeRetrospective
from app.models.trade_journal import TradeJournal

_VALID_ACCOUNT_MODES = {
    "kis_mock", "kiwoom_mock", "kis_live", "alpaca_paper", "upbit_live",
}
_VALID_OUTCOMES = {
    "filled", "partially_filled", "unfilled", "rejected", "cancelled",
}
_NO_FILL_ACCOUNT_MODES = {"kiwoom_mock"}  # fills not readable (ROB-460)
_KST = ZoneInfo("Asia/Seoul")


class RetrospectiveValidationError(ValueError):
    """Raised when a retrospective payload violates a typed constraint."""


def _to_decimal(x: float | None) -> Decimal | None:
    return Decimal(str(x)) if x is not None else None


def _avg(values: list) -> float | None:
    nums: list[Decimal] = []
    for v in values:
        if v is None:
            continue
        try:
            nums.append(Decimal(str(v)))
        except (InvalidOperation, TypeError, ValueError):
            continue
    if not nums:
        return None
    return float(sum(nums) / len(nums))


def serialize_retrospective(r: TradeRetrospective) -> dict[str, Any]:
    return {
        "id": r.id,
        "correlation_id": r.correlation_id,
        "journal_id": r.journal_id,
        "report_uuid": r.report_uuid,
        "report_item_uuid": r.report_item_uuid,
        "symbol": r.symbol,
        "instrument_type": (
            r.instrument_type.value
            if hasattr(r.instrument_type, "value")
            else str(r.instrument_type)
        ),
        "side": r.side,
        "account_mode": r.account_mode,
        "market": r.market,
        "strategy_key": r.strategy_key,
        "outcome": r.outcome,
        "plan_price": float(r.plan_price) if r.plan_price is not None else None,
        "fill_price": float(r.fill_price) if r.fill_price is not None else None,
        "realized_pnl": float(r.realized_pnl) if r.realized_pnl is not None else None,
        "realized_pnl_currency": r.realized_pnl_currency,
        "realized_pnl_source": r.realized_pnl_source,
        "pnl_pct": float(r.pnl_pct) if r.pnl_pct is not None else None,
        "fill_evidence_available": r.fill_evidence_available,
        "rationale": r.rationale,
        "result_summary": r.result_summary,
        "lesson": r.lesson,
        "next_strategy": r.next_strategy,
        "evidence_snapshot": r.evidence_snapshot,
        "created_by_profile": r.created_by_profile,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


class TradeRetrospectiveRepository:
    """The only write surface for review.trade_retrospectives."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_correlation_id(
        self, correlation_id: str
    ) -> TradeRetrospective | None:
        result = await self.db.execute(
            select(TradeRetrospective).where(
                TradeRetrospective.correlation_id == correlation_id
            )
        )
        return result.scalar_one_or_none()

    async def upsert(self, payload: dict[str, Any]) -> tuple[str, TradeRetrospective]:
        cid = payload.get("correlation_id")
        if cid is not None:
            existing = await self.get_by_correlation_id(cid)
            if existing is not None:
                for key, value in payload.items():
                    setattr(existing, key, value)
                await self.db.flush()
                return "updated", existing
        row = TradeRetrospective(**payload)
        self.db.add(row)
        await self.db.flush()
        return "created", row


async def _derive_realized_pnl_from_journal(
    db: AsyncSession, journal_id: int, side: str | None
) -> Decimal | None:
    j = (
        await db.execute(select(TradeJournal).where(TradeJournal.id == journal_id))
    ).scalar_one_or_none()
    if j is None or j.entry_price is None or j.exit_price is None or j.quantity is None:
        return None
    entry = Decimal(str(j.entry_price))
    exit_price = Decimal(str(j.exit_price))
    qty = Decimal(str(j.quantity))
    direction = Decimal("-1") if (side or j.side) == "sell" else Decimal("1")
    return (exit_price - entry) * qty * direction


async def save_retrospective(
    db: AsyncSession,
    *,
    symbol: str,
    instrument_type: str,
    account_mode: str,
    outcome: str,
    side: str | None = None,
    market: str | None = None,
    strategy_key: str | None = None,
    correlation_id: str | None = None,
    journal_id: int | None = None,
    report_uuid: str | None = None,
    report_item_uuid: str | None = None,
    plan_price: float | None = None,
    fill_price: float | None = None,
    realized_pnl: float | None = None,
    realized_pnl_currency: str | None = None,
    pnl_pct: float | None = None,
    rationale: str | None = None,
    result_summary: str | None = None,
    lesson: str | None = None,
    next_strategy: str | None = None,
    evidence_snapshot: dict | None = None,
    created_by_profile: str | None = None,
) -> tuple[str, TradeRetrospective]:
    if account_mode not in _VALID_ACCOUNT_MODES:
        raise RetrospectiveValidationError(f"invalid account_mode: {account_mode}")
    if outcome not in _VALID_OUTCOMES:
        raise RetrospectiveValidationError(f"invalid outcome: {outcome}")
    if side is not None and side not in ("buy", "sell"):
        raise RetrospectiveValidationError(f"invalid side: {side}")
    if realized_pnl_currency is not None and realized_pnl_currency not in ("KRW", "USD"):
        raise RetrospectiveValidationError(
            f"invalid realized_pnl_currency: {realized_pnl_currency}"
        )

    fill_evidence_available = account_mode not in _NO_FILL_ACCOUNT_MODES
    if not fill_evidence_available and (
        realized_pnl is not None or fill_price is not None
    ):
        raise RetrospectiveValidationError(
            f"{account_mode} cannot read fills (ROB-460); "
            "realized_pnl/fill_price not allowed"
        )

    realized_pnl_value = _to_decimal(realized_pnl)
    realized_pnl_source: str | None = None
    if realized_pnl_value is not None:
        realized_pnl_source = "caller_supplied"
    elif journal_id is not None and fill_evidence_available:
        derived = await _derive_realized_pnl_from_journal(db, journal_id, side)
        if derived is not None:
            realized_pnl_value = derived
            realized_pnl_source = "derived_from_journal"

    payload: dict[str, Any] = {
        "symbol": to_db_symbol(symbol),
        "instrument_type": instrument_type,
        "account_mode": account_mode,
        "outcome": outcome,
        "side": side,
        "market": market,
        "strategy_key": strategy_key,
        "correlation_id": correlation_id,
        "journal_id": journal_id,
        "report_uuid": report_uuid,
        "report_item_uuid": report_item_uuid,
        "plan_price": _to_decimal(plan_price),
        "fill_price": _to_decimal(fill_price),
        "realized_pnl": realized_pnl_value,
        "realized_pnl_currency": realized_pnl_currency,
        "realized_pnl_source": realized_pnl_source,
        "pnl_pct": _to_decimal(pnl_pct),
        "fill_evidence_available": fill_evidence_available,
        "rationale": rationale,
        "result_summary": result_summary,
        "lesson": lesson,
        "next_strategy": next_strategy,
        "evidence_snapshot": evidence_snapshot,
        "created_by_profile": created_by_profile,
    }
    repo = TradeRetrospectiveRepository(db)
    return await repo.upsert(payload)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_retrospective_service.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/trade_journal/trade_retrospective_service.py tests/test_trade_retrospective_service.py
git commit -m "feat(ROB-474): TradeRetrospectiveService — repository + save (guard/derive/upsert)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Read/집계 — `get_retrospectives` + `build_retrospective_aggregate`

**Files:**
- Modify: `app/services/trade_journal/trade_retrospective_service.py` (read fns 추가)
- Test: `tests/test_trade_retrospective_aggregate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trade_retrospective_aggregate.py
"""ROB-474 — retrospective list + aggregate."""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeRetrospective
from app.services.trade_journal import trade_retrospective_service as svc

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()


async def _seed(db, *, strategy, pnl, currency="KRW", evidence=True, account_mode="kis_mock"):
    await svc.save_retrospective(
        db, symbol="005930", instrument_type="equity_kr",
        account_mode=account_mode, outcome="filled", strategy_key=strategy,
        realized_pnl=(pnl if evidence else None),
        realized_pnl_currency=(currency if evidence else None),
        pnl_pct=(1.0 if pnl is not None and pnl > 0 else -1.0) if evidence else None,
    )
    await db.commit()


async def test_aggregate_by_strategy_win_rate_and_sum(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0)
    await _seed(db_session, strategy="A", pnl=-50.0)
    await _seed(db_session, strategy="B", pnl=200.0)
    result = await svc.build_retrospective_aggregate(
        db_session, group_by="strategy",
    )
    groups = {g["group"]: g for g in result["groups"]}
    assert groups["A"]["sample_size"] == 2
    assert groups["A"]["wins"] == 1
    assert groups["A"]["misses"] == 1
    assert groups["A"]["win_rate_pct"] == 50.0
    assert groups["A"]["realized_pnl_sum"]["KRW"] == 50.0  # 100 + (-50)
    assert groups["B"]["win_rate_pct"] == 100.0


async def test_currency_separated_sum(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0, currency="KRW")
    await _seed(db_session, strategy="A", pnl=5.0, currency="USD")
    result = await svc.build_retrospective_aggregate(db_session, group_by="strategy")
    g = result["groups"][0]
    assert g["realized_pnl_sum"] == {"KRW": 100.0, "USD": 5.0}


async def test_no_fill_evidence_excluded_from_aggregate(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0, evidence=True)
    # kiwoom: no evidence row
    await svc.save_retrospective(
        db_session, symbol="005930", instrument_type="equity_kr",
        account_mode="kiwoom_mock", outcome="unfilled", strategy_key="A",
    )
    await db_session.commit()
    result = await svc.build_retrospective_aggregate(db_session, group_by="strategy")
    assert result["excluded_no_fill_evidence"] == 1
    assert result["groups"][0]["sample_size"] == 1


async def test_empty_window_returns_no_groups(db_session: AsyncSession):
    result = await svc.build_retrospective_aggregate(
        db_session, kst_date_from="2000-01-01", kst_date_to="2000-01-02",
        group_by="strategy",
    )
    assert result["groups"] == []


async def test_get_retrospectives_list_and_summary(db_session: AsyncSession):
    await _seed(db_session, strategy="A", pnl=100.0)
    res = await svc.get_retrospectives(db_session, strategy_key="A")
    assert res["summary"]["count"] == 1
    assert res["summary"]["by_outcome"]["filled"] == 1
    assert res["entries"][0]["strategy_key"] == "A"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_retrospective_aggregate.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'build_retrospective_aggregate'`.

- [ ] **Step 3: Append the read functions**

Add to `app/services/trade_journal/trade_retrospective_service.py`:

```python
def _kst_day_start(date_str: str) -> datetime:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=_KST)


def _kst_day_end(date_str: str) -> datetime:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=_KST)


def _kst_date_str(dt: datetime) -> str:
    return dt.astimezone(_KST).date().isoformat()


async def get_retrospectives(
    db: AsyncSession,
    *,
    symbol: str | None = None,
    account_mode: str | None = None,
    strategy_key: str | None = None,
    market: str | None = None,
    correlation_id: str | None = None,
    days: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    filters = []
    if symbol is not None:
        filters.append(TradeRetrospective.symbol == to_db_symbol(symbol))
    if account_mode is not None:
        filters.append(TradeRetrospective.account_mode == account_mode)
    if strategy_key is not None:
        filters.append(TradeRetrospective.strategy_key == strategy_key)
    if market is not None:
        filters.append(TradeRetrospective.market == market)
    if correlation_id is not None:
        filters.append(TradeRetrospective.correlation_id == correlation_id)
    if days is not None:
        filters.append(TradeRetrospective.created_at >= now_kst() - timedelta(days=days))
    stmt = (
        select(TradeRetrospective)
        .where(*filters)
        .order_by(TradeRetrospective.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
    return {
        "entries": [serialize_retrospective(r) for r in rows],
        "summary": {"count": len(rows), "by_outcome": by_outcome},
    }


def _is_win(r: TradeRetrospective) -> bool:
    if r.realized_pnl is not None:
        return r.realized_pnl > 0
    return r.pnl_pct is not None and r.pnl_pct > 0


def _is_decided(r: TradeRetrospective) -> bool:
    return r.realized_pnl is not None or r.pnl_pct is not None


async def build_retrospective_aggregate(
    db: AsyncSession,
    *,
    kst_date_from: str | None = None,
    kst_date_to: str | None = None,
    account_mode: str | None = None,
    market: str | None = None,
    strategy_key: str | None = None,
    group_by: str = "strategy",
) -> dict[str, Any]:
    if group_by not in ("strategy", "day"):
        group_by = "strategy"
    filters = []
    if account_mode is not None:
        filters.append(TradeRetrospective.account_mode == account_mode)
    if market is not None:
        filters.append(TradeRetrospective.market == market)
    if strategy_key is not None:
        filters.append(TradeRetrospective.strategy_key == strategy_key)
    if kst_date_from is not None:
        filters.append(TradeRetrospective.created_at >= _kst_day_start(kst_date_from))
    if kst_date_to is not None:
        filters.append(TradeRetrospective.created_at <= _kst_day_end(kst_date_to))

    rows = (
        await db.execute(select(TradeRetrospective).where(*filters))
    ).scalars().all()

    groups: dict[str, list[TradeRetrospective]] = {}
    excluded_no_evidence = 0
    for r in rows:
        if not r.fill_evidence_available:
            excluded_no_evidence += 1
            continue
        key = (r.strategy_key or "no_strategy") if group_by == "strategy" else _kst_date_str(r.created_at)
        groups.setdefault(key, []).append(r)

    out: list[dict[str, Any]] = []
    for key, items in groups.items():
        decided = [it for it in items if _is_decided(it)]
        wins = sum(1 for it in decided if _is_win(it))
        misses = len(decided) - wins
        realized_sum: dict[str, float] = {}
        for it in items:
            if it.realized_pnl is not None and it.realized_pnl_currency:
                realized_sum[it.realized_pnl_currency] = (
                    realized_sum.get(it.realized_pnl_currency, 0.0)
                    + float(it.realized_pnl)
                )
        by_outcome: dict[str, int] = {}
        for it in items:
            by_outcome[it.outcome] = by_outcome.get(it.outcome, 0) + 1
        out.append({
            "group": key,
            "sample_size": len(items),
            "wins": wins,
            "misses": misses,
            "win_rate_pct": (wins / len(decided) * 100.0) if decided else None,
            "avg_pnl_pct": _avg([it.pnl_pct for it in items]),
            "realized_pnl_sum": realized_sum,
            "by_outcome": by_outcome,
        })
    out.sort(key=lambda g: -g["sample_size"])
    return {
        "group_by": group_by,
        "groups": out,
        "excluded_no_fill_evidence": excluded_no_evidence,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_retrospective_aggregate.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/trade_journal/trade_retrospective_service.py tests/test_trade_retrospective_aggregate.py
git commit -m "feat(ROB-474): 회고 read/집계 — get_retrospectives + build_retrospective_aggregate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: MCP 도구 (`save_trade_retrospective` / `get_trade_retrospectives` / `get_retrospective_aggregate`)

**Files:**
- Create: `app/mcp_server/tooling/trade_retrospective_tools.py`
- Test: `tests/test_trade_retrospective_tools.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trade_retrospective_tools.py
"""ROB-474 — MCP tool envelopes for trade retrospectives."""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.trade_retrospective_tools import (
    get_retrospective_aggregate,
    get_trade_retrospectives,
    save_trade_retrospective,
)
from app.models.review import TradeRetrospective

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest.fixture(autouse=True)
async def _cleanup(db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()


async def test_save_success_envelope():
    res = await save_trade_retrospective(
        symbol="005930", instrument_type="equity_kr",
        account_mode="kis_mock", outcome="filled", strategy_key="A",
        realized_pnl=100.0, realized_pnl_currency="KRW", lesson="ok",
    )
    assert res["success"] is True
    assert res["action"] == "created"
    assert res["data"]["strategy_key"] == "A"


async def test_save_validation_error_envelope():
    res = await save_trade_retrospective(
        symbol="005930", instrument_type="equity_kr",
        account_mode="kis_mock", outcome="bogus",
    )
    assert res["success"] is False
    assert "outcome" in res["error"]


async def test_save_missing_symbol_envelope():
    res = await save_trade_retrospective(
        symbol="", instrument_type="equity_kr",
        account_mode="kis_mock", outcome="filled",
    )
    assert res["success"] is False
    assert "symbol" in res["error"]


async def test_get_list_envelope():
    await save_trade_retrospective(
        symbol="005930", instrument_type="equity_kr",
        account_mode="kis_mock", outcome="filled", strategy_key="A",
    )
    res = await get_trade_retrospectives(strategy_key="A")
    assert res["success"] is True
    assert res["summary"]["count"] == 1
    assert "entries" in res


async def test_aggregate_envelope():
    await save_trade_retrospective(
        symbol="005930", instrument_type="equity_kr",
        account_mode="kis_mock", outcome="filled", strategy_key="A",
        realized_pnl=100.0, realized_pnl_currency="KRW",
    )
    res = await get_retrospective_aggregate(group_by="strategy")
    assert res["success"] is True
    assert "groups" in res
    assert res["groups"][0]["group"] == "A"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_trade_retrospective_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: app.mcp_server.tooling.trade_retrospective_tools`.

- [ ] **Step 3: Write the tools module**

```python
# app/mcp_server/tooling/trade_retrospective_tools.py
"""ROB-474 — MCP tools for structured trade retrospectives."""

from __future__ import annotations

import logging
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.trade_journal.trade_retrospective_service import (
    RetrospectiveValidationError,
    build_retrospective_aggregate,
    get_retrospectives,
    save_retrospective,
    serialize_retrospective,
)

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


async def save_trade_retrospective(
    symbol: str,
    instrument_type: str,
    account_mode: str,
    outcome: str,
    side: str | None = None,
    market: str | None = None,
    strategy_key: str | None = None,
    correlation_id: str | None = None,
    journal_id: int | None = None,
    report_uuid: str | None = None,
    report_item_uuid: str | None = None,
    plan_price: float | None = None,
    fill_price: float | None = None,
    realized_pnl: float | None = None,
    realized_pnl_currency: str | None = None,
    pnl_pct: float | None = None,
    rationale: str | None = None,
    result_summary: str | None = None,
    lesson: str | None = None,
    next_strategy: str | None = None,
    evidence_snapshot: dict | None = None,
    created_by_profile: str | None = None,
) -> dict[str, Any]:
    if not symbol:
        return {"success": False, "error": "symbol is required"}
    try:
        async with _session_factory()() as db:
            action, row = await save_retrospective(
                db,
                symbol=symbol,
                instrument_type=instrument_type,
                account_mode=account_mode,
                outcome=outcome,
                side=side,
                market=market,
                strategy_key=strategy_key,
                correlation_id=correlation_id,
                journal_id=journal_id,
                report_uuid=report_uuid,
                report_item_uuid=report_item_uuid,
                plan_price=plan_price,
                fill_price=fill_price,
                realized_pnl=realized_pnl,
                realized_pnl_currency=realized_pnl_currency,
                pnl_pct=pnl_pct,
                rationale=rationale,
                result_summary=result_summary,
                lesson=lesson,
                next_strategy=next_strategy,
                evidence_snapshot=evidence_snapshot,
                created_by_profile=created_by_profile,
            )
            await db.commit()
            await db.refresh(row)
            return {
                "success": True,
                "action": action,
                "data": serialize_retrospective(row),
            }
    except RetrospectiveValidationError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("save_trade_retrospective failed")
        return {"success": False, "error": f"save_trade_retrospective failed: {exc}"}


async def get_trade_retrospectives(
    symbol: str | None = None,
    account_mode: str | None = None,
    strategy_key: str | None = None,
    market: str | None = None,
    correlation_id: str | None = None,
    days: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    try:
        async with _session_factory()() as db:
            result = await get_retrospectives(
                db,
                symbol=symbol,
                account_mode=account_mode,
                strategy_key=strategy_key,
                market=market,
                correlation_id=correlation_id,
                days=days,
                limit=limit,
            )
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_trade_retrospectives failed")
        return {"success": False, "error": f"get_trade_retrospectives failed: {exc}"}


async def get_retrospective_aggregate(
    kst_date_from: str | None = None,
    kst_date_to: str | None = None,
    account_mode: str | None = None,
    market: str | None = None,
    strategy_key: str | None = None,
    group_by: str = "strategy",
) -> dict[str, Any]:
    today = now_kst().date().isoformat()
    date_from = kst_date_from or today
    date_to = kst_date_to or date_from
    try:
        async with _session_factory()() as db:
            result = await build_retrospective_aggregate(
                db,
                kst_date_from=date_from,
                kst_date_to=date_to,
                account_mode=account_mode,
                market=market,
                strategy_key=strategy_key,
                group_by=group_by,
            )
        return {
            "success": True,
            "kst_date_from": date_from,
            "kst_date_to": date_to,
            **result,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_retrospective_aggregate failed")
        return {"success": False, "error": f"get_retrospective_aggregate failed: {exc}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_trade_retrospective_tools.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/trade_retrospective_tools.py tests/test_trade_retrospective_tools.py
git commit -m "feat(ROB-474): MCP 도구 save/get/aggregate trade_retrospective

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 등록 배선 (registration 모듈 + registry + AVAILABLE_TOOL_NAMES)

**Files:**
- Create: `app/mcp_server/tooling/trade_retrospective_registration.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Modify: `app/mcp_server/__init__.py`
- Test: `tests/test_trade_retrospective_tools.py` (등록 테스트 추가)

- [ ] **Step 1: Write the failing test (append to the tools test file)**

```python
# append to tests/test_trade_retrospective_tools.py

def test_tool_names_set_complete():
    from app.mcp_server.tooling.trade_retrospective_registration import (
        TRADE_RETROSPECTIVE_TOOL_NAMES,
    )
    assert TRADE_RETROSPECTIVE_TOOL_NAMES == {
        "save_trade_retrospective",
        "get_trade_retrospectives",
        "get_retrospective_aggregate",
    }


def test_tools_in_available_surface():
    from app.mcp_server import AVAILABLE_TOOL_NAMES
    for name in (
        "save_trade_retrospective",
        "get_trade_retrospectives",
        "get_retrospective_aggregate",
    ):
        assert name in AVAILABLE_TOOL_NAMES


def test_register_wires_three_tools():
    from app.mcp_server.tooling.trade_retrospective_registration import (
        register_trade_retrospective_tools,
    )

    registered: list[str] = []

    class _FakeMCP:
        def tool(self, *, name, description):
            registered.append(name)

            def _wrap(fn):
                return fn

            return _wrap

    register_trade_retrospective_tools(_FakeMCP())
    assert set(registered) == {
        "save_trade_retrospective",
        "get_trade_retrospectives",
        "get_retrospective_aggregate",
    }
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_trade_retrospective_tools.py -v -k "tool_names or available or register_wires"`
Expected: FAIL — `ModuleNotFoundError` / names not in `AVAILABLE_TOOL_NAMES`.

- [ ] **Step 3: Write the registration module**

```python
# app/mcp_server/tooling/trade_retrospective_registration.py
"""ROB-474 — MCP registration for trade retrospective tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.trade_retrospective_tools import (
    get_retrospective_aggregate,
    get_trade_retrospectives,
    save_trade_retrospective,
)

TRADE_RETROSPECTIVE_TOOL_NAMES: set[str] = {
    "save_trade_retrospective",
    "get_trade_retrospectives",
    "get_retrospective_aggregate",
}


def register_trade_retrospective_tools(mcp: Any) -> None:
    _ = mcp.tool(
        name="save_trade_retrospective",
        description=(
            "Store a structured trade retrospective (outcome, absolute realized_pnl, "
            "fill/plan price, pnl_pct, rationale/result/lesson/next_strategy) for a "
            "trade. account_mode in {kis_mock, kiwoom_mock, kis_live, alpaca_paper, "
            "upbit_live}. Idempotent per correlation_id (omit it to append). "
            "kiwoom_mock cannot supply realized_pnl/fill_price (no fill evidence, "
            "ROB-460). realized_pnl is caller-supplied, or derived from journal_id "
            "when entry/exit/qty are present."
        ),
    )(save_trade_retrospective)
    _ = mcp.tool(
        name="get_trade_retrospectives",
        description=(
            "List structured trade retrospectives with filters "
            "(symbol/account_mode/strategy_key/market/correlation_id/days). Read-only."
        ),
    )(get_trade_retrospectives)
    _ = mcp.tool(
        name="get_retrospective_aggregate",
        description=(
            "Aggregate retrospectives by strategy_key or KST day over a KST date "
            "window: win_rate_pct, avg_pnl_pct, absolute realized_pnl sum (per "
            "currency), wins/misses. Only rows with fill evidence are counted "
            "(excluded_no_fill_evidence reported). Read-only. Complements "
            "get_mock_loop_retrospective (KST-day x watch-loop x percent)."
        ),
    )(get_retrospective_aggregate)


__all__ = ["TRADE_RETROSPECTIVE_TOOL_NAMES", "register_trade_retrospective_tools"]
```

- [ ] **Step 4: Wire into registry.py**

In `app/mcp_server/tooling/registry.py`, near the existing `from app.mcp_server.tooling.mock_loop_retro_registration import (register_mock_loop_retro_tools,)` import, add:

```python
from app.mcp_server.tooling.trade_retrospective_registration import (
    register_trade_retrospective_tools,
)
```

In the same function where `register_mock_loop_retro_tools(mcp)` is called (the "Always: read-only with account_mode (mock-safe via ROB-28)" block), add immediately after it:

```python
    register_trade_retrospective_tools(mcp)
```

- [ ] **Step 5: Add to AVAILABLE_TOOL_NAMES**

In `app/mcp_server/__init__.py`, find the `AVAILABLE_TOOL_NAMES = [...]` list and append after `"get_mock_loop_retrospective"`:

```python
    "save_trade_retrospective",
    "get_trade_retrospectives",
    "get_retrospective_aggregate",
```

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/test_trade_retrospective_tools.py -v`
Expected: PASS (all, incl. 3 new registration tests)

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/trade_retrospective_registration.py app/mcp_server/tooling/registry.py app/mcp_server/__init__.py tests/test_trade_retrospective_tools.py
git commit -m "feat(ROB-474): trade_retrospective 도구 등록 배선 (registry + AVAILABLE_TOOL_NAMES)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 전체 검증 (suite + lint + typecheck)

**Files:** none (verification only)

- [ ] **Step 1: Run the new test modules together**

Run:
```bash
uv run pytest tests/test_trade_journal_mock_unblock.py tests/test_trade_retrospective_model.py tests/test_trade_retrospective_service.py tests/test_trade_retrospective_aggregate.py tests/test_trade_retrospective_tools.py -v
```
Expected: ALL PASS.

- [ ] **Step 2: Regression — sibling trade_journal / mock-loop tests still green**

Run:
```bash
uv run pytest tests/test_mcp_trade_journal.py tests/test_mock_loop_retro_tool.py tests/test_journal_verdict_service.py tests/test_journal_counterfactual_service.py -v
```
Expected: ALL PASS (mock unblock + new table must not regress these).

- [ ] **Step 3: Lint + typecheck (CI runs app/ AND tests/)**

Run:
```bash
make lint
make typecheck
```
Expected: clean. Fix any ruff/ty findings in the new files (incl. test files — CI lints `tests/`). Do NOT leave unused imports.

- [ ] **Step 4: Full suite (or at least the broker/mcp slice) for safety**

Run: `make test`
Expected: green. If xdist flakes on a `review.*` deadlock unrelated to ROB-474, re-run; the new tests already hold `investment_reports_cleanup_lock`.

- [ ] **Step 5: Final commit if any lint fixups were made**

```bash
git add -A
git commit -m "chore(ROB-474): lint/typecheck 정리

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (spec coverage)

| Spec 요구 | Task |
|---|---|
| `review.trade_retrospectives` typed 테이블 (모든 컬럼/CHECK/Index) | Task 2 (모델) + Task 3 (마이그레이션) |
| `save_trade_retrospective` write + service/repository (유일 쓰기) | Task 4 + Task 6 |
| 키움 가드(fill 증거 없음→날조 금지) | Task 4 (`test_kiwoom_guard_*`) |
| realized_pnl caller-supplied / journal 파생 + source 표기 | Task 4 (`test_caller_supplied`, `test_derive_*`) |
| 멱등 upsert(correlation_id) + ad-hoc append | Task 4 (`test_upsert_*`, `test_null_correlation_id_appends`) |
| `get_trade_retrospectives` + `get_retrospective_aggregate` (strategy/day, win_rate, 통화별 sum, evidence 필터, 빈→None) | Task 5 + Task 6 |
| 기존 `get_mock_loop_retrospective`와 비중복 | 설계상 분리(strategy×free-window×절대 PnL); 도구 description에 명시(Task 7) |
| `save_trade_journal` mock 개방 + get 기본값 None | Task 1 |
| 등록 4 touch point(모듈/registry/AVAILABLE_TOOL_NAMES/TOOL_NAMES set) | Task 7 |
| 플래그 없음, broker mutation 없음 | 설계상 inert; 어떤 task도 게이트/스케줄러 추가 안 함 |
| report item 스키마 불변 | 어떤 task도 investment_reports 모델/스키마 변경 안 함 |
| migration 1개·trade_journals 미변경 | Task 3 (테이블 1개), Task 1 (마이그레이션 0) |
| 테스트 정직성(missing≠zero, div-0 가드) | Task 5 (`_avg`/`win_rate_pct` None 가드) |

**Deferred(Non-goals) — 의도적으로 task 없음:** 피드백 엣지 배선, strategy_key 자동 스레딩, 주문 ledger 자동 fill(ROB-459 P4), reconcile PnL booking, HTTP 라우터, trade_journals broker discriminator 컬럼.

**Placeholder scan:** 없음(모든 step에 실제 코드/명령/기대 출력). `<rev>` 류 미사용(revision id 명시 `20260609_rob474`, Step 1에서 head 재확인 지시).

**Type consistency:** 서비스 fn 명(`save_retrospective`/`get_retrospectives`/`build_retrospective_aggregate`/`serialize_retrospective`/`RetrospectiveValidationError`)이 Task 4·5·6에서 동일. 도구 fn 명(`save_trade_retrospective`/`get_trade_retrospectives`/`get_retrospective_aggregate`)이 Task 6·7·`TOOL_NAMES`·`AVAILABLE_TOOL_NAMES`에서 동일. 모델 컬럼명이 모델/서비스/serializer/마이그레이션 전반 일치.
