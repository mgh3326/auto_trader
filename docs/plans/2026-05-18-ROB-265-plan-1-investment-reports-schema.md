# ROB-265 Plan 1 — Investment Reports Schema & ORM (Additive)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the five `investment_*` tables (under `review` schema), their SQLAlchemy ORM models, and a single Alembic migration — fully additive so legacy `analysis_*` / `watch_order_intent_ledger` continue to function. No services, MCP, API, scanner, or frontend changes in this plan.

**Architecture:** Tables follow the locked decisions from the ROB-265 conversation: `item_kind = action | watch | risk` (no `no_action`/`thesis_update`/`action_preview` — those collapse into report-level columns), `item_status` excludes `executed`, `target_kind` lives on both items and alerts to preserve scanner asset/index/fx semantics, idempotency is deterministic per row class, and advisory-only invariants are enforced via DB `CHECK` constraints so a `kis_live` account scope or an `nxt` market session cannot be paired with an `execution_mode != 'advisory_only'`. Items are the source of truth; alerts are an immutable activation snapshot pointing back at the source item/report. Tests assert constraint behavior and key-composition stability.

**Tech Stack:** SQLAlchemy 2.x typed ORM (`Mapped`, `mapped_column`), PostgreSQL (JSONB, partial unique indexes, `CHECK`), Alembic async (`alembic/env.py` already configured), pytest-asyncio. Base class `app.models.base.Base`.

---

## File Structure

**Create:**
- `app/models/investment_reports.py` — 5 ORM classes (`InvestmentReport`, `InvestmentReportItem`, `InvestmentReportItemDecision`, `InvestmentWatchAlert`, `InvestmentWatchEvent`)
- `app/services/investment_reports/__init__.py` — package init (empty body, makes the package importable)
- `app/services/investment_reports/idempotency.py` — deterministic key composers + canonical watch-condition hash
- `alembic/versions/<NEW_REV>_rob265_add_investment_reports.py` — single migration; upgrade + downgrade
- `tests/test_investment_reports_model.py` — ORM round-trip and constraint tests
- `tests/test_investment_reports_idempotency.py` — key-composition unit tests

**Modify:**
- `app/models/__init__.py` — export the 5 new classes after the existing `.research_reports` line so Alembic autogenerate sees them.

---

### Task 1: Pre-flight — branch, head, and PostgreSQL test DB

**Files:** none

- [ ] **Step 1: Confirm worktree state**

Run: `git status && git rev-parse --abbrev-ref HEAD`
Expected: working tree clean; branch is `rob-265`.

- [ ] **Step 2: Capture the current Alembic head as `down_revision`**

Run: `uv run alembic heads`
Expected: one revision id printed (e.g. `f974ac12e573 (head)`).
Record this id. You will paste it into the new migration's `down_revision` in Task 9.

- [ ] **Step 3: Bring up local PostgreSQL + Redis for tests**

Run: `docker compose up -d postgres redis && docker compose ps`
Expected: both services healthy.

- [ ] **Step 4: Apply migrations to test DB so the `review` schema exists**

Run: `uv run alembic upgrade head`
Expected: "INFO ... Running upgrade ..." ending at the head you captured in Step 2, no errors.

---

### Task 2: Scaffold `investment_reports` model module and idempotency package

**Files:**
- Create: `app/models/investment_reports.py`
- Create: `app/services/investment_reports/__init__.py`
- Create: `app/services/investment_reports/idempotency.py`

- [ ] **Step 1: Create the empty model file with imports only**

Write to `app/models/investment_reports.py`:

```python
"""Investment report-scoped persistence (ROB-265).

Five entities under the ``review`` schema replace the legacy
``analysis_report*`` / ``watch_order_intent_ledger`` family.

* ``InvestmentReport`` — report header (one per published/draft report bundle).
* ``InvestmentReportItem`` — action/watch/risk items owned by a report.
* ``InvestmentReportItemDecision`` — operator decisions on items (audit).
* ``InvestmentWatchAlert`` — immutable activation snapshot of approved watch items.
* ``InvestmentWatchEvent`` — trigger events the scanner writes when an alert fires.

The shape is intentionally NOT backward-compatible with the legacy tables.
All writes must go through ``app.services.investment_reports.*`` (added in a
later plan). Direct ``INSERT/UPDATE/DELETE`` is forbidden once those services
land.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base
```

- [ ] **Step 2: Create the idempotency package init**

Write to `app/services/investment_reports/__init__.py`:

```python
"""Investment report service package (ROB-265).

Service-layer modules will be added in later plans. This package exists so
that ``app.services.investment_reports.idempotency`` can be imported by the
model layer and tests.
"""
```

- [ ] **Step 3: Create the idempotency module skeleton (real composers come in Task 8)**

Write to `app/services/investment_reports/idempotency.py`:

```python
"""Deterministic idempotency-key composers for ROB-265 investment reports.

Keys are colon-joined, lowercase where applicable, and use ``_`` for
``None`` slots so that a missing field never collides with another value.
The canonical watch-condition hash is sha256 over a sorted-key JSON dump.

Composers (filled in during Task 8):
* ``report_key``
* ``item_key``
* ``watch_activation_key``
* ``watch_event_key``
* ``canonical_watch_condition_hash``
"""

from __future__ import annotations

__all__ = [
    "report_key",
    "item_key",
    "watch_activation_key",
    "watch_event_key",
    "canonical_watch_condition_hash",
]
```

- [ ] **Step 4: Commit scaffolding**

```bash
git add app/models/investment_reports.py app/services/investment_reports/__init__.py app/services/investment_reports/idempotency.py
git commit -m "chore(rob-265): scaffold investment_reports model + idempotency package"
```

---

### Task 3: `InvestmentReport` ORM + constraint tests

**Files:**
- Modify: `app/models/investment_reports.py` (append the class)
- Create: `tests/test_investment_reports_model.py`

- [ ] **Step 1: Append the `InvestmentReport` class**

Append to `app/models/investment_reports.py`:

```python
# ---------------------------------------------------------------------------
# review.investment_reports — report header (ROB-265)
# ---------------------------------------------------------------------------
class InvestmentReport(Base):
    """Report-scoped header artifact. Owns items, decisions, and watches.

    ``thesis_text`` and ``no_action_note`` are report-level fields (locked
    refinement: kept off the item table to avoid ``item_kind`` bloat).
    ``previous_report_uuid`` is a trace hint only — context retrieval is
    a query in a later plan, not a single-link traversal.
    """

    __tablename__ = "investment_reports"
    __table_args__ = (
        UniqueConstraint("report_uuid", name="uq_investment_reports_report_uuid"),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_reports_idempotency_key"
        ),
        CheckConstraint(
            "status IN ('draft','published','decided','expired','superseded')",
            name="ck_investment_reports_status",
        ),
        CheckConstraint(
            "execution_mode IN ('advisory_only','mock_preview')",
            name="ck_investment_reports_execution_mode",
        ),
        CheckConstraint(
            "account_scope IS NULL OR account_scope IN "
            "('kis_live','kis_mock','alpaca_paper','upbit_live')",
            name="ck_investment_reports_account_scope",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_investment_reports_market",
        ),
        CheckConstraint(
            "market_session IS NULL OR market_session IN "
            "('regular','nxt','pre','post','24x7')",
            name="ck_investment_reports_market_session",
        ),
        # Advisory-only invariants — locked refinement #6.
        # If account is live, execution_mode MUST be advisory_only.
        CheckConstraint(
            "account_scope IS DISTINCT FROM 'kis_live' "
            "OR execution_mode = 'advisory_only'",
            name="ck_investment_reports_live_advisory_only",
        ),
        # If session is NXT, execution_mode MUST be advisory_only.
        CheckConstraint(
            "market_session IS DISTINCT FROM 'nxt' "
            "OR execution_mode = 'advisory_only'",
            name="ck_investment_reports_nxt_advisory_only",
        ),
        Index(
            "ix_investment_reports_market_session_created",
            "market",
            "market_session",
            "created_at",
        ),
        Index("ix_investment_reports_status_created", "status", "created_at"),
        Index(
            "ix_investment_reports_report_type_created", "report_type", "created_at"
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    market_session: Mapped[str | None] = mapped_column(Text)
    account_scope: Mapped[str | None] = mapped_column(Text)
    execution_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'advisory_only'")
    )
    created_by_profile: Mapped[str] = mapped_column(Text, nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    risk_summary: Mapped[str | None] = mapped_column(Text)
    thesis_text: Mapped[str | None] = mapped_column(Text)
    no_action_note: Mapped[str | None] = mapped_column(Text)

    market_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    portfolio_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    previous_report_uuid: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )

    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'draft'")
    )
    report_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
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
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
```

- [ ] **Step 2: Create the failing constraint test**

Write to `tests/test_investment_reports_model.py`:

```python
"""ROB-265 — InvestmentReport ORM + advisory-only invariant tests.

These exercise DB-level CHECK constraints, so they require the real
PostgreSQL configured by ``tests/conftest.py`` (DATABASE_URL points at
``test_db`` by default). Run with ``docker compose up -d postgres redis``
beforehand.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.base import Base
from app.models.investment_reports import InvestmentReport


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[InvestmentReport.__table__],
            checkfirst=True,
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.drop_all,
            tables=[InvestmentReport.__table__],
            checkfirst=True,
        )
    await engine.dispose()


def _base_payload(**overrides) -> dict:
    payload = dict(
        report_uuid=uuid.uuid4(),
        idempotency_key=f"key-{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="테스트 리포트",
        summary="요약",
        status="draft",
    )
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_round_trip_insert(session: AsyncSession) -> None:
    row = InvestmentReport(**_base_payload())
    session.add(row)
    await session.commit()

    result = await session.execute(sa.select(InvestmentReport).where(InvestmentReport.id == row.id))
    fetched = result.scalar_one()
    assert fetched.market == "kr"
    assert fetched.execution_mode == "mock_preview"
    assert fetched.market_snapshot == {}
    assert fetched.report_metadata == {}


@pytest.mark.asyncio
async def test_idempotency_key_is_unique(session: AsyncSession) -> None:
    key = f"dup-{uuid.uuid4()}"
    session.add(InvestmentReport(**_base_payload(idempotency_key=key)))
    await session.commit()

    session.add(InvestmentReport(**_base_payload(idempotency_key=key)))
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_advisory_only_invariant_blocks_live_with_mock_preview(
    session: AsyncSession,
) -> None:
    """kis_live account scope MUST pair with execution_mode='advisory_only'."""
    session.add(
        InvestmentReport(
            **_base_payload(
                account_scope="kis_live",
                execution_mode="mock_preview",
            )
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_advisory_only_invariant_allows_live_with_advisory_only(
    session: AsyncSession,
) -> None:
    row = InvestmentReport(
        **_base_payload(
            account_scope="kis_live",
            execution_mode="advisory_only",
        )
    )
    session.add(row)
    await session.commit()
    assert row.id is not None


@pytest.mark.asyncio
async def test_nxt_session_requires_advisory_only(session: AsyncSession) -> None:
    session.add(
        InvestmentReport(
            **_base_payload(
                market_session="nxt",
                execution_mode="mock_preview",
            )
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_status_check_rejects_unknown_value(session: AsyncSession) -> None:
    session.add(InvestmentReport(**_base_payload(status="bogus")))
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()
```

- [ ] **Step 3: Run tests — they should FAIL (table not yet in metadata)**

Run: `uv run pytest tests/test_investment_reports_model.py -v`
Expected: tests FAIL at collection or at `Base.metadata.create_all` because either:
- the test file can't import `InvestmentReport` (if Step 1 was skipped), or
- the CHECK constraint names collide with leftover from an earlier run.

Verify the failure is "table created cleanly, constraints active" by running once more:
Run: `uv run pytest tests/test_investment_reports_model.py::test_round_trip_insert -v`
Expected: PASS now (table is created per-test).

- [ ] **Step 4: Run all 6 tests in this file**

Run: `uv run pytest tests/test_investment_reports_model.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models/investment_reports.py tests/test_investment_reports_model.py
git commit -m "feat(rob-265): add InvestmentReport ORM with advisory-only invariants"
```

---

### Task 4: `InvestmentReportItem` ORM + tests

**Files:**
- Modify: `app/models/investment_reports.py` (append the class)
- Modify: `tests/test_investment_reports_model.py` (append tests + extend the session fixture)

- [ ] **Step 1: Append the `InvestmentReportItem` class**

Append to `app/models/investment_reports.py`:

```python
# ---------------------------------------------------------------------------
# review.investment_report_items — action/watch/risk items owned by a report
# ---------------------------------------------------------------------------
class InvestmentReportItem(Base):
    """Report-owned proposal item. Source of truth for proposed watches.

    Locked refinements:
    * ``item_kind ∈ {action, watch, risk}`` only.
    * ``item_status`` excludes ``executed`` — execution lives in trade
      journals/broker ledgers, never on a report item.
    * ``target_kind`` preserved so the watch scanner's asset/index/fx
      dispatch can be reproduced.
    """

    __tablename__ = "investment_report_items"
    __table_args__ = (
        UniqueConstraint("item_uuid", name="uq_investment_report_items_item_uuid"),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_report_items_idempotency_key"
        ),
        CheckConstraint(
            "item_kind IN ('action','watch','risk')",
            name="ck_investment_report_items_item_kind",
        ),
        CheckConstraint(
            "status IN ('proposed','approved','denied','deferred','activated','expired')",
            name="ck_investment_report_items_status",
        ),
        CheckConstraint(
            "target_kind IN ('asset','index','fx')",
            name="ck_investment_report_items_target_kind",
        ),
        CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name="ck_investment_report_items_side",
        ),
        CheckConstraint(
            "intent IN ('buy_review','sell_review','risk_review',"
            "'trend_recovery_review','rebalance_review')",
            name="ck_investment_report_items_intent",
        ),
        # Watch items must carry a watch_condition payload.
        CheckConstraint(
            "item_kind <> 'watch' OR watch_condition IS NOT NULL",
            name="ck_investment_report_items_watch_has_condition",
        ),
        Index(
            "ix_investment_report_items_report",
            "report_id",
            "status",
        ),
        Index(
            "ix_investment_report_items_kind_status",
            "item_kind",
            "status",
        ),
        Index(
            "ix_investment_report_items_symbol",
            "symbol",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("review.investment_reports.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    item_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str | None] = mapped_column(Text)
    side: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'asset'")
    )

    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    confidence: Mapped[float | None] = mapped_column(Numeric(8, 4))

    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    watch_condition: Mapped[dict | None] = mapped_column(JSONB)
    trigger_checklist: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    max_action: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'proposed'")
    )
    item_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
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

- [ ] **Step 2: Extend the session fixture to include the new table**

Edit `tests/test_investment_reports_model.py`. Replace the `session` fixture body's `tables=[InvestmentReport.__table__]` (in both `create_all` and `drop_all`) with the full table list:

```python
from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
)

_ALL_TABLES = [
    InvestmentReport.__table__,
    InvestmentReportItem.__table__,
]


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_ALL_TABLES, checkfirst=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=list(reversed(_ALL_TABLES)), checkfirst=True)
    await engine.dispose()
```

- [ ] **Step 3: Append item tests**

Append to `tests/test_investment_reports_model.py`:

```python
async def _make_report(session: AsyncSession, **overrides) -> InvestmentReport:
    row = InvestmentReport(**_base_payload(**overrides))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _base_item_payload(report_id: int, **overrides) -> dict:
    payload = dict(
        report_id=report_id,
        item_uuid=uuid.uuid4(),
        idempotency_key=f"item-{uuid.uuid4()}",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        target_kind="asset",
        priority=10,
        rationale="정규장 확인 후 수동 승인 후보",
    )
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_item_round_trip(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)
    assert item.status == "proposed"
    assert item.target_kind == "asset"
    assert item.trigger_checklist == []


@pytest.mark.asyncio
async def test_item_kind_check(session: AsyncSession) -> None:
    report = await _make_report(session)
    session.add(InvestmentReportItem(**_base_item_payload(report.id, item_kind="bogus")))
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_watch_item_requires_condition(session: AsyncSession) -> None:
    report = await _make_report(session)
    # Missing watch_condition for item_kind='watch' → violation.
    session.add(
        InvestmentReportItem(
            **_base_item_payload(report.id, item_kind="watch", side=None)
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_watch_item_with_condition_inserts(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(
        **_base_item_payload(
            report.id,
            item_kind="watch",
            side=None,
            intent="trend_recovery_review",
            watch_condition={
                "metric": "rsi",
                "operator": "below",
                "threshold": 30,
                "target_kind": "asset",
            },
        )
    )
    session.add(item)
    await session.commit()
    assert item.watch_condition["metric"] == "rsi"


@pytest.mark.asyncio
async def test_target_kind_check_rejects_unknown(session: AsyncSession) -> None:
    report = await _make_report(session)
    session.add(
        InvestmentReportItem(
            **_base_item_payload(report.id, target_kind="commodity")
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_cascade_delete_from_report(session: AsyncSession) -> None:
    report = await _make_report(session)
    session.add(InvestmentReportItem(**_base_item_payload(report.id)))
    session.add(InvestmentReportItem(**_base_item_payload(report.id)))
    await session.commit()

    await session.delete(report)
    await session.commit()

    remaining = await session.scalar(
        sa.select(sa.func.count()).select_from(InvestmentReportItem)
    )
    assert remaining == 0
```

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest tests/test_investment_reports_model.py -v`
Expected: all tests PASS (6 from Task 3 + 6 from this task = 12).

- [ ] **Step 5: Commit**

```bash
git add app/models/investment_reports.py tests/test_investment_reports_model.py
git commit -m "feat(rob-265): add InvestmentReportItem with target_kind + watch-condition check"
```

---

### Task 5: `InvestmentReportItemDecision` ORM + tests

**Files:**
- Modify: `app/models/investment_reports.py`
- Modify: `tests/test_investment_reports_model.py`

- [ ] **Step 1: Append the `InvestmentReportItemDecision` class**

Append to `app/models/investment_reports.py`:

```python
# ---------------------------------------------------------------------------
# review.investment_report_item_decisions — operator decision audit (ROB-265)
# ---------------------------------------------------------------------------
class InvestmentReportItemDecision(Base):
    """One decision row per (item, actor, idempotency_key).

    Multiple decisions per item are allowed (e.g. ``defer`` → later
    ``approve``). The latest-decision query is left to the service layer.
    """

    __tablename__ = "investment_report_item_decisions"
    __table_args__ = (
        UniqueConstraint(
            "decision_uuid", name="uq_investment_report_item_decisions_decision_uuid"
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_investment_report_item_decisions_idempotency_key",
        ),
        CheckConstraint(
            "decision IN ('approve','deny','defer','skip','partial_approve')",
            name="ck_investment_report_item_decisions_decision",
        ),
        Index(
            "ix_investment_report_item_decisions_item_created",
            "item_id",
            "created_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("review.investment_report_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    decision: Mapped[str] = mapped_column(Text, nullable=False)
    decision_note: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    approved_payload_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 2: Add the new table to `_ALL_TABLES` and append decision tests**

In `tests/test_investment_reports_model.py`, update imports and `_ALL_TABLES`:

```python
from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
)

_ALL_TABLES = [
    InvestmentReport.__table__,
    InvestmentReportItem.__table__,
    InvestmentReportItemDecision.__table__,
]
```

Append the tests:

```python
@pytest.mark.asyncio
async def test_decision_round_trip(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    decision = InvestmentReportItemDecision(
        item_id=item.id,
        decision_uuid=uuid.uuid4(),
        idempotency_key=f"dec-{uuid.uuid4()}",
        decision="approve",
        actor="operator-test",
    )
    session.add(decision)
    await session.commit()

    fetched = await session.scalar(
        sa.select(InvestmentReportItemDecision).where(
            InvestmentReportItemDecision.id == decision.id
        )
    )
    assert fetched.decision == "approve"


@pytest.mark.asyncio
async def test_decision_check_rejects_unknown(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    session.add(
        InvestmentReportItemDecision(
            item_id=item.id,
            decision_uuid=uuid.uuid4(),
            idempotency_key=f"dec-{uuid.uuid4()}",
            decision="unknown-verb",
            actor="operator-test",
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_multiple_decisions_per_item_allowed(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    for verb in ("defer", "approve"):
        session.add(
            InvestmentReportItemDecision(
                item_id=item.id,
                decision_uuid=uuid.uuid4(),
                idempotency_key=f"dec-{uuid.uuid4()}",
                decision=verb,
                actor="operator-test",
            )
        )
    await session.commit()

    total = await session.scalar(
        sa.select(sa.func.count()).select_from(InvestmentReportItemDecision)
    )
    assert total == 2
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_investment_reports_model.py -v`
Expected: 15 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/models/investment_reports.py tests/test_investment_reports_model.py
git commit -m "feat(rob-265): add InvestmentReportItemDecision audit table"
```

---

### Task 6: `InvestmentWatchAlert` ORM + tests

**Files:**
- Modify: `app/models/investment_reports.py`
- Modify: `tests/test_investment_reports_model.py`

- [ ] **Step 1: Append the `InvestmentWatchAlert` class**

Append to `app/models/investment_reports.py`:

```python
# ---------------------------------------------------------------------------
# review.investment_watch_alerts — activated watch projection (ROB-265)
# ---------------------------------------------------------------------------
class InvestmentWatchAlert(Base):
    """Immutable activation snapshot for an approved watch item.

    Items are the source of truth; alerts duplicate scanner-critical fields
    so the scanner doesn't have to join back to items on every tick. Once
    activated, the snapshot fields here are not mutated except for
    ``status`` and ``updated_at``.
    """

    __tablename__ = "investment_watch_alerts"
    __table_args__ = (
        UniqueConstraint("alert_uuid", name="uq_investment_watch_alerts_alert_uuid"),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_watch_alerts_idempotency_key"
        ),
        CheckConstraint(
            "status IN ('active','triggered','expired','canceled')",
            name="ck_investment_watch_alerts_status",
        ),
        CheckConstraint(
            "target_kind IN ('asset','index','fx')",
            name="ck_investment_watch_alerts_target_kind",
        ),
        CheckConstraint(
            "operator IN ('above','below')",
            name="ck_investment_watch_alerts_operator",
        ),
        CheckConstraint(
            "action_mode IN ('notify_only','preview_only','approval_required')",
            name="ck_investment_watch_alerts_action_mode",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_investment_watch_alerts_market",
        ),
        Index(
            "ix_investment_watch_alerts_market_status",
            "market",
            "status",
        ),
        Index(
            "ix_investment_watch_alerts_status_valid_until",
            "status",
            "valid_until",
        ),
        Index(
            "ix_investment_watch_alerts_source_report",
            "source_report_uuid",
        ),
        Index(
            "ix_investment_watch_alerts_source_item",
            "source_item_uuid",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alert_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    source_report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    source_item_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )

    market: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    threshold_key: Mapped[str] = mapped_column(Text, nullable=False)

    intent: Mapped[str] = mapped_column(Text, nullable=False)
    action_mode: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_checklist: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    max_action: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    alert_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

- [ ] **Step 2: Extend `_ALL_TABLES` and add alert tests**

In `tests/test_investment_reports_model.py`, add to imports and `_ALL_TABLES`:

```python
from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentWatchAlert,
)

_ALL_TABLES = [
    InvestmentReport.__table__,
    InvestmentReportItem.__table__,
    InvestmentReportItemDecision.__table__,
    InvestmentWatchAlert.__table__,
]
```

Append:

```python
def _base_alert_payload(report_uuid: uuid.UUID, item_uuid: uuid.UUID, **overrides) -> dict:
    payload = dict(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"alert-{uuid.uuid4()}",
        source_report_uuid=report_uuid,
        source_item_uuid=item_uuid,
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=70000,
        threshold_key="70000",
        intent="buy_review",
        action_mode="notify_only",
        rationale="저점 매수 후보 모니터링",
    )
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_alert_round_trip(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    alert = InvestmentWatchAlert(
        **_base_alert_payload(report.report_uuid, item.item_uuid)
    )
    session.add(alert)
    await session.commit()
    await session.refresh(alert)
    assert alert.status == "active"
    assert alert.target_kind == "asset"


@pytest.mark.asyncio
async def test_alert_action_mode_check(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    session.add(
        InvestmentWatchAlert(
            **_base_alert_payload(
                report.report_uuid,
                item.item_uuid,
                action_mode="auto_execute",
            )
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_alert_target_kind_index_allowed(session: AsyncSession) -> None:
    """Scanner asset/index/fx dimensions must survive."""
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id, target_kind="index"))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    alert = InvestmentWatchAlert(
        **_base_alert_payload(
            report.report_uuid,
            item.item_uuid,
            target_kind="index",
            symbol="KOSPI",
            metric="price",
        )
    )
    session.add(alert)
    await session.commit()
    assert alert.target_kind == "index"
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_investment_reports_model.py -v`
Expected: 18 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/models/investment_reports.py tests/test_investment_reports_model.py
git commit -m "feat(rob-265): add InvestmentWatchAlert activation snapshot"
```

---

### Task 7: `InvestmentWatchEvent` ORM + tests

**Files:**
- Modify: `app/models/investment_reports.py`
- Modify: `tests/test_investment_reports_model.py`

- [ ] **Step 1: Append the `InvestmentWatchEvent` class**

Append to `app/models/investment_reports.py`:

```python
# ---------------------------------------------------------------------------
# review.investment_watch_events — scanner trigger events (ROB-265)
# ---------------------------------------------------------------------------
class InvestmentWatchEvent(Base):
    """Scanner-emitted trigger event linked back to source report/item.

    Replaces every legacy write path that went through
    ``watch_order_intent_ledger``. ``idempotency_key`` is
    ``alert_uuid:kst_date:threshold_key`` so a single watch can only
    fire once per day per threshold cross.
    """

    __tablename__ = "investment_watch_events"
    __table_args__ = (
        UniqueConstraint("event_uuid", name="uq_investment_watch_events_event_uuid"),
        UniqueConstraint(
            "idempotency_key", name="uq_investment_watch_events_idempotency_key"
        ),
        CheckConstraint(
            "outcome IN ('notified','review_required','preview_attached',"
            "'expired','ignored','failed')",
            name="ck_investment_watch_events_outcome",
        ),
        Index(
            "ix_investment_watch_events_alert_created",
            "alert_id",
            "created_at",
        ),
        Index(
            "ix_investment_watch_events_source_report",
            "source_report_uuid",
        ),
        Index(
            "ix_investment_watch_events_kst_date",
            "kst_date",
        ),
        Index(
            "ix_investment_watch_events_outcome_created",
            "outcome",
            "created_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    # Use SET NULL so historical events survive an alert deletion (audit).
    alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("review.investment_watch_alerts.id", ondelete="SET NULL")
    )
    source_report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    source_item_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )

    current_value: Mapped[float | None] = mapped_column(Numeric(20, 8))
    threshold: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    scanner_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    follow_up_report_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("review.investment_report_items.id", ondelete="SET NULL")
    )

    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    kst_date: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 2: Extend `_ALL_TABLES` and add event tests**

In `tests/test_investment_reports_model.py`, add to imports and `_ALL_TABLES`:

```python
from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)

_ALL_TABLES = [
    InvestmentReport.__table__,
    InvestmentReportItem.__table__,
    InvestmentReportItemDecision.__table__,
    InvestmentWatchAlert.__table__,
    InvestmentWatchEvent.__table__,
]
```

Append:

```python
@pytest.mark.asyncio
async def test_event_round_trip(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)
    alert = InvestmentWatchAlert(
        **_base_alert_payload(report.report_uuid, item.item_uuid)
    )
    session.add(alert)
    await session.commit()
    await session.refresh(alert)

    event = InvestmentWatchEvent(
        event_uuid=uuid.uuid4(),
        idempotency_key=f"{alert.alert_uuid}:2026-05-18:70000",
        alert_id=alert.id,
        source_report_uuid=report.report_uuid,
        source_item_uuid=item.item_uuid,
        current_value=69500,
        threshold=70000,
        outcome="notified",
        correlation_id=str(uuid.uuid4()),
        kst_date="2026-05-18",
    )
    session.add(event)
    await session.commit()
    assert event.id is not None


@pytest.mark.asyncio
async def test_event_outcome_check(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    session.add(
        InvestmentWatchEvent(
            event_uuid=uuid.uuid4(),
            idempotency_key=f"x-{uuid.uuid4()}",
            source_report_uuid=report.report_uuid,
            source_item_uuid=item.item_uuid,
            threshold=70000,
            outcome="auto_executed",  # not in allowed set
            correlation_id=str(uuid.uuid4()),
            kst_date="2026-05-18",
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_event_idempotency_dedup(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    key = f"dup-event-{uuid.uuid4()}"
    base = dict(
        source_report_uuid=report.report_uuid,
        source_item_uuid=item.item_uuid,
        threshold=70000,
        outcome="notified",
        correlation_id=str(uuid.uuid4()),
        kst_date="2026-05-18",
    )

    session.add(
        InvestmentWatchEvent(
            event_uuid=uuid.uuid4(), idempotency_key=key, **base
        )
    )
    await session.commit()

    session.add(
        InvestmentWatchEvent(
            event_uuid=uuid.uuid4(), idempotency_key=key, **base
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_event_survives_alert_deletion(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    alert = InvestmentWatchAlert(
        **_base_alert_payload(report.report_uuid, item.item_uuid)
    )
    session.add(alert)
    await session.commit()
    await session.refresh(alert)

    event = InvestmentWatchEvent(
        event_uuid=uuid.uuid4(),
        idempotency_key=f"keep-{uuid.uuid4()}",
        alert_id=alert.id,
        source_report_uuid=report.report_uuid,
        source_item_uuid=item.item_uuid,
        threshold=70000,
        outcome="notified",
        correlation_id=str(uuid.uuid4()),
        kst_date="2026-05-18",
    )
    session.add(event)
    await session.commit()

    await session.delete(alert)
    await session.commit()

    refetched = await session.scalar(
        sa.select(InvestmentWatchEvent).where(InvestmentWatchEvent.id == event.id)
    )
    assert refetched is not None
    assert refetched.alert_id is None
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_investment_reports_model.py -v`
Expected: 22 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/models/investment_reports.py tests/test_investment_reports_model.py
git commit -m "feat(rob-265): add InvestmentWatchEvent with audit-preserving FK"
```

---

### Task 8: Idempotency-key composers + canonical hash

**Files:**
- Modify: `app/services/investment_reports/idempotency.py`
- Create: `tests/test_investment_reports_idempotency.py`

- [ ] **Step 1: Replace the idempotency module body with concrete composers**

Write to `app/services/investment_reports/idempotency.py` (replacing the skeleton):

```python
"""Deterministic idempotency-key composers for ROB-265 investment reports.

All composers return a colon-joined, lowercase-where-applicable string.
``_`` is the slot for ``None`` so a missing field never collides with a
real value. The canonical watch-condition hash is sha256 of a JSON dump
with sorted keys, so logically equivalent payloads produce the same hash
regardless of dict insertion order.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "report_key",
    "item_key",
    "watch_activation_key",
    "watch_event_key",
    "canonical_watch_condition_hash",
]

_NONE_SLOT = "_"


def _slot(value: Any) -> str:
    if value is None:
        return _NONE_SLOT
    return str(value).strip().lower()


def report_key(
    *,
    report_type: str,
    market: str,
    market_session: str | None,
    kst_date: str,
    generator_version: str,
) -> str:
    """Stable key for one generator pass producing one report."""
    return ":".join(
        [
            "report",
            _slot(report_type),
            _slot(market),
            _slot(market_session),
            _slot(kst_date),
            _slot(generator_version),
        ]
    )


def item_key(
    *,
    report_uuid: str,
    item_kind: str,
    symbol: str | None,
    side: str | None,
    intent: str,
    watch_condition: dict | None,
) -> str:
    """Stable key per (report, kind, symbol, side, intent, condition)."""
    condition_hash = (
        canonical_watch_condition_hash(watch_condition)
        if watch_condition is not None
        else _NONE_SLOT
    )
    return ":".join(
        [
            "item",
            _slot(report_uuid),
            _slot(item_kind),
            _slot(symbol),
            _slot(side),
            _slot(intent),
            condition_hash,
        ]
    )


def watch_activation_key(*, source_item_uuid: str) -> str:
    """One activation per approved item."""
    return ":".join(["activation", _slot(source_item_uuid)])


def watch_event_key(
    *, alert_uuid: str, kst_date: str, threshold_key: str
) -> str:
    """One event per (alert, day, threshold)."""
    return ":".join(
        [
            "event",
            _slot(alert_uuid),
            _slot(kst_date),
            _slot(threshold_key),
        ]
    )


def canonical_watch_condition_hash(payload: dict) -> str:
    """sha256 of sort_keys JSON dump.

    Returns the first 16 hex chars — enough collision-resistance for an
    idempotency-key slot and short enough to stay readable in logs.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 2: Create the idempotency unit tests**

Write to `tests/test_investment_reports_idempotency.py`:

```python
"""ROB-265 — deterministic idempotency key composer tests."""

from __future__ import annotations

import pytest

from app.services.investment_reports.idempotency import (
    canonical_watch_condition_hash,
    item_key,
    report_key,
    watch_activation_key,
    watch_event_key,
)


def test_report_key_is_stable() -> None:
    a = report_key(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    b = report_key(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    assert a == b == "report:kr_morning:kr:regular:2026-05-18:v1"


def test_report_key_none_slot() -> None:
    key = report_key(
        report_type="crypto_morning",
        market="crypto",
        market_session=None,
        kst_date="2026-05-18",
        generator_version="v1",
    )
    assert key == "report:crypto_morning:crypto:_:2026-05-18:v1"


def test_canonical_hash_is_order_independent() -> None:
    a = canonical_watch_condition_hash({"metric": "rsi", "operator": "below", "threshold": 30})
    b = canonical_watch_condition_hash({"threshold": 30, "operator": "below", "metric": "rsi"})
    assert a == b
    assert len(a) == 16


def test_canonical_hash_changes_on_value_change() -> None:
    a = canonical_watch_condition_hash({"metric": "rsi", "threshold": 30})
    b = canonical_watch_condition_hash({"metric": "rsi", "threshold": 31})
    assert a != b


def test_item_key_with_and_without_condition() -> None:
    with_cond = item_key(
        report_uuid="REPORT-UUID",
        item_kind="watch",
        symbol="005930",
        side=None,
        intent="trend_recovery_review",
        watch_condition={"metric": "rsi", "operator": "below", "threshold": 30},
    )
    without_cond = item_key(
        report_uuid="REPORT-UUID",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        watch_condition=None,
    )
    assert with_cond.startswith("item:report-uuid:watch:005930:_:trend_recovery_review:")
    assert without_cond == "item:report-uuid:action:005930:buy:buy_review:_"


def test_item_key_condition_change_changes_key() -> None:
    a = item_key(
        report_uuid="R",
        item_kind="watch",
        symbol="005930",
        side=None,
        intent="trend_recovery_review",
        watch_condition={"metric": "rsi", "threshold": 30},
    )
    b = item_key(
        report_uuid="R",
        item_kind="watch",
        symbol="005930",
        side=None,
        intent="trend_recovery_review",
        watch_condition={"metric": "rsi", "threshold": 31},
    )
    assert a != b


def test_watch_activation_key() -> None:
    assert (
        watch_activation_key(source_item_uuid="ITEM-UUID")
        == "activation:item-uuid"
    )


def test_watch_event_key() -> None:
    assert (
        watch_event_key(
            alert_uuid="ALERT-UUID", kst_date="2026-05-18", threshold_key="70000"
        )
        == "event:alert-uuid:2026-05-18:70000"
    )
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_investment_reports_idempotency.py -v`
Expected: 8 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/services/investment_reports/idempotency.py tests/test_investment_reports_idempotency.py
git commit -m "feat(rob-265): deterministic idempotency composers for reports/items/watches"
```

---

### Task 9: Alembic migration — single `upgrade()` / `downgrade()`

**Files:**
- Create: `alembic/versions/<NEW_REV>_rob265_add_investment_reports.py`

- [ ] **Step 1: Pick a stable revision id**

Choose `20260518_rob265` as the revision id (matches the project's date-prefixed convention used by `20260515_rob257`). Filename: `alembic/versions/20260518_rob265_add_investment_reports.py`.

- [ ] **Step 2: Write the migration**

Write to `alembic/versions/20260518_rob265_add_investment_reports.py`. Replace `<HEAD_FROM_TASK_1>` with the revision id you captured in Task 1, Step 2.

```python
"""rob-265 add investment_* tables (additive)

Revision ID: 20260518_rob265
Revises: <HEAD_FROM_TASK_1>
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260518_rob265"
down_revision: str | None = "<HEAD_FROM_TASK_1>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb_default(literal: str) -> sa.sql.elements.TextClause:
    return sa.text(f"'{literal}'::jsonb")


def upgrade() -> None:
    # ----------------------------------------------------------------
    # review.investment_reports
    # ----------------------------------------------------------------
    op.create_table(
        "investment_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("report_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("market_session", sa.Text(), nullable=True),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column(
            "execution_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'advisory_only'"),
        ),
        sa.Column("created_by_profile", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("risk_summary", sa.Text(), nullable=True),
        sa.Column("thesis_text", sa.Text(), nullable=True),
        sa.Column("no_action_note", sa.Text(), nullable=True),
        sa.Column(
            "market_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "portfolio_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "previous_report_uuid", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'draft'")
        ),
        sa.Column(
            "metadata",
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
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','published','decided','expired','superseded')",
            name="ck_investment_reports_status",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('advisory_only','mock_preview')",
            name="ck_investment_reports_execution_mode",
        ),
        sa.CheckConstraint(
            "account_scope IS NULL OR account_scope IN "
            "('kis_live','kis_mock','alpaca_paper','upbit_live')",
            name="ck_investment_reports_account_scope",
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_investment_reports_market",
        ),
        sa.CheckConstraint(
            "market_session IS NULL OR market_session IN "
            "('regular','nxt','pre','post','24x7')",
            name="ck_investment_reports_market_session",
        ),
        sa.CheckConstraint(
            "account_scope IS DISTINCT FROM 'kis_live' "
            "OR execution_mode = 'advisory_only'",
            name="ck_investment_reports_live_advisory_only",
        ),
        sa.CheckConstraint(
            "market_session IS DISTINCT FROM 'nxt' "
            "OR execution_mode = 'advisory_only'",
            name="ck_investment_reports_nxt_advisory_only",
        ),
        sa.UniqueConstraint("report_uuid", name="uq_investment_reports_report_uuid"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_investment_reports_idempotency_key"
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_reports_market_session_created",
        "investment_reports",
        ["market", "market_session", "created_at"],
        schema="review",
    )
    op.create_index(
        "ix_investment_reports_status_created",
        "investment_reports",
        ["status", "created_at"],
        schema="review",
    )
    op.create_index(
        "ix_investment_reports_report_type_created",
        "investment_reports",
        ["report_type", "created_at"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_report_items
    # ----------------------------------------------------------------
    op.create_table(
        "investment_report_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("item_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column(
            "target_kind",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'asset'"),
        ),
        sa.Column(
            "priority", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("confidence", sa.Numeric(8, 4), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "evidence_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "watch_condition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "trigger_checklist",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("[]"),
        ),
        sa.Column(
            "max_action",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'proposed'"),
        ),
        sa.Column(
            "metadata",
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
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "item_kind IN ('action','watch','risk')",
            name="ck_investment_report_items_item_kind",
        ),
        sa.CheckConstraint(
            "status IN ('proposed','approved','denied','deferred','activated','expired')",
            name="ck_investment_report_items_status",
        ),
        sa.CheckConstraint(
            "target_kind IN ('asset','index','fx')",
            name="ck_investment_report_items_target_kind",
        ),
        sa.CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name="ck_investment_report_items_side",
        ),
        sa.CheckConstraint(
            "intent IN ('buy_review','sell_review','risk_review',"
            "'trend_recovery_review','rebalance_review')",
            name="ck_investment_report_items_intent",
        ),
        sa.CheckConstraint(
            "item_kind <> 'watch' OR watch_condition IS NOT NULL",
            name="ck_investment_report_items_watch_has_condition",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["review.investment_reports.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "item_uuid", name="uq_investment_report_items_item_uuid"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_investment_report_items_idempotency_key"
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_report_items_report",
        "investment_report_items",
        ["report_id", "status"],
        schema="review",
    )
    op.create_index(
        "ix_investment_report_items_kind_status",
        "investment_report_items",
        ["item_kind", "status"],
        schema="review",
    )
    op.create_index(
        "ix_investment_report_items_symbol",
        "investment_report_items",
        ["symbol"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_report_item_decisions
    # ----------------------------------------------------------------
    op.create_table(
        "investment_report_item_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("decision_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "approved_payload_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('approve','deny','defer','skip','partial_approve')",
            name="ck_investment_report_item_decisions_decision",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["review.investment_report_items.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "decision_uuid",
            name="uq_investment_report_item_decisions_decision_uuid",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_investment_report_item_decisions_idempotency_key",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_report_item_decisions_item_created",
        "investment_report_item_decisions",
        ["item_id", "created_at"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_watch_alerts
    # ----------------------------------------------------------------
    op.create_table(
        "investment_watch_alerts",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("alert_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "source_report_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "source_item_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(20, 8), nullable=False),
        sa.Column("threshold_key", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("action_mode", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "trigger_checklist",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("[]"),
        ),
        sa.Column(
            "max_action",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column(
            "metadata",
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
        sa.Column(
            "activated_at",
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
        sa.CheckConstraint(
            "status IN ('active','triggered','expired','canceled')",
            name="ck_investment_watch_alerts_status",
        ),
        sa.CheckConstraint(
            "target_kind IN ('asset','index','fx')",
            name="ck_investment_watch_alerts_target_kind",
        ),
        sa.CheckConstraint(
            "operator IN ('above','below')",
            name="ck_investment_watch_alerts_operator",
        ),
        sa.CheckConstraint(
            "action_mode IN ('notify_only','preview_only','approval_required')",
            name="ck_investment_watch_alerts_action_mode",
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="ck_investment_watch_alerts_market",
        ),
        sa.UniqueConstraint(
            "alert_uuid", name="uq_investment_watch_alerts_alert_uuid"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_investment_watch_alerts_idempotency_key"
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_alerts_market_status",
        "investment_watch_alerts",
        ["market", "status"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_alerts_status_valid_until",
        "investment_watch_alerts",
        ["status", "valid_until"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_alerts_source_report",
        "investment_watch_alerts",
        ["source_report_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_alerts_source_item",
        "investment_watch_alerts",
        ["source_item_uuid"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_watch_events
    # ----------------------------------------------------------------
    op.create_table(
        "investment_watch_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("event_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "source_report_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "source_item_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("current_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("threshold", sa.Numeric(20, 8), nullable=False),
        sa.Column(
            "scanner_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("follow_up_report_item_id", sa.BigInteger(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("kst_date", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('notified','review_required','preview_attached',"
            "'expired','ignored','failed')",
            name="ck_investment_watch_events_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["review.investment_watch_alerts.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["follow_up_report_item_id"],
            ["review.investment_report_items.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "event_uuid", name="uq_investment_watch_events_event_uuid"
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_investment_watch_events_idempotency_key",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_events_alert_created",
        "investment_watch_events",
        ["alert_id", "created_at"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_events_source_report",
        "investment_watch_events",
        ["source_report_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_events_kst_date",
        "investment_watch_events",
        ["kst_date"],
        schema="review",
    )
    op.create_index(
        "ix_investment_watch_events_outcome_created",
        "investment_watch_events",
        ["outcome", "created_at"],
        schema="review",
    )


def downgrade() -> None:
    # Drop in reverse FK order.
    op.drop_index(
        "ix_investment_watch_events_outcome_created",
        table_name="investment_watch_events",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_events_kst_date",
        table_name="investment_watch_events",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_events_source_report",
        table_name="investment_watch_events",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_events_alert_created",
        table_name="investment_watch_events",
        schema="review",
    )
    op.drop_table("investment_watch_events", schema="review")

    op.drop_index(
        "ix_investment_watch_alerts_source_item",
        table_name="investment_watch_alerts",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_alerts_source_report",
        table_name="investment_watch_alerts",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_alerts_status_valid_until",
        table_name="investment_watch_alerts",
        schema="review",
    )
    op.drop_index(
        "ix_investment_watch_alerts_market_status",
        table_name="investment_watch_alerts",
        schema="review",
    )
    op.drop_table("investment_watch_alerts", schema="review")

    op.drop_index(
        "ix_investment_report_item_decisions_item_created",
        table_name="investment_report_item_decisions",
        schema="review",
    )
    op.drop_table("investment_report_item_decisions", schema="review")

    op.drop_index(
        "ix_investment_report_items_symbol",
        table_name="investment_report_items",
        schema="review",
    )
    op.drop_index(
        "ix_investment_report_items_kind_status",
        table_name="investment_report_items",
        schema="review",
    )
    op.drop_index(
        "ix_investment_report_items_report",
        table_name="investment_report_items",
        schema="review",
    )
    op.drop_table("investment_report_items", schema="review")

    op.drop_index(
        "ix_investment_reports_report_type_created",
        table_name="investment_reports",
        schema="review",
    )
    op.drop_index(
        "ix_investment_reports_status_created",
        table_name="investment_reports",
        schema="review",
    )
    op.drop_index(
        "ix_investment_reports_market_session_created",
        table_name="investment_reports",
        schema="review",
    )
    op.drop_table("investment_reports", schema="review")
```

- [ ] **Step 3: Roundtrip the migration**

Run: `uv run alembic upgrade head`
Expected: ends at `20260518_rob265`, no errors.

Run: `uv run alembic downgrade -1`
Expected: returns to the previous head.

Run: `uv run alembic upgrade head`
Expected: re-applies `20260518_rob265` cleanly.

- [ ] **Step 4: Verify Alembic autogenerate has nothing to add (after Task 10's `__init__.py` export)**

Skip this step until Task 10 finishes — then return and run: `uv run alembic check`
Expected: "No new upgrade operations detected." (zero diff between ORM metadata and the new migration).

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260518_rob265_add_investment_reports.py
git commit -m "feat(rob-265): alembic migration for investment_* tables (additive)"
```

---

### Task 10: Export new models + lint/typecheck + final commit

**Files:**
- Modify: `app/models/__init__.py`

- [ ] **Step 1: Add exports**

Edit `app/models/__init__.py`. Insert this block immediately after the existing `from .research_reports import ...` line, alphabetically ordered with the rest:

```python
from .investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)
```

- [ ] **Step 2: Run `alembic check` (autogenerate diff)**

Run: `uv run alembic check`
Expected: "No new upgrade operations detected." If the output lists pending operations, the ORM-vs-migration column order/server_default/CHECK constraints have drifted — reconcile by editing the migration (not the ORM) so the constraint names match exactly.

- [ ] **Step 3: Format**

Run: `uv run ruff format app/models/investment_reports.py app/services/investment_reports/idempotency.py alembic/versions/20260518_rob265_add_investment_reports.py tests/test_investment_reports_model.py tests/test_investment_reports_idempotency.py app/models/__init__.py`
Expected: zero diff (or files reformatted).

- [ ] **Step 4: Lint**

Run: `uv run ruff check app/models/investment_reports.py app/services/investment_reports/ alembic/versions/20260518_rob265_add_investment_reports.py tests/test_investment_reports_model.py tests/test_investment_reports_idempotency.py app/models/__init__.py`
Expected: "All checks passed!"

- [ ] **Step 5: Typecheck**

Run: `uv run ty check app/models/investment_reports.py app/services/investment_reports/idempotency.py`
Expected: "Success" or "0 errors".

- [ ] **Step 6: Full test sweep for changed paths**

Run: `uv run pytest tests/test_investment_reports_model.py tests/test_investment_reports_idempotency.py -v`
Expected: 30 tests PASS (22 ORM + 8 idempotency).

- [ ] **Step 7: Verify legacy tests still pass (additive guarantee)**

Run: `uv run pytest tests/test_analysis_report_workflow.py tests/test_mcp_watch_order_intent_ledger.py tests/test_watch_order_intent_service.py -v`
Expected: all PASS — nothing in this plan should have touched legacy behavior.

- [ ] **Step 8: Commit final wiring**

```bash
git add app/models/__init__.py
git commit -m "chore(rob-265): export investment_* models from app.models"
```

- [ ] **Step 9: Push and open the Plan-1 PR**

Run: `git push -u origin rob-265`

Open the PR with this body (use `gh pr create`):

```
## Summary
ROB-265 Plan 1 — additive `investment_*` schema + ORM + idempotency helpers. Coexists with legacy `analysis_*` / `watch_order_intent_ledger` (no behavior change). Subsequent plans add services, MCP/API, scanner re-wire, frontend, and the legacy clean cut.

- 5 new tables under `review` schema
- DB-enforced advisory-only invariants for `kis_live` account and `nxt` session
- Deterministic idempotency keys for reports/items/watch activations/watch events
- `target_kind` preserved on items + alerts (asset/index/fx)

## Test plan
- [ ] `uv run pytest tests/test_investment_reports_model.py tests/test_investment_reports_idempotency.py -v`
- [ ] `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
- [ ] `uv run alembic check` reports no pending operations
- [ ] `uv run ruff check ...` clean
- [ ] `uv run ty check ...` clean
- [ ] Legacy suites still pass: `test_analysis_report_workflow.py`, `test_mcp_watch_order_intent_ledger.py`, `test_watch_order_intent_service.py`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

---

## Self-review notes (applied)

- **Spec coverage:** Locked refinements #1–#9 are all addressed in this plan's surface area: narrowed `item_kind`/`item_status` (Task 4), `target_kind` preserved (Tasks 4, 6), advisory-only DB invariants for `kis_live`/`nxt` (Task 3), deterministic idempotency composers (Task 8), source-of-truth items vs. immutable alert snapshot (Tasks 4, 6). Items deferred to later plans: services, MCP/API contract, watch scanner re-wire, frontend, legacy removal — explicitly out of scope here.
- **Placeholders:** Only one — `<HEAD_FROM_TASK_1>` in the migration's `down_revision`. The plan tells the engineer exactly which command to run to obtain it and where to paste it. All other steps have complete code or exact commands.
- **Type consistency:** ORM column names, table names, and constraint names match between Tasks 3–7 and the Alembic migration in Task 9. `_ALL_TABLES` extends monotonically. The metadata field is consistently named `report_metadata`/`item_metadata`/`alert_metadata` in Python but maps to the column name `metadata` via the first positional arg of `mapped_column`, matching the existing `AnalysisReport.report_metadata` precedent in `app/models/review.py`.

---

## Roadmap — remaining plans under ROB-265

These will be written as separate plan documents once Plan 1 lands. Each one is a working, testable PR on its own.

- **Plan 2 — Services (repository + ingestion + query):** `app/services/investment_reports/{repository,ingestion,query_service}.py` with the create-report-bundle, decide-item, activate-watch, and previous-report-context operations. Tests are async pytest against PostgreSQL. No MCP/API yet.
- **Plan 3 — MCP/API contract:** `app/routers/investment_reports.py` (GET-mostly) and `app/mcp_server/tooling/investment_reports_handlers.py` exposing the six tools listed in the Linear issue. Includes OpenClaw notification payload update (correlation_id preserved, intents replaced with report/event context). Legacy tools still registered at this point.
- **Plan 4 — Watch scanner re-wire:** rewrite `app/jobs/watch_scanner.py` to read `investment_watch_alerts` (with `target_kind` dispatch matrix preserved) and write `investment_watch_events`. `WatchOrderIntentService` is left in place during this plan so the legacy ledger keeps accepting writes from nowhere (scanner stops writing). End-state: zero new rows in `watch_order_intent_ledger`. Includes test suite rewrite from `test_watch_order_intent_service.py` to event-flow shape.
- **Plan 5 — Frontend `/invest/reports` + NXT pilot + legacy clean cut:** new desktop/mobile pages under `frontend/invest/src/...`, hooks/API client, route alias from `/invest/action-center`. NXT advisory-only end-to-end smoke (KIS-live account scope + `nxt` session + advisory_only execution). Then drop legacy: `analysis_*` MCP/API/services/models, `watch_order_intent_ledger_*` MCP/API/service/model, and the four legacy tables. PR notes carry the pre-drop row counts (SQL block already drafted in this conversation).

---

**Plan complete and saved to `docs/plans/2026-05-18-ROB-265-plan-1-investment-reports-schema.md`.**
